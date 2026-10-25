"use client";

/**
 * Pagina Export (PRD §11.1, §15.4, §13.1, §19.2 / F4, task t_35aa4508).
 *
 * Opzioni + preview conteggio:
 *   - Formato: DOCX editoriale (prioritario, §19.2), EPUB, HTML;
 *   - Include bozze (§15.4) — quando ci sono bozze il watermark `[BOZZA]` è
 *     forzato (§13.1) e la pagina lo segnala esplicitamente;
 *   - Preview del piano (§15.4): i conteggi verificabili (totale / approvati /
 *     bozze), la lista dei capitoli e il manifest completo (versione progetto,
 *     timestamp, modelli, snapshot glossario/TM) vengono mostrati PRIMA del
 *     download;
 *   - Download del file (il byte passa solo origin→backend→origin, §13.1) e
 *     storico degli snapshot di export generati (§15.4 / audit).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import { ErrorNote, Loading, formatDateTime } from "@/components/ui";
import {
  getExportPlan,
  listExportSnapshots,
  listProjects,
  runExport,
  type ExportOptions,
  type ExportPlanResponse,
} from "@/lib/api";
import { QUERY_KEY_PROJECTS } from "@/lib/constants";
import { useI18n } from "@/lib/i18n";

const FORMAT_OPTS: [ExportOptions["format"], string][] = [
  ["docx", "DOCX"],
  ["epub", "EPUB"],
  ["pdf", "PDF"],
  ["html", "HTML"],
];

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export default function ExportPage() {
  const queryClient = useQueryClient();
  const { locale, t } = useI18n();
  const hints: Record<string, string> = {
    docx: t("exportPage.formats.docx"),
    epub: t("exportPage.formats.epub"),
    pdf: t("exportPage.formats.pdf"),
    html: t("exportPage.formats.html"),
  };

  const projectsQuery = useQuery({
    queryKey: [QUERY_KEY_PROJECTS],
    queryFn: listProjects,
  });
  const [projectId, setProjectId] = useState<string | null>(null);
  useEffect(() => {
    if (!projectId && projectsQuery.data?.length) {
      setProjectId(projectsQuery.data[0].id);
    }
  }, [projectsQuery.data, projectId]);

  const project =
    projectsQuery.data?.find((p) => p.id === projectId) ?? null;

  // --- opzioni (§15.4 / §13.1) ---
  const [format, setFormat] = useState<ExportOptions["format"]>("docx");
  const [includeDrafts, setIncludeDrafts] = useState(false);
  const [watermark, setWatermark] = useState(false);
  // Solo PDF: piedipagina "Titolo · N" (2026-10-01, flag utente).
  const [pageNumbers, setPageNumbers] = useState(true);
  // Clone della struttura originale (2026-10-01): font/allineamenti/
  // immagini/pagine dell'originale, con rapporto pagine impostabile.
  const [cloneStructure, setCloneStructure] = useState(false);
  const [pageRatio, setPageRatio] = useState(1.0);

  const options: ExportOptions = {
    format,
    include_drafts: includeDrafts,
    watermark,
    page_numbers: pageNumbers,
    clone_structure: cloneStructure,
    page_ratio: pageRatio,
  };

  // --- preview del piano (§15.4): conteggi + manifest PRIMA del download ---
  const planQuery = useQuery({
    queryKey: ["export-plan", projectId, format, includeDrafts, watermark],
    queryFn: () =>
      getExportPlan(projectId as string, options),
    enabled: Boolean(projectId),
    // La preview non deve bloccare la pagina se il progetto non è ancora
    // esportabile (nessun segmento): il messaggio di errore è il preview.
  });

  // --- storico snapshot export (§15.4 / audit) ---
  const snapshotsQuery = useQuery({
    queryKey: ["export-snapshots", projectId],
    queryFn: () => listExportSnapshots(projectId as string),
    enabled: Boolean(projectId),
  });

  // --- download (§11.1) ---
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const exportMutation = useMutation({
    mutationFn: () =>
      runExport(projectId as string, options),
    onSuccess: ({ blob, filename, snapshotId }) => {
      downloadBlob(blob, filename);
      setNotice(
        t("exportPage.downloaded", { format: format.toUpperCase() }) +
          (snapshotId
            ? t("exportPage.snapshotRegistered", { id: snapshotId.slice(0, 8) })
            : "")
      );
      setError(null);
      queryClient.invalidateQueries({
        queryKey: ["export-snapshots", projectId],
      });
      queryClient.invalidateQueries({
        queryKey: ["export-plan", projectId],
      });
    },
    onError: (e: Error) => {
      setError(e.message || t("exportPage.exportFailed"));
    },
  });

  const handleExport = useCallback(() => {
    setNotice(null);
    setError(null);
    exportMutation.mutate();
  }, [exportMutation]);

  if (!projectId) {
    return (
      <div className="space-y-3">
        <h1 className="text-xl font-semibold">{t("exportPage.title")}</h1>
        {projectsQuery.isLoading ? (
          <Loading label={t("projects.loading")} />
        ) : (
          <ErrorNote message={t("preview.noProjects")} />
        )}
      </div>
    );
  }

  const plan = planQuery.data as ExportPlanResponse | undefined;
  const counts = plan?.counts;
  const hasDrafts = (counts?.drafts ?? 0) > 0;
  // §15.4: se le bozze vengono spedite il watermark è obbligatorio.
  const willWatermark = includeDrafts && hasDrafts;
  // Il download è disabilitato se il piano non è valido (es. zero approvati
  // e senza watermark) — il preview mostra il motivo.
  const exportBlocked =
    !plan ||
    plan.selected_segments === 0 ||
    (includeDrafts && hasDrafts && !watermark);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">{t("exportPage.title")}</h1>
        <select
          value={projectId}
          onChange={(e) => {
            setProjectId(e.target.value);
            setNotice(null);
            setError(null);
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
        {project && (
          <span className="text-xs text-slate-500">
            {project.source_language} → {project.target_language} ·{" "}
            {project.genre_profile}
          </span>
        )}
      </div>

      {notice && (
        <div className="rounded-md border border-emerald-800 bg-emerald-950/40 px-4 py-2 text-sm text-emerald-300">
          {notice}
        </div>
      )}
      {error && <ErrorNote message={error} />}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* SINISTRA: opzioni (§11.1) */}
        <div className="rounded-md border border-slate-800 bg-slate-900/40 p-4">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            {t("exportPage.optionsTitle")}
          </h2>

          <div className="mt-3 space-y-2">
            <p className="text-xs text-slate-500">{t("exportPage.formatHint")}</p>
            {FORMAT_OPTS.map(([value, label]) => (
              <label
                key={value}
                className={`flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 ${
                  format === value
                    ? "border-indigo-500/60 bg-indigo-500/10"
                    : "border-slate-800 hover:bg-slate-800/40"
                }`}
              >
                <input
                  type="radio"
                  name="format"
                  checked={format === value}
                  onChange={() => setFormat(value)}
                  className="accent-indigo-500"
                />
                <span className="text-sm font-medium text-slate-100">
                  {label}
                </span>
                <span className="ml-auto text-xs text-slate-500">
                  {hints[value]}
                </span>
              </label>
            ))}
          </div>

          <div className="mt-4 space-y-3 border-t border-slate-800 pt-4">
            <label className="flex items-start gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={includeDrafts}
                onChange={(e) => setIncludeDrafts(e.target.checked)}
                className="mt-0.5 accent-indigo-500"
              />
              <span>
                {t("exportPage.includeDraftsLabel")}
                <span className="mt-0.5 block text-xs text-slate-500">
                  {t("exportPage.includeDraftsHint")}
                </span>
              </span>
            </label>

            <label
              className={`flex items-start gap-2 text-sm ${
                watermark ? "text-slate-300" : "text-slate-500"
              }`}
            >
              <input
                type="checkbox"
                checked={watermark}
                disabled={!includeDrafts || !hasDrafts}
                onChange={(e) => setWatermark(e.target.checked)}
                className="mt-0.5 accent-amber-500"
              />
              <span>
                {t("exportPage.watermarkLabel")}
                <span className="mt-0.5 block text-xs text-slate-500">
                  {willWatermark
                    ? t("exportPage.watermarkRequired")
                    : t("exportPage.watermarkOptional")}
                </span>
              </span>
            </label>

            {format === "pdf" && (
              <label
                className={`flex items-start gap-2 text-sm ${
                  pageNumbers ? "text-slate-300" : "text-slate-500"
                }`}
              >
                <input
                  type="checkbox"
                  checked={pageNumbers}
                  onChange={(e) => setPageNumbers(e.target.checked)}
                  className="mt-0.5 accent-indigo-500"
                />
                <span>
                  {t("exportPage.pageNumbersLabel")}
                  <span className="mt-0.5 block text-xs text-slate-500">
                    {t("exportPage.pageNumbersHint")}
                  </span>
                </span>
              </label>
            )}

            <label
              className={`flex items-start gap-2 text-sm ${
                cloneStructure ? "text-slate-300" : "text-slate-500"
              }`}
            >
              <input
                type="checkbox"
                checked={cloneStructure}
                onChange={(e) => setCloneStructure(e.target.checked)}
                className="mt-0.5 accent-indigo-500"
              />
              <span>
                {t("exportPage.cloneLabel")}
                <span className="mt-0.5 block text-xs text-slate-500">
                  {t("exportPage.cloneHint")}
                </span>
              </span>
            </label>

            {cloneStructure && format === "pdf" && (
              <label className="flex items-center gap-2 text-sm text-slate-400">
                {t("exportPage.pageRatioLabel")}
                <input
                  type="number"
                  min={0.5}
                  max={2}
                  step={0.05}
                  value={pageRatio}
                  onChange={(e) => setPageRatio(Number(e.target.value) || 1)}
                  className="w-20 rounded bg-slate-800 px-2 py-1 text-white"
                />
                <span className="text-xs text-slate-500">
                  {t("exportPage.pageRatioHint")}
                </span>
              </label>
            )}
          </div>

          <button
            onClick={handleExport}
            disabled={exportBlocked || exportMutation.isPending}
            className="mt-4 w-full rounded-md bg-indigo-600 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {exportMutation.isPending
              ? t("exportPage.generating")
              : t("exportPage.generate", { format: format.toUpperCase() })}
          </button>
          {exportBlocked && !exportMutation.isPending && (
            <p className="mt-2 text-xs text-slate-500">
              {t("exportPage.blocked")}
            </p>
          )}
        </div>

        {/* CENTRO: preview conteggio + manifest (§15.4) */}
        <div className="rounded-md border border-slate-800 bg-slate-900/40 p-4">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            {t("exportPage.previewTitle")}
          </h2>

          {planQuery.isLoading && <Loading label={t("exportPage.planLoading")} />}
          {planQuery.isError && (
            <ErrorNote
              message={
                (planQuery.error as Error)?.message ||
                t("exportPage.planError")
              }
            />
          )}
          {plan && (
            <div className="mt-3 space-y-4">
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="rounded-md bg-slate-800/60 py-2">
                  <div className="text-lg font-semibold text-slate-100">
                    {counts?.total_segments ?? 0}
                  </div>
                  <div className="text-xs text-slate-500">
                    {t("exportPage.total")}
                  </div>
                </div>
                <div className="rounded-md bg-emerald-500/10 py-2">
                  <div className="text-lg font-semibold text-emerald-300">
                    {counts?.approved ?? 0}
                  </div>
                  <div className="text-xs text-slate-500">
                    {t("exportPage.approvedCount")}
                  </div>
                </div>
                <div className="rounded-md bg-amber-500/10 py-2">
                  <div className="text-lg font-semibold text-amber-300">
                    {counts?.drafts ?? 0}
                  </div>
                  <div className="text-xs text-slate-500">
                    {t("exportPage.draftsCount")}
                  </div>
                </div>
              </div>

              <div className="rounded-md border border-slate-800 bg-slate-950/40 px-3 py-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-slate-500">{t("exportPage.toSend")}</span>
                  <span className="font-medium text-slate-100">
                    {plan.selected_segments}
                  </span>
                </div>
                <div className="mt-1 flex justify-between">
                  <span className="text-slate-500">{t("exportPage.choice")}</span>
                  <span className="text-slate-300">
                    {plan.include_drafts
                      ? t("exportPage.choiceBoth")
                      : t("exportPage.choiceApproved")}
                    {plan.watermark ? t("exportPage.watermarkMark") : ""}
                  </span>
                </div>
              </div>

              {willWatermark && !watermark && (
                <ErrorNote message={t("exportPage.watermarkNeeded")} />
              )}

              {plan.chapters.length > 0 && (
                <div>
                  <p className="text-xs text-slate-500">
                    {t("exportPage.chapters", { n: plan.chapters.length })}
                  </p>
                  <ul className="mt-1 grid grid-cols-1 gap-1 text-sm text-slate-300">
                    {plan.chapters.map((c, i) => (
                      <li key={`${c}-${i}`} className="truncate">
                        {c}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>

        {/* DESTRA: manifest (§15.4) + storico snapshot */}
        <div className="space-y-4">
          <div className="rounded-md border border-slate-800 bg-slate-900/40 p-4">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              Manifest (§15.4)
            </h2>
            {plan?.manifest ? (
              <dl className="mt-3 space-y-1.5 text-sm">
                {[
                  [t("exportPage.manifest.version"), plan.manifest.version],
                  [t("exportPage.manifest.generated"), plan.manifest.generated_at],
                  [
                    t("exportPage.manifest.languages"),
                    `${plan.manifest.source_language} → ${plan.manifest.target_language}`,
                  ],
                  [t("exportPage.manifest.profile"), plan.manifest.genre_profile],
                  [t("exportPage.manifest.status"), plan.manifest.project_status],
                  [
                    t("exportPage.manifest.models"),
                    plan.manifest.models.length
                      ? plan.manifest.models.join(", ")
                      : t("exportPage.manifest.modelsUnset"),
                  ],
                  [
                    t("exportPage.manifest.glossary"),
                    plan.manifest.glossary_snapshot_id
                      ? plan.manifest.glossary_snapshot_id.slice(0, 8) + "…"
                      : "—",
                  ],
                  [
                    t("exportPage.manifest.tm"),
                    plan.manifest.tm_snapshot_id
                      ? plan.manifest.tm_snapshot_id.slice(0, 8) + "…"
                      : "—",
                  ],
                ].map(([k, v]) => (
                  <div
                    key={k as string}
                    className="flex justify-between gap-2"
                  >
                    <dt className="text-slate-500">{k}</dt>
                    <dd className="text-right font-mono text-xs text-slate-300">
                      {v}
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="mt-3 text-xs text-slate-500">
                {t("exportPage.manifestPending")}
              </p>
            )}
          </div>

          <div className="rounded-md border border-slate-800 bg-slate-900/40 p-4">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              {t("exportPage.historyTitle")}
            </h2>
            {snapshotsQuery.isLoading && (
              <Loading label={t("common.loading")} />
            )}
            {!snapshotsQuery.isLoading &&
              (!snapshotsQuery.data ||
                snapshotsQuery.data.snapshots.length === 0) && (
                <p className="mt-3 text-xs text-slate-500">
                  {t("exportPage.historyEmpty")}
                </p>
              )}
            <ul className="mt-3 space-y-2">
              {snapshotsQuery.data?.snapshots.map((s) => (
                <li
                  key={s.id}
                  className="flex items-center justify-between rounded-md bg-slate-800/40 px-3 py-2 text-sm"
                >
                  <span className="font-medium text-slate-200">
                    {(s.format ?? "?").toUpperCase()}
                    {s.include_drafts ? ` ${t("exportPage.withDrafts")}` : ""}
                    {s.watermark ? t("exportPage.draftMark") : ""}
                  </span>
                  <span className="text-xs text-slate-500">
                    {t("exportPage.itemsCount", { n: s.item_count })} ·{" "}
                    {formatDateTime(s.created_at, locale)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
