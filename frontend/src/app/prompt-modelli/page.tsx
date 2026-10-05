"use client";

/**
 * Prompt e modelli (§8.2 / §8.1 / §11.1 / §15.3) — task t_29ec876e.
 *
 * Pagina STATICa della nav principale: il progetto NON arriva da `useParams`
 * (che su una rotta statica è sempre vuoto) ma da un selettore client-side
 * popolato con `listProjects()` — stesso pattern delle pagine QA ed Export.
 *
 * Contenuti:
 *  - matrice delle capability di LLM Gateway (§8.1, popolata dinamicamente
 *    da /api/v1/gateway/models: context window, max output, JSON, streaming,
 *    reasoning, seed, stato);
 *  - DUE selettori obbligatori distinti (§8.2): modello analisi/testo e
 *    modello traduzione, con le capability del modello scelto;
 *  - impostazioni avanzate per modello (§8.2 / §16.3) tramite
 *    {@link AdvancedSettingsPanel} (temperatura, top_p, seed, reasoning
 *    on/off + budget, timeout, retry, max output, template prompt);
 *  - preview del token budget (§5.4 / §15.3) sul capitolo SELEZIONATO tramite
 *    {@link BudgetPreview}: blocchi stimati e verifica input+output contro la
 *    context window, con avviso che blocca l'avvio del batch se insufficiente.
 *
 * Le modifiche sono salvate con `updateProject()` (PATCH /api/v1/projects/{id})
 * e persistite in `translation_model_id` / `text_model_id` / `model_settings`
 * del progetto; alla selezione del progetto i valori salvati pre-popolano il
 * form (AC3).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { AdvancedSettingsPanel } from "@/components/advanced-settings-panel";
import { BudgetPreview } from "@/components/budget-preview";
import { ModelSelector } from "@/components/model-selector";
import { ErrorNote, Loading } from "@/components/ui";
import {
  ApiError,
  getChapterPlan,
  getStructure,
  listGatewayModels,
  listProjects,
  updateProject,
} from "@/lib/api";
import { QUERY_KEY_MODELS, QUERY_KEY_PROJECTS } from "@/lib/constants";
import type { ModelSettings, GatewayModel } from "@/lib/api-types";

const DEFAULT_SETTINGS: ModelSettings = {
  translation: {},
  text: {},
};

/** Combina i due selettori e le impostazioni avanzate in un payload. */
function buildPayload(
  translationModelId: string | null,
  textModelId: string | null,
  settings: ModelSettings
) {
  return {
    translation_model_id: translationModelId || null,
    text_model_id: textModelId || null,
    model_settings: settings,
  };
}

/** Riga capability sintetica per un modello scelto in un selettore. */
function SelectedCapability({ model }: { model: GatewayModel }) {
  const ctx =
    model.context_window != null
      ? `${(model.context_window / 1000).toFixed(0)}k ctx`
      : "ctx ?";
  const out =
    model.max_output != null
      ? `${(model.max_output / 1000).toFixed(1)}k out`
      : "out ?";
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-400">
      <span className="rounded bg-slate-800 px-1.5 py-0.5">{ctx}</span>
      <span className="rounded bg-slate-800 px-1.5 py-0.5">{out}</span>
      <span
        className={`rounded px-1.5 py-0.5 ${
          model.status === "available"
            ? "bg-emerald-500/20 text-emerald-300"
            : "bg-slate-700 text-slate-300"
        }`}
      >
        {model.status === "available" ? "attivo" : model.status === "offline" ? "offline" : "degradato"}
      </span>
    </div>
  );
}

