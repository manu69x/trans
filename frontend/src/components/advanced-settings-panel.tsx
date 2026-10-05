/**
 * Pannelo impostazioni avanzate (§8.2 / PRD §16.3).
 *
 * Rendere tutti i parametri avanzati di un modello: temperatura, top_p,
 * seed, reasoning, budget reasoning, timeout, retry, max_output, prompt
 * template. Ogni controllo è opzionale e scrivibile nel {@link ModelSettings}.
 *
 * I campi non compilati restano `null`; la persistenza avviene via
 * `updateProject()` che serializza l'intero `model_settings`.
 */

import type { ModelSettings } from "@/lib/api-types";

const EMPTY_SETTINGS: ModelSettings = {
  translation: {},
  text: {},
};

function Field({
  htmlFor,
  label,
  hint,
  children,
}: {
  htmlFor: string;
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={htmlFor} className="label">
        {label}
      </label>
      {children}
      {hint && <p className="text-[11px] text-slate-500">{hint}</p>}
    </div>
  );
}

/** Input numerico che accetta valori nulli (vuoto → null). */
function NumberInput({
  id,
  value,
  onChange,
  min,
  max,
  step,
  placeholder,
}: {
  id: string;
  value: number | null | undefined;
  onChange: (v: number | null) => void;
  min?: number;
  max?: number;
  step?: number;
  placeholder?: string;
}) {
  return (
    <input
      id={id}
      type="number"
      className="input"
      min={min}
      max={max}
      step={step ?? 1}
      placeholder={placeholder}
      value={typeof value === "number" ? value : ""}
      onChange={(e) => {
        const raw = e.target.value.trim();
        onChange(raw === "" ? null : Number(raw));
      }}
    />
  );
}

/** Toggle che accetta tre stati: on / off / (non impostato). */
function ReasoningToggle({
  id,
  value,
  onChange,
}: {
  id: string;
  value: "on" | "off" | null | undefined;
  onChange: (v: "on" | "off" | null) => void;
}) {
  return (
    <div className="flex items-center gap-4">
      <label className="flex cursor-pointer items-center gap-1.5 text-sm text-slate-300">
        <input
          type="radio"
          name={id}
          checked={value === "on"}
          onChange={() => onChange("on")}
        />
        On
      </label>
      <label className="flex cursor-pointer items-center gap-1.5 text-sm text-slate-300">
        <input
          type="radio"
          name={id}
          checked={value === "off"}
          onChange={() => onChange("off")}
        />
        Off
      </label>
      <label className="flex cursor-pointer items-center gap-1.5 text-sm text-slate-300">
        <input
          type="radio"
          name={id}
          checked={value == null}
          onChange={() => onChange(null)}
        />
        Di default
      </label>
    </div>
  );
}

/** Range di temperatura consigliato dal §8.2, per tipo di modello. */
const TEMPERATURE_RANGE = {
  translation: {
    min: 0.1,
    max: 0.3,
    hint: "0.1–0.3 (§8.2): più basso = più fedele al sorgente",
  },
  text: {
    min: 0,
    max: 0.2,
    hint: "0–0.2 (§8.2): output deterministico per estrazione/analisi",
  },
} as const;

