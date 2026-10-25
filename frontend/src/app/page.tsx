"use client";

/**
 * Dashboard (PRD §11.1, §11.3): progetti con stato (macchina a stati §5.1),
 * progresso, job attivi e modelli disponibili. Dati reali dall'API T09.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { Loading, ErrorNote, StatusBadge, formatDateTime } from "@/components/ui";
import {
  deleteProject,
  listGatewayModels,
  listProjects,
  listProjectJobs,
} from "@/lib/api";
import {
  QUERY_KEY_JOBS,
  QUERY_KEY_MODELS,
  QUERY_KEY_PROJECTS,
} from "@/lib/constants";
import type { Job, Project } from "@/lib/api-types";
import { useI18n } from "@/lib/i18n";

/** Estrae il numero di pagine totali dai result dei job di parse. */
function pagesTotal(jobs: Job[]): number | null {
  for (const job of jobs) {
    const result = job.result as
      | { total_pages?: number; page_count?: number }
      | null;
    if (result) {
      if (typeof result.total_pages === "number") return result.total_pages;
      if (typeof result.page_count === "number") return result.page_count;
    }
  }
  return null;
}

/** Estrae il progresso corrente (pagine elaborate) dai job di import. */
function pagesDone(jobs: Job[]): number | null {
  let done: number | null = null;
  for (const job of jobs) {
    const progress = (job.result as { progress?: { pages_done?: number } } | null)
      ?.progress;
    if (progress && typeof progress.pages_done === "number") {
      done = Math.max(done ?? 0, progress.pages_done);
    }
  }
  return done;
}

