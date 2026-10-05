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
import {
  JOB_STATUS_LABEL_IT,
  JOB_TYPE_LABEL_IT,
  QUERY_KEY_JOBS,
} from "@/lib/constants";

export default function ProjectDetailPage() {
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
    return <ErrorNote message="Identificativo progetto mancante." />;
  }

  if (projectQuery.isLoading || jobsQuery.isLoading) {
    return <Loading label="Caricamento progetto…" />;
  }

  if (projectQuery.isError) {
    return (
      <ErrorNote message="Progetto non trovato o backend non raggiungibile." />
    );
  }

  if (jobsQuery.isError) {
    return (
      <ErrorNote message="Impossibile caricare i job del progetto dal backend." />
    );
  }

  const project = projectQuery.data;

  if (!project) {
    return <Loading label="Caricamento progetto…" />;
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
          ← Progetti
        </Link>
      </p>
      <div className="mt-1 flex items-center gap-3">
        <h1 className="text-2xl font-bold text-white">{project.title}</h1>
        <StatusBadge status={project.status} />
      </div>
      <p className="mt-1 text-xs text-slate-500">
        {project.source_language.toUpperCase()} →{" "}
        {project.target_language.toUpperCase()} · profilo{" "}
        {project.genre_profile} · creato il{" "}
        {formatDateTime(project.created_at)} ·{" "}
        {project.copyright_confirmed
          ? "copyright confermato"
          : "copyright da confermare"}
      </p>

      <section className="mt-6" aria-labelledby="job-attivi">
        <h2 id="job-attivi" className="text-sm font-semibold text-white">
          Job attivi
        </h2>
        {activeJobs.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">Nessun job attivo.</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {activeJobs.map((job) => (
              <li key={String(job.id)} className="card text-sm">
                <p className="font-medium text-white">
                  {JOB_TYPE_LABEL_IT[job.job_type] ?? job.job_type}{" "}
                  <span className="ml-1 text-xs font-normal text-slate-400">
                    ({JOB_STATUS_LABEL_IT[job.status] ?? job.status})
                  </span>
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mt-6" aria-labelledby="job-storico">
        <h2 id="job-storico" className="text-sm font-semibold text-white">
          Storico job
        </h2>
        {pastJobs.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">Nessun job completato.</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {pastJobs.map((job) => (
              <li key={String(job.id)} className="card text-sm">
                <div className="flex items-center justify-between gap-3">
                  <p className="font-medium text-white">
                    {JOB_TYPE_LABEL_IT[job.job_type] ?? job.job_type}
                  </p>
                  <span
                    className={`badge ${
                      job.status === "completed"
                        ? "bg-emerald-500/20 text-emerald-300"
                        : "bg-red-500/20 text-red-300"
                    }`}
                  >
                    {JOB_STATUS_LABEL_IT[job.status] ?? job.status}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  {formatDateTime(job.created_at)}
                  {job.error ? ` · errore: ${job.error}` : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
