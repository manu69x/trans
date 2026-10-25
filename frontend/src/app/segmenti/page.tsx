"use client";

/**
 * Scheda Segmenti (PRD §5.4): gestione della segmentazione capitoli/parti.
 *
 * - checkbox per capitolo/part + "Seleziona tutti" e segmentazione massiva
 * - campo "frasi per segmento" (default 1) per accorpare N frasi
 * - token massimi del traduttore ben visibili (16.384, usable dopo reserve)
 * - token stimati per capitolo/part e per segmento: verde entro il massino,
 *   rosso se sfora
 * - risegmentazione manuale (split a metà) per i segmenti troppo lunghi
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { ErrorNote, Loading, StatusBadge } from "@/components/ui";
import {
  ApiError,
  clearSegmentation,
  getSegmentsOverview,
  listProjectJobs,
  listProjects,
  segmentChapter,
  splitSegment,
  verifySegmentCorrespondence,
  type SegmentOverviewChapter,
  type SegmentOverviewSeg,
  type VerifyReport,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export default function SegmentiPage() {
  const queryClient = useQueryClient();
  const { locale, t } = useI18n();
  const intlLocale = locale === "it" ? "it-IT" : "en-GB";
  const nf = useMemo(
    () => new Intl.NumberFormat(intlLocale),
    [intlLocale]
  );
  const [projectId, setProjectId] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [sentencesPerSegment, setSentencesPerSegment] = useState(1);
  // Valore "grezzo" del campo: libero durante la digitazione (il campo può
  // svuotarsi), normalizzato al blur. Fix: prima l'onChange forzava subito
  // a 1 e non si poteva cancellare né riscrivere (es. scrivere 9999).
  const [spsRaw, setSpsRaw] = useState("1");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [toastKind, setToastKind] = useState<"info" | "ok" | "err">("info");
  // Job di segmentazione in volo: poll fino a completamento per segnalare
  // la fine (prima il toast "avviata" restava per sempre e nessuno diceva
  // quando la segmentazione era finita).
  const [pendingSegJobs, setPendingSegJobs] = useState<string[]>([]);
  // Report di verifica originale <-> segmenti (null = mai eseguita).
  const [verifyReport, setVerifyReport] = useState<VerifyReport | null>(null);

  // Toast auto-dismiss: nessun bannero permanente.
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(t);
  }, [toast]);

  function show(msg: string, kind: "info" | "ok" | "err" = "info") {
    setToast(msg);
    setToastKind(kind);
  }

  // Poll dei job di segmentazione: al termine (o fallimento) aggiorna la
  // struttura e notifica.
  useEffect(() => {
    if (pendingSegJobs.length === 0 || !projectId) return;
    let alive = true;
    const timer = setInterval(async () => {
      try {
        const jobs = await listProjectJobs(projectId);
        const mine = jobs.filter((j) => pendingSegJobs.includes(j.id));
        if (mine.length === 0) return;
        const settled = mine.filter(
          (j) => j.status === "completed" || j.status === "failed"
        );
        if (settled.length < mine.length) return;
        if (!alive) return;
        const failed = mine.filter((j) => j.status === "failed").length;
        setPendingSegJobs([]);
        queryClient.invalidateQueries({
          queryKey: ["segmenti-overview", projectId],
        });
        if (failed === 0) {
          show(
            mine.length === 1
              ? t("segments.segDoneOne", { n: mine.length })
              : t("segments.segDoneMany", { n: mine.length }),
            "ok"
          );
        } else {
          show(
            failed === 1
              ? t("segments.segDoneErrOne", { failed, n: mine.length })
              : t("segments.segDoneErrMany", { failed, n: mine.length }),
            "err"
          );
        }
      } catch {
        /* ritenta al prossimo tick */
      }
    }, 2500);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [pendingSegJobs, projectId, queryClient]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setProjectId(params.get("projectId") ?? "");
  }, []);

  const projectsQuery = useQuery({
    queryKey: ["progetti"],
    queryFn: listProjects,
    enabled: typeof window !== "undefined",
  });

  const overviewQuery = useQuery({
    queryKey: ["segmenti-overview", projectId],
    queryFn: () => getSegmentsOverview(projectId),
    enabled: Boolean(projectId) && typeof window !== "undefined",
    refetchInterval: 5_000,
  });

  const segmentMutation = useMutation({
    mutationFn: async (nodeIds: string[]) => {
      const out: string[] = [];
      for (const id of nodeIds) {
        const res = await segmentChapter(projectId, id, sentencesPerSegment);
        out.push(res.job_id);
      }
      return out;
    },
    onSuccess: (jobs) => {
      setPendingSegJobs(jobs);
      show(
        t("segments.segStarted", {
          n: jobs.length,
          sps: sentencesPerSegment,
        })
      );
      setSelected(new Set());
      queryClient.invalidateQueries({
        queryKey: ["segmenti-overview", projectId],
      });
    },
    onError: (e: Error) => show(t("segments.segError", { msg: e.message }), "err"),
  });

  const splitMutation = useMutation({
    mutationFn: ({ id, parts }: { id: string; parts: number }) =>
      splitSegment(projectId, id, parts),
    onSuccess: () => {
      show(t("segments.splitDone"), "ok");
      queryClient.invalidateQueries({
        queryKey: ["segmenti-overview", projectId],
      });
    },
    onError: (e: Error) => show(t("segments.splitError", { msg: e.message }), "err"),
  });

  const clearMutation = useMutation({
    mutationFn: (nodeIds: string[]) => clearSegmentation(projectId, nodeIds),
    onSuccess: (res) => {
      const skipped = (res.chapters_all_approved || []).length;
      let message = t("segments.clearDoneStart", { deleted: res.deleted });
      if (res.kept_approved) {
        message += t("segments.clearKept", { n: res.kept_approved });
      }
      if (skipped) {
        message += t("segments.clearSkipped", { n: skipped });
      }
      show(message + ")", "ok");
      setSelected(new Set());
      queryClient.invalidateQueries({
        queryKey: ["segmenti-overview", projectId],
      });
    },
    onError: (e: Error) => show(t("segments.clearError", { msg: e.message }), "err"),
  });

  // Verifica originale <-> segmenti: report carattere-per-carattere.
  const verifyMutation = useMutation({
    mutationFn: () => verifySegmentCorrespondence(projectId),
    onSuccess: (report) => {
      setVerifyReport(report);
      if (report.ok) {
        show(t("segments.verifyOk"), "ok");
      } else {
        const n = report.summary.chapters_with_issues;
        const u = report.summary.uncovered_pages;
        const extra = u ? t("segments.uncoveredExtra", { u }) : "";
        show(
          n === 1
            ? t("segments.verifyIssuesOne", { n, extra })
            : t("segments.verifyIssuesMany", { n, extra }),
          "err"
        );
      }
    },
    onError: (e: Error) => show(t("segments.verifyError", { msg: e.message }), "err"),
  });

  function switchProject(id: string) {
    const url = new URL(window.location.href);
    url.searchParams.set("projectId", id);
    window.history.replaceState(null, "", url.toString());
    setProjectId(id);
    setSelected(new Set());
    setExpanded(null);
    setVerifyReport(null);
  }

  function toggleAll() {
    const chapters = overviewQuery.data?.chapters ?? [];
    setSelected((prev) => {
      const all = chapters.every((c) => prev.has(c.node_id));
      if (all) return new Set();
      return new Set(chapters.map((c) => c.node_id));
    });
  }

  const chapters = overviewQuery.data?.chapters ?? [];
  const maxTokens = overviewQuery.data?.max_block_tokens ?? 16_384;
  const usableTokens = overviewQuery.data?.usable_block_tokens ?? 11_264;
  const anySelected = selected.size > 0;

  const totals = useMemo(() => {
    const segs = chapters.reduce((a, c) => a + c.segment_count, 0);
    const toks = chapters.reduce((a, c) => a + c.total_tokens, 0);
    const over = chapters.filter((c) => c.over_limit).length;
    return { segs, toks, over };
  }, [chapters]);

  if (!projectId) {
    const projects = projectsQuery.data ?? [];
    return (
      <div className="mx-auto max-w-xl">
        <h1 className="text-2xl font-bold text-white">{t("segments.pickProject")}</h1>
        <p className="mt-1 text-sm text-slate-400">
          {t("segments.pickProjectHint")}
        </p>
        {projectsQuery.isLoading ? (
          <Loading label={t("projects.loading")} />
        ) : projects.length === 0 ? (
          <p className="mt-4 text-sm text-slate-400">
            {t("segments.noProjectsShort")}
          </p>
        ) : (
          <ul className="mt-4 space-y-2">
            {projects.map((p) => (
              <li key={p.id}>
                <button
                  onClick={() => switchProject(p.id)}
                  className="card w-full text-left hover:bg-slate-800/60"
                >
                  <span className="font-medium text-white">{p.title}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-white">{t("segments.title")}</h1>
        <select
          value={projectId}
          onChange={(e) => switchProject(e.target.value)}
          className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
        >
          {(projectsQuery.data ?? []).some((p) => p.id === projectId) ? (
            (projectsQuery.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))
          ) : (
            <option value={projectId}>…</option>
          )}
        </select>
      </div>

      {/* Token massimi del traduttore — ben evidente */}
      <div className="mt-3 rounded-md border border-indigo-800/60 bg-indigo-950/40 px-4 py-3">
        <p className="text-sm text-indigo-100">
          <span className="font-bold">
            {t("segments.maxTokensLabel", { max: nf.format(maxTokens) })}
          </span>{" "}
          · {t("segments.usableLabel")}{" "}
          <span className="font-semibold">{nf.format(usableTokens)}</span>{" "}
          {t("segments.reserveInfo", {
            ctx: overviewQuery.data?.context_reserve_tokens ?? "—",
            out: overviewQuery.data?.output_reserve_tokens ?? "—",
          })}
        </p>
        <p className="mt-1 text-xs text-indigo-300/80">{t("segments.legend")}</p>
      </div>

      {overviewQuery.isLoading ? (
        <Loading label={t("segments.loading")} />
      ) : overviewQuery.isError ? (
        <ErrorNote
          message={
            overviewQuery.error instanceof ApiError
              ? t("segments.apiError", { detail: overviewQuery.error.detail })
              : t("segments.loadError")
          }
        />
      ) : (
        <>
          {/* Toolbar */}
          <div className="mt-4 flex flex-wrap items-center gap-3 border-b border-slate-800 pb-3">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={
                  chapters.length > 0 &&
                  chapters.every((c) => selected.has(c.node_id))
                }
                onChange={toggleAll}
              />
              {t("segments.selectAll")}
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-400">
              {t("segments.sentencesPerSegment")}
              <input
                type="number"
                min={1}
                max={9999}
                value={spsRaw}
                onChange={(e) => {
                  const raw = e.target.value;
                  setSpsRaw(raw);
                  const n = Number(raw);
                  if (raw !== "" && Number.isFinite(n) && n >= 1) {
                    setSentencesPerSegment(Math.min(9999, n));
                  }
                }}
                onBlur={() => {
                  const n = Math.max(1, Math.min(9999, Number(spsRaw) || 1));
                  setSentencesPerSegment(n);
                  setSpsRaw(String(n));
                }}
                className="w-20 rounded bg-slate-800 px-2 py-1 text-sm text-white"
              />
              <span className="text-xs text-slate-500">
                {t("segments.defaultHint")}
              </span>
            </label>
            <button
              onClick={() => segmentMutation.mutate([...selected])}
              disabled={!anySelected || segmentMutation.isPending}
              className="rounded bg-indigo-500/25 px-3 py-1.5 text-sm text-indigo-200 hover:bg-indigo-500/35 disabled:opacity-40"
            >
              {segmentMutation.isPending
                ? t("segments.segmentStart")
                : t("segments.segmentBtn")}
            </button>
            <button
              onClick={() => {
                if (
                  window.confirm(
                    t("segments.clearConfirm", { n: selected.size })
                  )
                ) {
                  clearMutation.mutate([...selected]);
                }
              }}
              disabled={!anySelected || clearMutation.isPending}
              className="rounded bg-red-500/20 px-3 py-1.5 text-sm text-red-200 hover:bg-red-500/30 disabled:opacity-40"
            >
              {clearMutation.isPending
                ? t("segments.clearing")
                : t("segments.clearBtn")}
            </button>
            <button
              onClick={() => verifyMutation.mutate()}
              disabled={verifyMutation.isPending}
              title={t("segments.verifyTitle")}
              className="rounded bg-emerald-500/20 px-3 py-1.5 text-sm text-emerald-200 hover:bg-emerald-500/30 disabled:opacity-40"
            >
              {verifyMutation.isPending
                ? t("segments.verifying")
                : t("segments.verifyBtn")}
            </button>
            <span className="ml-auto text-xs text-slate-500">
              {t("segments.totals", {
                segs: totals.segs,
                toks: nf.format(totals.toks),
                over: totals.over,
              })}
            </span>
          </div>

          {verifyMutation.isPending && (
            <div className="mt-4 rounded-md border border-amber-800/60 bg-amber-950/30 px-4 py-3 text-sm text-amber-200">
              <span className="mr-2 inline-block h-2 w-2 animate-pulse rounded-full bg-amber-400" />
              {t("segments.verifyPending")}
            </div>
          )}

          {verifyReport && !verifyMutation.isPending && (
            <VerifyReportPanel report={verifyReport} onClose={() => setVerifyReport(null)} />
          )}

          {/* Elenco capitoli/parti — tutti, con token stimati subito */}
          {chapters.length === 0 ? (
            <p className="mt-6 text-sm text-slate-400">
              {t("segments.noChaptersBody")}{" "}
              <Link href="/import" className="text-indigo-300 underline">
                {t("segments.noChaptersLink")}
              </Link>{" "}
              {t("segments.noChaptersTail")}
            </p>
          ) : (
            <ul className="mt-4 space-y-2">
              {chapters.map((c) => (
                <ChapterRow
                  key={c.node_id}
                  chapter={c}
                  selected={selected.has(c.node_id)}
                  expanded={expanded === c.node_id}
                  usableTokens={usableTokens}
                  onToggle={() =>
                    setSelected((prev) => {
                      const next = new Set(prev);
                      if (next.has(c.node_id)) next.delete(c.node_id);
                      else next.add(c.node_id);
                      return next;
                    })
                  }
                  onExpand={() =>
                    setExpanded((e) => (e === c.node_id ? null : c.node_id))
                  }
                  onSegmentOne={() => segmentMutation.mutate([c.node_id])}
                  onSplit={(id, parts) =>
                    splitMutation.mutate({ id, parts })
                  }
                  splitPending={splitMutation.isPending}
                />
              ))}
            </ul>
          )}
        </>
      )}

      {pendingSegJobs.length > 0 && (
        <div className="fixed bottom-4 left-1/2 z-10 -translate-x-1/2 rounded bg-slate-800/95 px-4 py-2 text-sm text-slate-200 shadow-lg ring-1 ring-slate-700">
          <span className="mr-2 inline-block h-2 w-2 animate-pulse rounded-full bg-amber-400" />
          {pendingSegJobs.length === 1
            ? t("segments.segRunningOne", { n: pendingSegJobs.length })
            : t("segments.segRunningMany", { n: pendingSegJobs.length })}
        </div>
      )}
      {toast && (
        <div
          className={`fixed bottom-16 left-1/2 z-10 -translate-x-1/2 rounded px-4 py-2 text-sm text-white shadow-lg ${
            toastKind === "ok"
              ? "bg-emerald-600"
              : toastKind === "err"
              ? "bg-red-600"
              : "bg-indigo-600"
          }`}
        >
          {toast}
        </div>
      )}
    </div>
  );
}

function ChapterRow({
  chapter: c,
  selected,
  expanded,
  usableTokens,
  onToggle,
  onExpand,
  onSegmentOne,
  onSplit,
  splitPending,
}: {
  chapter: SegmentOverviewChapter;
  selected: boolean;
  expanded: boolean;
  usableTokens: number;
  onToggle: () => void;
  onExpand: () => void;
  onSegmentOne: () => void;
  onSplit: (segmentId: string, parts: number) => void;
  splitPending: boolean;
}) {
  const { locale, t } = useI18n();
  const intlLocale = locale === "it" ? "it-IT" : "en-GB";
  const hasTokens = c.total_tokens > 0 || c.segment_count > 0;
  const green = hasTokens && !c.over_limit;
  const red = hasTokens && c.over_limit;
  const noSegments = c.segment_count === 0;
  return (
    <li className="card">
      <div className="flex flex-wrap items-center gap-3">
        <input
          type="checkbox"
          checked={selected}
          onClick={(e) => e.stopPropagation()}
          onChange={onToggle}
        />
        <button
          onClick={onExpand}
          className="flex-1 text-left"
          title={t("segments.showSegments")}
        >
          <span className="font-medium text-white">{c.title}</span>
          <span className="ml-2 text-xs text-slate-500">
            ({c.kind === "chapter"
              ? t("segments.kindChapter")
              : c.kind === "part"
                ? t("segments.kindPart")
                : t("segments.kindOther")})
          </span>
          {c.paragraph_count != null && (
            <span className="ml-3 text-xs text-slate-400">
              {c.paragraph_count}{" "}
              {c.paragraph_count === 1
                ? t("segments.paragraphOne")
                : t("segments.paragraphMany")}
            </span>
          )}
          <span
            className={`ml-3 inline-block rounded px-2 py-0.5 text-xs font-semibold ${
              green
                ? "bg-emerald-500/20 text-emerald-300"
                : red
                ? "bg-red-500/20 text-red-300"
                : "bg-slate-700/50 text-slate-400"
            }`}
          >
            {t("segments.tokensCount", {
              n: c.total_tokens.toLocaleString(intlLocale),
            })}
            {red ? t("segments.overFlag") : green ? t("segments.okFlag") : ""}
          </span>
          {noSegments && c.total_tokens > 0 && (
            <span className="ml-2 text-xs text-slate-500">
              {t("segments.estimate")}
            </span>
          )}
        </button>
        <span className="text-xs text-slate-500">
          {t("segments.segmentCount", { n: c.segment_count })}
        </span>
        <span
          className={`h-3 w-3 rounded-full ${
            green ? "bg-emerald-400" : red ? "bg-red-500" : "bg-slate-600"
          }`}
          title={
            green
              ? t("segments.dotGreen")
              : red
                ? t("segments.dotRed")
                : t("segments.dotGray")
          }
        />
        <button
          onClick={onSegmentOne}
          disabled={splitPending}
          className="rounded bg-indigo-500/25 px-2 py-1 text-xs text-indigo-200 hover:bg-indigo-500/35"
        >
          {t("segments.segmentOne")}
        </button>
      </div>
      {expanded && c.segments.length > 0 && (
        <div className="mt-3 border-t border-slate-800 pt-2">
          <p className="mb-1 text-xs text-slate-500">
            {t("segments.limitPerSegment", {
              n: usableTokens.toLocaleString(intlLocale),
            })}
          </p>
          <ul className="max-h-72 space-y-1 overflow-y-auto">
            {c.segments.map((s) => (
              <SegmentRow key={s.segment_id} seg={s} usableTokens={usableTokens} onSplit={onSplit} splitPending={splitPending} />
            ))}
          </ul>
        </div>
      )}
      {expanded && c.segments.length === 0 && (
        <p className="mt-2 text-xs text-slate-500">{t("segments.noSegmentsHint")}</p>
      )}
    </li>
  );
}

function SegmentRow({
  seg: s,
  usableTokens,
  onSplit,
  splitPending,
}: {
  seg: SegmentOverviewSeg;
  usableTokens: number;
  onSplit: (segmentId: string, parts: number) => void;
  splitPending: boolean;
}) {
  const { t } = useI18n();
  return (
    <li
      className={`flex items-center gap-2 rounded px-2 py-1 text-xs ${
        s.over_limit ? "bg-red-950/40" : "bg-slate-800/40"
      }`}
    >
      <span className="text-slate-500">#{s.ordinal}</span>
      {s.numero != null && (
        <span
          className="rounded bg-indigo-500/20 px-1.5 py-0.5 text-[11px] font-semibold text-indigo-300"
          title={t("segments.numeroTitle")}
        >
          N° {s.numero}
        </span>
      )}
      <span
        className={`font-semibold ${s.over_limit ? "text-red-300" : "text-emerald-300"}`}
      >
        {s.tokens} tok
      </span>
      <span className="flex-1 truncate text-slate-300" title={s.text}>
        {s.text}
      </span>
      {s.over_limit && (
        <>
          <input
            type="number"
            min={2}
            max={10}
            defaultValue={2}
            title={t("segments.splitPartsTitle")}
            className="w-14 rounded bg-slate-800 px-1 py-0.5 text-white"
            id={`split-parts-${s.segment_id}`}
          />
          <button
            onClick={() => {
              const el = document.getElementById(
                `split-parts-${s.segment_id}`
              ) as HTMLInputElement | null;
              const parts = Math.max(2, Math.min(10, Number(el?.value) || 2));
              onSplit(s.segment_id, parts);
            }}
            disabled={splitPending}
            className="rounded bg-amber-500/25 px-2 py-0.5 text-amber-200 hover:bg-amber-500/35 disabled:opacity-40"
          >
            {t("segments.resegmentBtn")}
          </button>
        </>
      )}
    </li>
  );
}

/** Formatta un delta con il segno (+12 / -3 / 0). */
function fmtSigned(n: number): string {
  return n > 0 ? `+${n}` : `${n}`;
}

function VerifyReportPanel({
  report,
  onClose,
}: {
  report: VerifyReport;
  onClose: () => void;
}) {
  const { locale, t, dict } = useI18n();
  const intlLocale = locale === "it" ? "it-IT" : "en-GB";
  const issueLabels = dict.enums.verifyIssue as Record<string, string>;
  const diffLabels = dict.enums.verifyDiff as Record<string, string>;
  const s = report.summary;
  const badChapters = report.chapters.filter((c) => !c.ok);
  const warnChapters = report.chapters.filter(
    (c) => c.ok && (c.warnings?.length ?? 0) > 0
  );
  const okChapters = report.chapters.filter(
    (c) => c.ok && (c.warnings?.length ?? 0) === 0
  );
  return (
    <div className="mt-4 rounded-md border border-slate-700 bg-slate-900/70 px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <span
          className={`inline-block rounded px-3 py-1 text-sm font-bold ${
            report.ok
              ? "bg-emerald-500/25 text-emerald-200"
              : "bg-red-500/25 text-red-200"
          }`}
        >
          {report.ok ? (
            t("segments.fullMatch")
          ) : (
            <>
              {`✗ ${
                s.chapters_with_issues + (s.uncovered_pages > 0 ? 1 : 0)
              } ${
                s.chapters_with_issues + (s.uncovered_pages > 0 ? 1 : 0) === 1
                  ? t("segments.problemsOne")
                  : t("segments.problemsMany")
              }`}
            </>
          )}
        </span>
        <span className="text-xs text-slate-400">
          {new Date(report.generated_at).toLocaleString(intlLocale)}
        </span>
        <button
          onClick={onClose}
          className="ml-auto rounded bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
        >
          {t("segments.close")}
        </button>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <Stat
          label={t("segments.statChapters")}
          value={t("segments.statChaptersValue", {
            ok: s.chapters_ok,
            total: s.chapters,
          })}
        />
        <Stat
          label={t("segments.statSegments")}
          value={s.segments_total.toLocaleString(intlLocale)}
        />
        <Stat
          label={t("segments.statOriginalChars")}
          value={s.original_chars.toLocaleString(intlLocale)}
        />
        <Stat
          label={t("segments.statCharDelta")}
          value={fmtSigned(s.char_delta)}
          tone={s.char_delta === 0 ? "ok" : "warn"}
        />
      </div>

      {warnChapters.length > 0 && (
        <div className="mt-3 rounded border border-amber-900/60 bg-amber-950/30 px-3 py-2">
          <p className="text-xs font-semibold text-amber-200">
            {warnChapters.length === 1
              ? t("segments.warnChaptersOne", { n: warnChapters.length })
              : t("segments.warnChaptersMany", { n: warnChapters.length })}
          </p>
          <ul className="mt-1 space-y-1">
            {warnChapters.map((c) => (
              <li key={c.node_id} className="text-[11px] text-slate-400">
                <span className="text-white">{c.title}</span> ·{" "}
                {c.warnings.map((w, i) => (
                  <span key={i}>{w.detail}</span>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.ok ? (
        <p className="mt-3 text-sm text-emerald-300">
          {t("segments.allOk", { n: s.chapters })}
        </p>
      ) : (
        <>
          {badChapters.length > 0 && (
            <ul className="mt-3 space-y-2">
              {badChapters.map((c) => (
                <li
                  key={c.node_id}
                  className="rounded border border-red-900/60 bg-red-950/30 px-3 py-2"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-white">{c.title}</span>
                    <span className="text-xs text-slate-400">
                      {c.start_page != null && c.end_page != null
                        ? t("segments.pagesRange", {
                            start: c.start_page,
                            end: c.end_page,
                          })
                        : t("segments.noPagesRange")}
                    </span>
                    <span className="text-xs text-slate-400">
                      {t("segments.segmentCount", { n: c.segment_count })}
                    </span>
                    <span className="text-xs text-slate-400">
                      {t("segments.origSegChars", {
                        orig: c.original_chars.toLocaleString(intlLocale),
                        seg: c.segment_chars.toLocaleString(intlLocale),
                      })}
                      <span
                        className={
                          c.char_delta === 0
                            ? "text-emerald-300"
                            : "text-red-300"
                        }
                      >
                        Δ {fmtSigned(c.char_delta)}
                      </span>
                    </span>
                  </div>
                  <ul className="mt-1 space-y-1">
                    {c.issues.map((iss, i) => (
                      <li key={i} className="text-xs">
                        <span className="rounded bg-red-500/20 px-1.5 py-0.5 font-semibold text-red-200">
                          {issueLabels[iss.type] ?? iss.type}
                        </span>{" "}
                        <span className="text-slate-300">{iss.detail}</span>
                        {iss.regions && iss.regions.length > 0 && (
                          <ul className="mt-1 space-y-1 border-l-2 border-red-900/60 pl-2">
                            {iss.regions.map((r, j) => (
                              <li key={j} className="text-[11px] text-slate-400">
                                <span className="text-amber-300">
                                  {diffLabels[r.type] ?? r.type}
                                </span>{" "}
                                {t("segments.regionAt", {
                                  pos: r.position.toLocaleString(intlLocale),
                                  from: r.original_len,
                                  to: r.segment_len,
                                })}
                                {r.original_snippet && (
                                  <div className="mt-0.5 rounded bg-slate-950/60 px-2 py-1 font-mono text-[11px] text-red-300">
                                    {t("segments.originalSnippet", {
                                      text: r.original_snippet,
                                    })}
                                  </div>
                                )}
                                {r.segment_snippet && (
                                  <div className="rounded bg-slate-950/60 px-2 py-1 font-mono text-[11px] text-emerald-300">
                                    {t("segments.segmentSnippet", {
                                      text: r.segment_snippet,
                                    })}
                                  </div>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          )}

          {report.uncovered_pages.length > 0 && (
            <div className="mt-3 rounded border border-amber-900/60 bg-amber-950/30 px-3 py-2">
              <p className="text-xs font-semibold text-amber-200">
                {report.uncovered_pages.length === 1
                  ? t("segments.uncoveredOne", {
                      n: report.uncovered_pages.length,
                      chars: report.summary.uncovered_chars.toLocaleString(
                        intlLocale
                      ),
                    })
                  : t("segments.uncoveredMany", {
                      n: report.uncovered_pages.length,
                      chars: report.summary.uncovered_chars.toLocaleString(
                        intlLocale
                      ),
                    })}
              </p>
              <ul className="mt-1 max-h-40 space-y-1 overflow-y-auto">
                {report.uncovered_pages.map((p) => (
                  <li key={p.page} className="text-[11px] text-slate-400">
                    <span className="text-amber-300">
                      {t("segments.pageAbbr", { page: p.page })}
                    </span>{" "}
                    · {t("segments.charsAbbr", { n: p.chars })} ·{" "}
                    <span className="font-mono">{p.preview}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {okChapters.length > 0 && (
            <p className="mt-3 text-xs text-slate-500">
              {okChapters.length === 1
                ? t("segments.verifiedCleanOne", { n: okChapters.length })
                : t("segments.verifiedCleanMany", { n: okChapters.length })}
            </p>
          )}
        </>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  tone?: "neutral" | "ok" | "warn";
}) {
  return (
    <div className="rounded bg-slate-800/60 px-2 py-1.5">
      <p className="text-[10px] uppercase tracking-wide text-slate-500">{label}</p>
      <p
        className={`text-sm font-semibold ${
          tone === "ok"
            ? "text-emerald-300"
            : tone === "warn"
            ? "text-amber-300"
            : "text-white"
        }`}
      >
        {value}
      </p>
    </div>
  );
}