function ProjectCard({ project }: { project: Project }) {
  const { locale, t, dict } = useI18n();
  const jobTypeLabels = dict.enums.jobType as Record<string, string>;
  const jobStatusLabels = dict.enums.jobStatus as Record<string, string>;
  const jobsQuery = useQuery({
    queryKey: QUERY_KEY_JOBS(project.id),
    queryFn: () => listProjectJobs(project.id),
    staleTime: 3_000,
    refetchInterval: 5_000,
  });
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const deleteMutation = useMutation({
    mutationFn: () => deleteProject(project.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY_PROJECTS });
    },
  });

  const jobs = jobsQuery.data ?? [];
  const activeJobs = jobs.filter(
    (j) => j.status === "running" || j.status === "pending"
  );
  const total = pagesTotal(jobs);
  const done = pagesDone(jobs);
  const progress =
    total != null && done != null ? Math.min(100, (done / total) * 100) : null;

  return (
    <div className="card">
      <div className="flex items-start justify-between gap-3">
        <div>
          <Link
            href={`/progetti/${project.id}`}
            className="font-semibold text-white hover:text-indigo-300"
          >
            {project.title}
          </Link>
          <p className="mt-0.5 text-xs text-slate-400">
            {project.source_language.toUpperCase()} →{" "}
            {project.target_language.toUpperCase()} ·{" "}
            {t("dash.updatedOn", {
              date: formatDateTime(project.updated_at, locale),
            })}
          </p>
        </div>
        <div className="text-right">
          <StatusBadge status={project.status} />
          <p className="mt-1 text-xs text-slate-500">{project.genre_profile}</p>
        </div>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-300">
        <div>
          <dt className="text-slate-500">{t("dash.activeJobs")}</dt>
          <dd>{activeJobs.length}</dd>
        </div>
        <div>
          <dt className="text-slate-500">{t("dash.pages")}</dt>
          <dd>
            {done != null ? done : "—"}
            {total != null ? ` / ${total}` : ""}
          </dd>
        </div>
      </dl>

      {progress != null && (
        <div
          className="mt-3 h-2 w-full overflow-hidden rounded-full bg-slate-800"
          role="progressbar"
          aria-valuenow={Math.round(progress)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={t("dash.importProgress", { title: project.title })}
        >
          <div
            className="h-full rounded-full bg-indigo-500 transition-all"
            style={{ width: `${progress}%` }}
          />
        </div>
      )}

      {activeJobs.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs text-slate-400">
          {activeJobs.slice(0, 3).map((job) => (
            <li key={String(job.id)}>
              {jobTypeLabels[job.job_type] ?? job.job_type} ·{" "}
              {jobStatusLabels[job.status] ?? job.status}
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 border-t border-slate-800 pt-2 text-right">
        {confirming ? (
          <div className="flex items-center justify-end gap-2 text-xs">
            <span className="text-slate-400">
              {t("dash.deleteConfirm", { title: project.title })}
            </span>
            <button
              type="button"
              disabled={deleting}
              onClick={() => {
                setDeleting(true);
                deleteMutation.mutate(undefined, {
                  onSettled: () => setDeleting(false),
                });
              }}
              className="rounded-md bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-50"
            >
              {deleting ? t("common.deleting") : t("common.confirm")}
            </button>
            <button
              type="button"
              disabled={deleting}
              onClick={() => setConfirming(false)}
              className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"
            >
              {t("common.cancel")}
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:border-red-600 hover:text-red-400"
            aria-label={t("dash.deleteProject", { title: project.title })}
          >
            {t("common.delete")}
          </button>
        )}
      </div>
    </div>
  );
}

function ModelsCard() {
  const { t } = useI18n();
  const modelsQuery = useQuery({
    queryKey: QUERY_KEY_MODELS,
    queryFn: listGatewayModels,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const models = modelsQuery.data?.models ?? [];

  return (
    <section className="card" aria-labelledby="modelli-titolo">
      <h2 id="modelli-titolo" className="text-sm font-semibold text-white">
        {t("dash.modelsTitle")}
      </h2>
      <p className="mt-0.5 text-xs text-slate-400">{t("dash.modelsSubtitle")}</p>
      {modelsQuery.isLoading ? (
        <Loading label={t("dash.modelsLoading")} />
      ) : models.length === 0 ? (
        <p className="mt-3 text-xs text-slate-500">
          {t("dash.modelsUnavailable")}
        </p>
      ) : (
        <ul className="mt-3 space-y-1.5 text-xs">
          {models.slice(0, 8).map((model) => (
            <li key={model.id} className="flex items-center justify-between gap-2">
              <span className="truncate text-slate-300" title={model.id}>
                {model.display_name ?? model.id}
              </span>
              <span
                className={`badge ${
                  model.status === "available"
                    ? "bg-emerald-500/20 text-emerald-300"
                    : "bg-slate-700 text-slate-300"
                }`}
              >
                {model.status === "available"
                  ? t("dash.modelActive")
                  : t("dash.modelInactive")}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function DashboardPage() {
  const { t } = useI18n();
  const projectsQuery = useQuery({
    queryKey: QUERY_KEY_PROJECTS,
    queryFn: listProjects,
    refetchInterval: 10_000,
  });

  const projects = projectsQuery.data ?? [];

  if (projectsQuery.isLoading) {
    return <Loading label={t("dash.loadingProjects")} />;
  }

  if (projectsQuery.isError) {
    return <ErrorNote message={t("dash.backendDown")} />;
  }

  return (
    <div className="mx-auto max-w-6xl">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">{t("dash.title")}</h1>
          <p className="mt-1 text-sm text-slate-400">
            {projects.length === 0
              ? t("dash.emptyHint")
              : projects.length === 1
                ? t("dash.oneProject")
                : t("dash.manyProjects", { n: projects.length })}
          </p>
        </div>
        <Link href="/progetti/nuovo" className="btn-primary">
          {t("dash.newProject")}
        </Link>
      </header>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          {projects.length === 0 ? (
            <div className="card text-center text-sm text-slate-400">
              <p>
                {t("dash.emptyBody")}{" "}
                <Link
                  href="/progetti/nuovo"
                  className="text-indigo-300 underline"
                >
                  {t("dash.emptyLink")}
                </Link>{" "}
                {t("dash.emptyTail")}
              </p>
            </div>
          ) : (
            projects.map((project) => (
              <ProjectCard key={String(project.id)} project={project} />
            ))
          )}
        </div>
        <div className="space-y-4">
          <ModelsCard />
        </div>
      </div>
    </div>
  );
}
