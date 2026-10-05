"use client";

/** Elenco progetti (voce di nav "Progetti"). */

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import {
  ErrorNote,
  Loading,
  StatusBadge,
  formatDateTime,
} from "@/components/ui";
import { listProjects } from "@/lib/api";
import { QUERY_KEY_PROJECTS } from "@/lib/constants";

export default function ProjectsPage() {
  const projectsQuery = useQuery({
    queryKey: QUERY_KEY_PROJECTS,
    queryFn: listProjects,
    refetchInterval: 10_000,
  });

  const projects = projectsQuery.data ?? [];

  return (
    <div className="mx-auto max-w-4xl">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Progetti</h1>
        <Link href="/progetti/nuovo" className="btn-primary">
          Nuovo progetto
        </Link>
      </header>

      {projectsQuery.isLoading && <Loading label="Caricamento progetti…" />}
      {projectsQuery.isError && (
        <div className="mt-4">
          <ErrorNote message="Impossibile caricare i progetti dal backend." />
        </div>
      )}

      {projectsQuery.isSuccess && (
        <ul className="mt-6 divide-y divide-slate-800 rounded-lg border border-slate-800 bg-slate-900/40">
          {projects.length === 0 && (
            <li className="px-4 py-8 text-center text-sm text-slate-400">
              Nessun progetto presente.
            </li>
          )}
          {projects.map((project) => (
            <li key={String(project.id)} className="px-4 py-3">
              <Link
                href={`/progetti/${project.id}`}
                className="flex items-center justify-between gap-4 hover:bg-slate-800/40"
              >
                <div>
                  <p className="font-medium text-white">{project.title}</p>
                  <p className="text-xs text-slate-400">
                    {project.genre_profile} ·{" "}
                    {project.source_language.toUpperCase()} →{" "}
                    {project.target_language.toUpperCase()} · creato il{" "}
                    {formatDateTime(project.created_at)}
                  </p>
                </div>
                <StatusBadge status={project.status} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
