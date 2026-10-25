"use client";

/**
 * Pagina QA (PRD §11.1, §10.4, §15.4, §18.2 / F4).
 *
 * Tre zone, come l'editor:
 *   - SINISTRA: filtri (severità / capitolo / tipo) + lista issue;
 *   - CENTRO: revisione affiancata EN/IT del segmento dell'issue, con lo span
 *     evidenziato nel target;
 *   - DESTRA: form di annotazione MQM umana su span (categoria + gravità +
 *     commento, §10.4) e report MQM per 1.000 parole per categoria e gravità
 *     (§18.2).
 *
 * Le issue sono collegabili a segmento + span + categoria + gravità (§15.4):
 * ogni riga della lista porta il segmento, la gravità, la categoria MQM e lo
 * span; il form crea righe ``kind=human`` (AC1) e l'editor bilingue (T27)
 * mostra le stesse issue nel pannello destro (AC3).
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { ErrorNote, Loading } from "@/components/ui";
import {
  getQaTaxonomy,
  listProjects,
  listQaIssues,
  listQaMqm,
  listSegments,
  createQaIssue,
  resolveQaIssue,
  getStructure,
  type QaIssue,
  type EditorSegment,
} from "@/lib/api";
import { MQM_SEVERITY_CLASS } from "@/lib/constants";
import { useI18n } from "@/lib/i18n";

/** Escapes per RegExp (evidenziazione span nel target). */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Divide ``text`` nelle occorrenze (case-insensitive) di ``span``. */
function splitBySpan(text: string, span: string | null | undefined) {
  if (!span) return [{ text, hit: false } as { text: string; hit: boolean }];
  const re = new RegExp(escapeRegExp(span), "gi");
  const out: { text: string; hit: boolean }[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index! > last)
      out.push({ text: text.slice(last, m.index!), hit: false });
    out.push({ text: m[0], hit: true });
    last = m.index! + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), hit: false });
  return out.length ? out : [{ text, hit: false } as { text: string; hit: boolean }];
}

function highlight(text: string, span: string | null | undefined): ReactNode {
  return splitBySpan(text, span).map((p, i) =>
    p.hit ? (
      <mark key={i} className="rounded bg-amber-400/40 px-0.5 text-white">
        {p.text}
      </mark>
    ) : (
      <span key={i}>{p.text}</span>
    )
  );
}

const SEVERITY_VALUES = ["", "minor", "major", "critical"] as const;
const KIND_VALUES = ["", "human", "qa", "critic", "qe", "ocr", "entity"] as const;
const GROUP_VALUES = [
  "",
  "accuracy",
  "terminology",
  "italian",
  "style",
  "locale",
  "source",
] as const;

