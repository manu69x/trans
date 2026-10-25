"use client";

/**
 * Dettaglio progetto: scheda con metadati, stato §5.1, documenti caricati e
 * job (attivi e storico) con progresso. Punto di arrivo del wizard.
 */

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";

import {
  ErrorNote,
  Loading,
  StatusBadge,
  formatDateTime,
} from "@/components/ui";
import { getProject, listProjectJobs } from "@/lib/api";
import { QUERY_KEY_JOBS } from "@/lib/constants";
import { useI18n } from "@/lib/i18n";

export default function ProjectDetailPage() {
  const { locale, t, dict } = useI18n();
  const jobTypeLabels = dict.enums.jobType as Record<string, string>;
  const jobStatusLabels = dict.enums.jobStatus as Record<string, string>;
  const params = useParams<{ projectId: string }>();
  const projectId = params?.projectId;

  const projectQuery = useQuery({
    queryKey: ["progetto", projectId],
    queryFn: () => getProject(projectId as string),
    enabled: Boolean(projectId),
  });

  const jobsQuery = useQuery({
    queryKey: QUERY_KEY_JOBS(projectId ?? "_"),
    queryFn: () => listProjectJobs(projectId as string),
    enabled: Boolean(projectId),
    refetchInterval: 4_000,
  });

  if (!projectId) {
    return <ErrorNote message={t("projectDetail.missingId")} />;
  }

  if (projectQuery.isLoading || jobsQuery.isLoading) {
    return <Loading label={t("projectDetail.loading")} />;
  }

  if (projectQuery.isError) {
    return <ErrorNote message={t("projectDetail.notFound")} />;
  }

  if (jobsQuery.isError) {
    return <ErrorNote message={t("projectDetail.jobsLoadError")} />;
  }

  const project = projectQuery.data;

  if (!project) {
    return <Loading label={t("projectDetail.loading")} />;
  }

  const jobs = jobsQuery.data ?? [];
  const activeJobs = jobs.filter(
    (j) => j.status === "running" || j.status === "pending"
  );
  const pastJobs = jobs
    .filter((j) => j.status !== "running" && j.status !== "pending")
    .slice()
    .reverse();

  return (
    <div className="mx-auto max-w-4xl">
      <p className="text-xs text-slate-500">
        <Link href="/progetti" className="hover:text-slate-300">
          {t("projectDetail.backToProjects")}
        </Link>
      </p>
      <div className="mt-1 flex items-center gap-3">
        <h1 className="text-2xl font-bold text-white">{project.title}</h1>
        <StatusBadge status={project.status} />
      </div>
      <p className="mt-1 text-xs text-slate-500">
        {project.source_language.toUpperCase()} →{" "}
        {project.target_language.toUpperCase()} ·{" "}
        {t("projectDetail.profile", { genre: project.genre_profile })} ·{" "}
        {t("projects.createdOn", {
          date: formatDateTime(project.created_at, locale),
        })}{" "}
        ·{" "}
        {project.copyright_confirmed
          ? t("projectDetail.copyrightOk")
          : t("projectDetail.copyrightPending")}
      </p>

      <section className="mt-6" aria-labelledby="job-attivi">
        <h2 id="job-attivi" className="text-sm font-semibold text-white">
          {t("projectDetail.activeJobs")}
        </h2>
        {activeJobs.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">
            {t("projectDetail.noActiveJobs")}
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {activeJobs.map((job) => (
              <li key={String(job.id)} className="card text-sm">
                <p className="font-medium text-white">
                  {jobTypeLabels[job.job_type] ?? job.job_type}{" "}
                  <span className="ml-1 text-xs font-normal text-slate-400">
                    ({jobStatusLabels[job.status] ?? job.status})
                  </span>
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mt-6" aria-labelledby="job-storico">
        <h2 id="job-storico" className="text-sm font-semibold text-white">
          {t("projectDetail.jobHistory")}
        </h2>
        {pastJobs.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">
            {t("projectDetail.noPastJobs")}
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {pastJobs.map((job) => (
              <li key={String(job.id)} className="card text-sm">
                <div className="flex items-center justify-between gap-3">
                  <p className="font-medium text-white">
                    {jobTypeLabels[job.job_type] ?? job.job_type}
                  </p>
                  <span
                    className={`badge ${
                      job.status === "completed"
                        ? "bg-emerald-500/20 text-emerald-300"
                        : "bg-red-500/20 text-red-300"
                    }`}
                  >
                    {jobStatusLabels[job.status] ?? job.status}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  {formatDateTime(job.created_at, locale)}
                  {job.error
                    ? ` · ${t("projectDetail.jobError", { err: job.error })}`
                    : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