export default function PromptModelsPage() {
  const queryClient = useQueryClient();

  // --- progetto: rotta statica → risolto lato client (pattern QA/Export) ---
  const projectsQuery = useQuery({
    queryKey: [...QUERY_KEY_PROJECTS],
    queryFn: listProjects,
  });
  const [projectId, setProjectId] = useState<string | null>(null);
  useEffect(() => {
    if (!projectId && projectsQuery.data?.length) {
      setProjectId(projectsQuery.data[0].id);
    }
  }, [projectsQuery.data, projectId]);
  const project = projectsQuery.data?.find((p) => p.id === projectId) ?? null;

  // --- matrice capability (§8.1) ---
  const modelsQuery = useQuery({
    queryKey: QUERY_KEY_MODELS,
    queryFn: listGatewayModels,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  // --- capitolo selezionato (§8.2: "dato il capitolo selezionato") ---
  const structureQuery = useQuery({
    queryKey: ["struttura", projectId],
    queryFn: () => getStructure(projectId as string),
    enabled: Boolean(projectId),
    retry: false,
  });
  const chapters = useMemo(
    () =>
      (structureQuery.data?.nodes ?? [])
        .filter((n) => n.kind === "chapter")
        .map((n) => ({ id: n.node_id, label: n.normalized_title ?? n.node_id }))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [structureQuery.data]
  );
  const [chapterId, setChapterId] = useState<string>("");

  // --- piano blocchi del capitolo selezionato (§5.4, per il budget) ---
  const planQuery = useQuery({
    queryKey: ["piano", projectId, chapterId],
    queryFn: () => getChapterPlan(projectId as string, chapterId),
    enabled: Boolean(projectId && chapterId),
    retry: false,
  });

  // --- salvataggio (AC3: persistenza per progetto) ---
  const saveMutation = useMutation({
    mutationFn: async (payload: ReturnType<typeof buildPayload>) => {
      if (!projectId) throw new Error("nessun progetto selezionato");
      return updateProject(projectId, payload);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [...QUERY_KEY_PROJECTS] });
      queryClient.invalidateQueries({ queryKey: ["piano", projectId] });
    },
  });

  // Stato del form: i due selettori + le impostazioni avanzate.
  const [translationModelId, setTranslationModelId] = useState<string | null>(null);
  const [textModelId, setTextModelId] = useState<string | null>(null);
  const [settings, setSettings] = useState<ModelSettings>(DEFAULT_SETTINGS);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  // Pre-popola il form dai valori persistiti del progetto (AC3): solo quando
  // cambia il progetto e solo se l'utente non ha già modifiche in corso.
  const projectIdForReset = project?.id;
  useEffect(() => {
    if (!project) return;
    setTranslationModelId(project.translation_model_id ?? null);
    setTextModelId(project.text_model_id ?? null);
    setSettings({
      translation: project.model_settings?.translation ?? {},
      text: project.model_settings?.text ?? {},
    });
    setDirty(false);
    setSavedAt(null);
    // Solo al cambio progetto: usare `project` intero reintrodurrebbe il
    // reset a ogni poll dei progetti.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectIdForReset]);

  const models = (modelsQuery.data?.models ?? []).sort((a, b) =>
    (a.display_name ?? a.id).localeCompare(b.display_name ?? b.id)
  );
  const translationModel: GatewayModel | null =
    models.find((m) => m.id === translationModelId) ?? null;
  const textModel: GatewayModel | null =
    models.find((m) => m.id === textModelId) ?? null;

  // max_output effettivo per il budget: quello del pannello traduzione,
  // altrimenti la riserva default (2048) gestita da checkChapterBudget.
  const maxOutput = settings.translation?.max_output ?? null;

  const save = () => {
    if (!projectId) return;
    setSavedAt(null);
    saveMutation.mutate(buildPayload(translationModelId, textModelId, settings), {
      onSuccess: () => {
        setSavedAt(new Date().toLocaleTimeString("it-IT"));
        setDirty(false);
      },
    });
  };

  const markDirty = () => setDirty(true);

  if (projectsQuery.isLoading) {
    return <Loading label="Caricamento progetti…" />;
  }

  if (!projectId) {
    return (
      <div className="space-y-3">
        <h1 className="text-2xl font-bold text-white">Prompt e modelli</h1>
        <ErrorNote message="Nessun progetto disponibile: creane uno dalla pagina Progetti." />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-white">Prompt e modelli</h1>
        <select
          value={projectId}
          onChange={(e) => {
            setProjectId(e.target.value);
          }}
          className="rounded-md border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-white"
          aria-label="Progetto"
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
      <p className="mt-1 text-sm text-slate-400">
        Scegli i due modelli (§8.2), regola le impostazioni avanzate e verifica
        il budget token del capitolo selezionato (§15.3).
      </p>

      {/* Matrice delle capability (§8.1) */}
      <section className="mt-6" aria-labelledby="matrice-title">
        <h2 id="matrice-title" className="mb-2 text-sm font-semibold text-white">
          Matrice delle capability di LLM Gateway
        </h2>
        {modelsQuery.isLoading ? (
          <Loading label="Caricamento modelli…" />
        ) : modelsQuery.isError ? (
          <ErrorNote message="Impossibile contattare LLM Gateway: assicurati che sia attivo." />
        ) : models.length === 0 ? (
          <p className="text-sm text-slate-400">
            Nessun modello esposto dal proxy: verifica che llama-swap abbia
            almeno un modello registrato.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {models.map((m) => (
              <div key={m.id} className="card space-y-1.5">
                <div className="flex items-center justify-between">
                  <span className="truncate font-medium text-white" title={m.id}>
                    {m.display_name ?? m.id}
                  </span>
                  <span
                    className={`badge ${
                      m.status === "available"
                        ? "bg-emerald-500/20 text-emerald-300"
                        : "bg-slate-700 text-slate-300"
                    }`}
                  >
                    {m.status === "available" ? "attivo" : m.status === "offline" ? "offline" : "degradato"}
                  </span>
                </div>
                <p className="text-[11px] text-slate-500">{m.provider}</p>
                <div className="flex flex-wrap gap-1.5">
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">
                    {m.context_window != null
                      ? `${(m.context_window / 1000).toFixed(0)}k ctx`
                      : "ctx ?"}
                  </span>
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">
                    {m.max_output != null
                      ? `${(m.max_output / 1000).toFixed(1)}k out`
                      : "out ?"}
                  </span>
                  {!m.supports_json_schema && (
                    <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                      no JSON
                    </span>
                  )}
                  {!m.supports_reasoning && (
                    <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                      no reasoning
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Selettori (§8.2): DUE campi distinti e obbligatori */}
      <section className="mt-6" aria-labelledby="selettori-title">
        <h2 id="selettori-title" className="mb-2 text-sm font-semibold text-white">
          Selezione modelli
        </h2>
        <div className="space-y-4">
          <div>
            <ModelSelector
              label="Modello di analisi/testo"
              name="text-model"
              value={textModelId ?? ""}
              placeholder="Seleziona un modello di analisi/testo"
              models={models}
              onChange={(id) => {
                setTextModelId(id || null);
                markDirty();
              }}
            />
            {textModel && <SelectedCapability model={textModel} />}
          </div>
          <div>
            <ModelSelector
              label="Modello di traduzione"
              name="translation-model"
              value={translationModelId ?? ""}
              placeholder="Seleziona un modello di traduzione"
              models={models}
              onChange={(id) => {
                setTranslationModelId(id || null);
                markDirty();
              }}
            />
            {translationModel && <SelectedCapability model={translationModel} />}
          </div>
        </div>
      </section>

      {/* Impostazioni avanzate (§8.2 / §16.3) */}
      <section className="mt-6" aria-labelledby="avanzate-title">
        <h2 id="avanzate-title" className="mb-2 text-sm font-semibold text-white">
          Impostazioni avanzate
        </h2>
        <AdvancedSettingsPanel
          settings={settings}
          onChange={(next) => {
            setSettings(next);
            markDirty();
          }}
        />
      </section>

      {/* Salvataggio (AC3) */}
      <section className="mt-6 flex items-center gap-4" aria-label="Salvataggio">
        <button
          type="button"
          className="btn-primary"
          disabled={saveMutation.isPending}
          onClick={save}
        >
          {saveMutation.isPending ? "Salvataggio…" : "Salva impostazioni"}
        </button>
        {savedAt && !saveMutation.isError && (
          <span className="text-sm text-emerald-300">
            Impostazioni salvate ({savedAt})
          </span>
        )}
        {saveMutation.isError && (
          <ErrorNote
            message={
              saveMutation.error instanceof ApiError
                ? saveMutation.error.detail
                : "Impossibile salvare le impostazioni."
            }
          />
        )}
        {dirty && !savedAt && !saveMutation.isError && (
          <span className="text-xs text-amber-300">Modifiche non salvate</span>
        )}
      </section>

      {/* Preview token budget (§5.4 / §15.3) sul capitolo selezionato */}
      <section className="mt-6" aria-labelledby="budget-title">
        <h2 id="budget-title" className="mb-2 text-sm font-semibold text-white">
          Preview token budget
        </h2>
        <div className="space-y-4">
          <div className="w-full max-w-xs">
            <label htmlFor="budget-chapter" className="label">
              Capitolo
            </label>
            <select
              id="budget-chapter"
              className="input"
              value={chapterId}
              onChange={(e) => setChapterId(e.target.value)}
            >
              <option value="">Seleziona un capitolo…</option>
              {chapters.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
            {structureQuery.data &&
              chapters.length === 0 &&
              structureQuery.data.nodes.length > 0 && (
                <p className="mt-1 text-[11px] text-amber-300">
                  Nessun nodo di tipo capitolo nella struttura del progetto.
                </p>
              )}
          </div>
          {chapterId ? (
            planQuery.isError ? (
              <ErrorNote message="Piano non disponibile per questo capitolo: esegui prima la segmentazione." />
            ) : (
              <BudgetPreview
                plan={planQuery.data ?? null}
                model={translationModel}
                maxOutput={maxOutput}
              />
            )
          ) : (
            <p className="text-sm text-slate-400">
              Seleziona un capitolo per stimare i blocchi e verificare il
              budget token (§15.3).
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