export default function QaPage() {
  const queryClient = useQueryClient();
  const { t, dict } = useI18n();
  const mqmGroups = dict.enums.mqmGroup as Record<string, string>;
  const mqmCategories = dict.enums.mqmCategory as Record<string, string>;
  const mqmSeverities = dict.enums.mqmSeverity as Record<string, string>;
  const qaKinds = dict.enums.qaKind as Record<string, string>;

  // --- progetto ---
  const projectsQuery = useQuery({ queryKey: ["progetti"], queryFn: listProjects });
  const [projectId, setProjectId] = useState<string | null>(null);
  useEffect(() => {
    if (!projectId && projectsQuery.data?.length) {
      setProjectId(projectsQuery.data[0].id);
    }
  }, [projectsQuery.data, projectId]);

  // --- filtri (§11.1: severità / capitolo / tipo) ---
  const structureQuery = useQuery({
    queryKey: ["struttura", projectId],
    queryFn: () => getStructure(projectId as string),
    enabled: Boolean(projectId),
  });
  const chapters = useMemo(
    () =>
      (structureQuery.data?.nodes ?? [])
        .filter((n) => n.kind === "chapter")
        .map((n) => ({ id: n.node_id, label: n.normalized_title ?? n.node_id }))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [structureQuery.data]
  );

  const [severity, setSeverity] = useState<string>("");
  const [chapterId, setChapterId] = useState<string>("");
  const [kind, setKind] = useState<string>("");
  const [group, setGroup] = useState<string>("");
  const [resolved, setResolved] = useState<"all" | "open" | "done">("all");

  const issuesQuery = useQuery({
    queryKey: ["qa-issue", projectId, severity, chapterId, kind, group, resolved],
    queryFn: () =>
      listQaIssues(projectId as string, {
        severity: severity || null,
        chapter_id: chapterId || null,
        kind: kind || null,
        group: group || null,
        resolved: resolved === "all" ? null : resolved === "open",
      }),
    enabled: Boolean(projectId),
  });

  // --- issue selezionata → segmento → revisione affiancata ---
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected =
    issuesQuery.data?.issues.find((i) => i.id === selectedId) ?? null;

  const segmentsQuery = useQuery({
    queryKey: ["segmenti-qa", projectId],
    queryFn: () => listSegments(projectId as string),
    enabled: Boolean(projectId),
  });
  const segment: EditorSegment | null = useMemo(
    () =>
      segmentsQuery.data?.segments.find((s) => s.segment_id === selected?.segment_id) ??
      null,
    [segmentsQuery.data, selected]
  );

  // --- annotazione MQM umana su span (§10.4) ---
  const taxonomyQuery = useQuery({
    queryKey: ["qa-taxonomy", projectId],
    queryFn: () => getQaTaxonomy(projectId as string),
    enabled: Boolean(projectId),
  });

  const [annoCatGroup, setAnnoCatGroup] = useState<string>("");
  const [annoCategory, setAnnoCategory] = useState<string>("");
  const [annoSeverity, setAnnoSeverity] = useState<"minor" | "major" | "critical">(
    "minor"
  );
  const [annoSpan, setAnnoSpan] = useState("");
  const [annoComment, setAnnoComment] = useState("");
  // L'annotazione vale per il segmento dell'issue selezionata, altrimenti per
  // il primo segmento del progetto.
  const annoSegmentId =
    selected?.segment_id ?? segmentsQuery.data?.segments[0]?.segment_id ?? "";
  // Se cambio segmento attivo, riprendo lo span dell'issue come default.
  useEffect(() => {
    if (selected?.span) setAnnoSpan(selected.span);
  }, [selected?.id, selected?.span]);

  const annotateMutation = useMutation({
    mutationFn: (payload: {
      category: string;
      severity: "minor" | "major" | "critical";
      span: string;
      comment: string;
    }) =>
      createQaIssue(projectId as string, {
        unit_id: annoSegmentId,
        severity: payload.severity,
        category: payload.category,
        kind: "human",
        message:
          t("qaPage.annotationMessage", {
            category: mqmCategories[payload.category] ?? payload.category,
          }) +
          (payload.span
            ? t("qaPage.annotationSpanSuffix", { span: payload.span })
            : ""),
        span: payload.span || null,
        comment: payload.comment || null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["qa-issue", projectId] });
      queryClient.invalidateQueries({ queryKey: ["mqm", projectId] });
      setAnnoSpan("");
      setAnnoComment("");
    },
  });

  const resolveMutation = useMutation({
    mutationFn: (issueId: string) =>
      resolveQaIssue(projectId as string, issueId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["qa-issue", projectId] });
      queryClient.invalidateQueries({ queryKey: ["mqm", projectId] });
    },
  });

  // --- report MQM per 1.000 parole (§18.2) ---
  const mqmQuery = useQuery({
    queryKey: ["mqm", projectId, severity, group],
    queryFn: () =>
      listQaMqm(projectId as string, null, severity || null),
    enabled: Boolean(projectId),
  });

  if (!projectId) {
    return (
      <div className="space-y-3">
        <h1 className="text-xl font-semibold">{t("qaPage.title")}</h1>
        {projectsQuery.isLoading ? (
          <Loading label={t("projects.loading")} />
        ) : (
          <ErrorNote message={t("preview.noProjects")} />
        )}
      </div>
    );
  }

  const issues = issuesQuery.data?.issues ?? [];
  const categoryOptions =
    taxonomyQuery.data && taxonomyQuery.data.groups[annoCatGroup]
      ? taxonomyQuery.data.groups[annoCatGroup]
      : [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">{t("qaPage.title")}</h1>
        <select
          value={projectId}
          onChange={(e) => {
            setProjectId(e.target.value);
            setSelectedId(null);
          }}
          className="rounded-md border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-white"
          aria-label={t("importPage.project")}
        >
          {projectsQuery.data?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.title}
            </option>
          ))}
        </select>
        <span className="text-xs text-slate-500">
          {t("qaPage.issuesSummary", {
            n: issues.length,
            critical: issuesQuery.data?.critical_unresolved ?? 0,
          })}
        </span>
      </div>

      {/* Filtri (§11.1: severità / capitolo / tipo) */}
      <div className="flex flex-wrap items-end gap-2 rounded-md border border-slate-800 bg-slate-900/40 px-3 py-2">
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          <span>{t("qaPage.severity")}</span>
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
            className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
          >
            {SEVERITY_VALUES.map((v) => (
              <option key={v} value={v}>
                {v === "" ? t("qaPage.allF") : (mqmSeverities[v] ?? v)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          <span>{t("promptModels.chapterLabel")}</span>
          <select
            value={chapterId}
            onChange={(e) => setChapterId(e.target.value)}
            className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
          >
            <option value="">{t("editor.filterAll")}</option>
            {chapters.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          <span>{t("importPage.type")}</span>
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
          >
            {KIND_VALUES.map((v) => (
              <option key={v} value={v}>
                {v === "" ? t("editor.filterAll") : (qaKinds[v] ?? v)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          <span>{t("qaPage.category")}</span>
          <select
            value={group}
            onChange={(e) => setGroup(e.target.value)}
            className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
          >
            {GROUP_VALUES.map((v) => (
              <option key={v} value={v}>
                {v === "" ? t("editor.filterAll") : (mqmGroups[v] ?? v)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          <span>{t("qaPage.state")}</span>
          <select
            value={resolved}
            onChange={(e) => setResolved(e.target.value as typeof resolved)}
            className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
          >
            <option value="all">{t("qaPage.allF")}</option>
            <option value="open">{t("qaPage.openF")}</option>
            <option value="done">{t("qaPage.resolvedF")}</option>
          </select>
        </label>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* SINISTRA: lista issue */}
        <div className="flex max-h-[70vh] flex-col rounded-md border border-slate-800">
          <div className="border-b border-slate-800 px-3 py-2 text-xs font-semibold text-slate-400">
            {t("qaPage.issueList", { n: issues.length })}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {issuesQuery.isLoading && (
              <Loading label={t("qaPage.loadingIssues")} />
            )}
            {issuesQuery.isError && (
              <div className="p-3">
                <ErrorNote message={t("qaPage.issuesError")} />
              </div>
            )}
            {!issuesQuery.isLoading &&
              issues.length === 0 && (
                <p className="p-3 text-xs text-slate-500">
                  {t("qaPage.noIssues")}
                </p>
              )}
            {issues.map((i) => (
              <button
                key={i.id}
                onClick={() => setSelectedId(i.id)}
                className={`block w-full border-b border-slate-800/60 px-3 py-2 text-left ${
                  selectedId === i.id
                    ? "bg-indigo-500/10"
                    : "hover:bg-slate-800/50"
                }`}
              >
                <div className="flex flex-wrap items-center gap-1.5">
                  <span
                    className={`badge ${MQM_SEVERITY_CLASS[i.severity] ?? ""}`}
                  >
                    {mqmSeverities[i.severity] ?? i.severity}
                  </span>
                  <span className="badge bg-slate-800 text-slate-300">
                    {qaKinds[i.kind] ?? i.kind}
                  </span>
                  {i.category && (
                    <span className="badge bg-slate-700 text-slate-200">
                      {mqmCategories[i.category] ?? i.category}
                    </span>
                  )}
                  {i.resolved && (
                    <span className="badge bg-emerald-500/20 text-emerald-300">
                      {t("qaPage.resolvedBadge")}
                    </span>
                  )}
                </div>
                <p className="mt-1 text-sm text-slate-200">{i.message}</p>
                {i.span && (
                  <p className="mt-0.5 text-xs text-slate-500">
                    {t("qaPage.spanLabel", { span: i.span })}
                  </p>
                )}
                {i.comment && (
                  <p className="mt-0.5 text-xs italic text-slate-400">
                    {i.comment}
                  </p>
                )}
              </button>
            ))}
          </div>
        </div>

        {/* CENTRO: revisione affiancata EN/IT dello span */}
        <div className="flex max-h-[70vh] flex-col rounded-md border border-slate-800">
          <div className="border-b border-slate-800 px-3 py-2 text-xs font-semibold text-slate-400">
            {t("qaPage.reviewTitle")}{" "}
            {selected
              ? t("qaPage.reviewSeg", {
                  id: selected.segment_id.slice(0, 8),
                })
              : ""}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {!selected ? (
              <p className="text-xs text-slate-500">{t("qaPage.pickIssue")}</p>
            ) : !segment ? (
              <p className="text-xs text-slate-500">
                {t("qaPage.loadingSegment")}
              </p>
            ) : (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <div className="mb-1 text-xs text-slate-500">
                    {t("editor.sourcePanel")}
                  </div>
                  <p className="whitespace-pre-wrap rounded bg-slate-800/50 p-2 text-sm text-slate-200">
                    {segment.source_text}
                  </p>
                </div>
                <div>
                  <div className="mb-1 text-xs text-slate-500">
                    {t("editor.targetPanel")}
                  </div>
                  <p className="whitespace-pre-wrap rounded bg-slate-800/50 p-2 text-sm text-white">
                    {highlight(segment.target_text ?? "", selected.span)}
                  </p>
                </div>
              </div>
            )}
          </div>
          {selected && (
            <div className="border-t border-slate-800 p-3">
              <button
                onClick={() => resolveMutation.mutate(selected.id)}
                className="rounded bg-slate-800 px-3 py-1 text-xs text-slate-200 hover:bg-slate-700"
              >
                {selected.resolved
                  ? t("qaPage.reopen")
                  : t("qaPage.markResolved")}
              </button>
            </div>
          )}
        </div>

        {/* DESTRA: annotazione MQM + report §18.2 */}
        <div className="flex max-h-[70vh] flex-col gap-4 overflow-y-auto">
          {/* Annotazione su span (§10.4) */}
          <div className="rounded-md border border-slate-800 p-3">
            <h3 className="text-xs font-semibold text-slate-400">
              {t("qaPage.annotationTitle")}
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              {t("qaPage.segmentLabel")}{" "}
              <span className="text-slate-300">
                {annoSegmentId ? `#${annoSegmentId.slice(0, 8)}…` : "—"}
              </span>
              {selected ? ` ${t("qaPage.ofSelectedIssue")}` : ""}
            </p>
            <div className="mt-2 space-y-2">
              <label className="flex flex-col gap-1 text-xs text-slate-400">
                <span>{t("qaPage.group")}</span>
                <select
                  value={annoCatGroup}
                  onChange={(e) => {
                    setAnnoCatGroup(e.target.value);
                    setAnnoCategory("");
                  }}
                  className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
                >
                  <option value="">—</option>
                  {GROUP_VALUES.filter(Boolean).map((g) => (
                    <option key={g} value={g}>
                      {mqmGroups[g] ?? g}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs text-slate-400">
                <span>{t("qaPage.category")}</span>
                <select
                  value={annoCategory}
                  onChange={(e) => setAnnoCategory(e.target.value)}
                  disabled={!annoCatGroup}
                  className="rounded bg-slate-800 px-2 py-1 text-sm text-white disabled:opacity-40"
                >
                  <option value="">—</option>
                  {categoryOptions.map((c) => (
                    <option key={c} value={c}>
                      {mqmCategories[c] ?? c}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs text-slate-400">
                <span>{t("qaPage.severityField")}</span>
                <select
                  value={annoSeverity}
                  onChange={(e) =>
                    setAnnoSeverity(e.target.value as typeof annoSeverity)
                  }
                  className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
                >
                  {Object.keys(mqmSeverities).map((v) => (
                    <option key={v} value={v}>
                      {mqmSeverities[v]}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs text-slate-400">
                <span>{t("qaPage.spanField")}</span>
                <input
                  value={annoSpan}
                  onChange={(e) => setAnnoSpan(e.target.value)}
                  placeholder={t("qaPage.spanPlaceholder")}
                  className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
                />
              </label>
              <label className="flex flex-col gap-1 text-xs text-slate-400">
                <span>{t("qaPage.comment")}</span>
                <textarea
                  value={annoComment}
                  onChange={(e) => setAnnoComment(e.target.value)}
                  rows={2}
                  placeholder={t("qaPage.commentPlaceholder")}
                  className="resize-none rounded bg-slate-800 px-2 py-1 text-sm text-white"
                />
              </label>
              <button
                disabled={!annoSegmentId || !annoCategory}
                onClick={() =>
                  annotateMutation.mutate({
                    category: annoCategory,
                    severity: annoSeverity,
                    span: annoSpan,
                    comment: annoComment,
                  })
                }
                className="w-full rounded bg-indigo-500/25 py-1.5 text-sm text-indigo-200 hover:bg-indigo-500/35 disabled:opacity-40"
              >
                {t("qaPage.saveAnnotation")}
              </button>
              {annotateMutation.isError && (
                <p className="text-xs text-red-300">
                  {(annotateMutation.error as Error).message}
                </p>
              )}
            </div>
          </div>

          {/* Report MQM per 1.000 parole (§18.2) */}
          <div className="rounded-md border border-slate-800 p-3">
            <h3 className="text-xs font-semibold text-slate-400">
              {t("qaPage.reportTitle")}
            </h3>
            {mqmQuery.isLoading && <Loading label={t("qaPage.computing")} />}
            {mqmQuery.isError && (
              <ErrorNote message={t("qaPage.reportError")} />
            )}
            {mqmQuery.data && (
              <div className="mt-2 space-y-2 text-xs text-slate-300">
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  <span>
                    {t("qaPage.sourceWords")}{" "}
                    <b>{mqmQuery.data.n_source_words}</b>
                  </span>
                  <span>
                    {t("qaPage.issuesTotal")} <b>{mqmQuery.data.total_issues}</b>
                  </span>
                  <span>
                    {t("qaPage.per1000")} <b>{mqmQuery.data.per_1000_all}</b>
                  </span>
                </div>
                {mqmQuery.data.by_category_severity && (
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-left text-slate-500">
                        <th className="py-1 pr-2 font-normal">
                          {t("qaPage.categoryCol")}
                        </th>
                        {Object.keys(mqmSeverities).map((s) => (
                          <th key={s} className="py-1 pr-2 font-normal">
                            {t("qaPage.per1000Col", { sev: mqmSeverities[s] })}
                          </th>
                        ))}
                        <th className="py-1 font-normal">
                          {t("qaPage.totalCol")}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(mqmQuery.data.by_category).map(
                        ([cat, count]) => (
                          <tr key={cat} className="border-t border-slate-800">
                            <td className="py-1 pr-2">
                              {mqmCategories[cat] ?? cat}
                            </td>
                            {Object.keys(mqmSeverities).map((s) => (
                              <td key={s} className="py-1 pr-2">
                                {mqmQuery.data.by_category_severity[cat]?.[s] ??
                                  0}
                              </td>
                            ))}
                            <td className="py-1">{count}</td>
                          </tr>
                        )
                      )}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
