/** Componenti UI condivisi: badge di stato, stati di caricamento/errore. */

import { QUERY_KEY_PROJECTS } from "@/lib/constants";

const STATUS_CLASS: Record<string, string> = {
  DRAFT: "bg-slate-700 text-slate-200",
  IMPORTING: "bg-amber-500/20 text-amber-300",
  PARSED: "bg-sky-500/20 text-sky-300",
  STRUCTURE_REVIEW: "bg-violet-500/20 text-violet-300",
  ENTITY_REVIEW: "bg-fuchsia-500/20 text-fuchsia-300",
  READY_FOR_TRANSLATION: "bg-teal-500/20 text-teal-300",
  TRANSLATING: "bg-indigo-500/20 text-indigo-300",
  QA_REVIEW: "bg-orange-500/20 text-orange-300",
  APPROVED: "bg-emerald-500/20 text-emerald-300",
  EXPORTED: "bg-emerald-700/30 text-emerald-200",
  DEFAULT: "bg-slate-700 text-slate-200",
};

export const STATUS_LABEL_IT: Record<string, string> = {
  DRAFT: "Bozza",
  IMPORTING: "Importazione",
  PARSED: "Analizzato",
  STRUCTURE_REVIEW: "Revisione struttura",
  ENTITY_REVIEW: "Revisione entità",
  READY_FOR_TRANSLATION: "Pronto per la traduzione",
  TRANSLATING: "In traduzione",
  QA_REVIEW: "Revisione QA",
  APPROVED: "Approvato",
  EXPORTED: "Esportato",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge ${STATUS_CLASS[status] ?? STATUS_CLASS.DEFAULT}`}>
      {STATUS_LABEL_IT[status] ?? status}
    </span>
  );
}

export function Loading({ label = "Caricamento…" }: { label?: string }) {
  return (
    <p className="py-8 text-center text-sm text-slate-400" role="status">
      {label}
    </p>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div
      className="rounded-md border border-red-800 bg-red-950/50 px-4 py-3 text-sm text-red-300"
      role="alert"
    >
      {message}
    </div>
  );
}

export { QUERY_KEY_PROJECTS };

/** Formatta una data ISO in italiano. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("it-IT", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(iso));
}

/** Formatta una dimensione in byte. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = bytes;
  let unit = "B";
  for (const next of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = next;
  }
  return `${value.toFixed(1)} ${unit}`;
}
