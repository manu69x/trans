/** Costanti condivise del frontend.
 *
 * Le etichette localizzate (stati job, MQM, entità, generi…) vivono nei
 * dizionari i18n (src/lib/dictionaries/it.ts + en.ts) sotto `enums`.
 * Qui restano solo le chiavi di cache TanStack Query e le classi CSS. */

/** Chiavi di cache TanStack Query. */
export const QUERY_KEY_PROJECTS = ["progetti"] as const;
export const QUERY_KEY_JOBS = (projectId: string) => [
  "lavori",
  projectId,
] as const;
export const QUERY_KEY_MODELS = ["modelli"] as const;

/** MQM — classe colore per gravità (§10.4). */
export const MQM_SEVERITY_CLASS: Record<string, string> = {
  minor: "bg-slate-700 text-slate-200",
  major: "bg-amber-500/20 text-amber-300",
  critical: "bg-red-500/20 text-red-300",
};