function ModelPanel({
  title,
  settings,
  onChange,
  temperatureMin,
  temperatureMax,
  temperatureHint,
}: {
  title: string;
  settings: ModelSettings["translation"] | ModelSettings["text"];
  onChange: (next: ModelSettings["translation"]) => void;
  temperatureMin?: number;
  temperatureMax?: number;
  temperatureHint?: string;
}) {
  const update = <K extends keyof ModelSettings["translation"]>(
    key: K,
    value: ModelSettings["translation"][K] | null
  ) => {
    onChange({ ...settings, [key]: value });
  };

  return (
    <fieldset className="card space-y-4">
      <legend className="mb-2 text-sm font-semibold text-white">{title}</legend>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field
          htmlFor={`${title}-temperature`}
          label="Temperatura"
          hint={temperatureHint ?? "0–1, più alto = più creativo"}
        >
          <NumberInput
            id={`${title}-temperature`}
            value={settings.temperature}
            onChange={(v) => update("temperature", v)}
            min={temperatureMin ?? 0}
            max={temperatureMax ?? 2}
            step={0.05}
            placeholder="—"
          />
        </Field>
        <Field htmlFor={`${title}-top_p`} label="Top-p" hint="0–1, filtra le parole più probabili">
          <NumberInput
            id={`${title}-top_p`}
            value={settings.top_p}
            onChange={(v) => update("top_p", v)}
            min={0}
            max={1}
            step={0.05}
            placeholder="—"
          />
        </Field>
        <Field htmlFor={`${title}-seed`} label="Seed" hint="Ripetibilità: stesso seed → stessa generazione">
          <NumberInput
            id={`${title}-seed`}
            value={settings.seed}
            onChange={(v) => update("seed", v)}
            min={0}
            step={1}
            placeholder="—"
          />
        </Field>
        <Field htmlFor={`${title}-max_output`} label="Output massimo" hint="Token massimi per risposta">
          <NumberInput
            id={`${title}-max_output`}
            value={settings.max_output}
            onChange={(v) => update("max_output", v)}
            min={0}
            step={256}
            placeholder="Es. 2048"
          />
        </Field>
        <Field htmlFor={`${title}-reasoning`} label="Reasoning">
          <ReasoningToggle
            id={`${title}-reasoning`}
            value={settings.reasoning}
            onChange={(v) => update("reasoning", v)}
          />
        </Field>
        <Field
          htmlFor={`${title}-reasoning_budget`}
          label="Budget reasoning"
          hint="Token riservati al ragionamento (se attivato)"
        >
          <NumberInput
            id={`${title}-reasoning_budget`}
            value={settings.reasoning_budget}
            onChange={(v) => update("reasoning_budget", v)}
            min={0}
            step={128}
            placeholder="—"
          />
        </Field>
        <Field htmlFor={`${title}-timeout`} label="Timeout (s)" hint="Scadenza della richiesta">
          <NumberInput
            id={`${title}-timeout`}
            value={settings.timeout}
            onChange={(v) => update("timeout", v)}
            min={0}
            step={5}
            placeholder="—"
          />
        </Field>
        <Field htmlFor={`${title}-retry`} label="Ritenti" hint="Tentativi in caso di errore">
          <NumberInput
            id={`${title}-retry`}
            value={settings.retry}
            onChange={(v) => update("retry", v)}
            min={0}
            step={1}
            placeholder="—"
          />
        </Field>
      </div>
      <div className="space-y-1.5">
        <label htmlFor={`${title}-prompt_template`} className="label">
          Template prompt
        </label>
        <textarea
          id={`${title}-prompt_template`}
          className="input min-h-24 font-mono text-xs"
          placeholder="Placeholder del template versionato…"
          value={settings.prompt_template ?? ""}
          onChange={(e) => update("prompt_template", e.target.value)}
        />
      </div>
    </fieldset>
  );
}

export function AdvancedSettingsPanel({
  settings,
  onChange,
}: {
  settings: ModelSettings;
  onChange: (next: ModelSettings) => void;
}) {
  const next = settings ?? EMPTY_SETTINGS;

  return (
    <div className="space-y-6">
      <ModelPanel
        title="Modello di traduzione"
        settings={next.translation}
        onChange={(v) => onChange({ ...next, translation: v })}
        temperatureMin={TEMPERATURE_RANGE.translation.min}
        temperatureMax={TEMPERATURE_RANGE.translation.max}
        temperatureHint={TEMPERATURE_RANGE.translation.hint}
      />
      <ModelPanel
        title="Modello di analisi/testo"
        settings={next.text}
        onChange={(v) => onChange({ ...next, text: v })}
        temperatureMin={TEMPERATURE_RANGE.text.min}
        temperatureMax={TEMPERATURE_RANGE.text.max}
        temperatureHint={TEMPERATURE_RANGE.text.hint}
      />
    </div>
  );
}
