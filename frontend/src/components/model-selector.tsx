/**
 * Selettore modello condiviso (§8.2 / §8.1).
 *
 * Dropdown di un singolo modello tra le righe della matrice delle
 * capability di LLM Gateway. Nessun nome hardcodato: le opzioni sono
 * popolate dinamicamente da `listGatewayModels()`.
 *
 * Rende visibili le capacità rilevanti per la scelta del traduttore
 * (JSON, streaming, reasoning, seed, finestra di contesto) e lo stato
 * (attivo/inattivo) come badge.
 */

import type { GatewayModel } from "@/lib/api-types";

const CAPABILITY: {
  key: keyof GatewayModel;
  label: string;
  ok: string;
  off: string;
}[] = [
  { key: "supports_json_schema", label: "JSON", ok: "JSON", off: "no JSON" },
  { key: "supports_streaming", label: "Streaming", ok: "Streaming", off: "no stream" },
  { key: "supports_reasoning", label: "Reasoning", ok: "Reasoning", off: "no reasoning" },
  { key: "supports_seed", label: "Seed", ok: "Seed", off: "no seed" },
];

function CapabilityChips({ model }: { model: GatewayModel }) {
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      {CAPABILITY.map((c) => {
        const active = Boolean(model[c.key]);
        return (
          <span
            key={c.key}
            className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
              active
                ? "bg-indigo-500/20 text-indigo-200"
                : "bg-slate-800 text-slate-400"
            }`}
            title={`${c.label}: ${active ? "supportato" : "non supportato"}`}
          >
            {active ? c.ok : c.off}
          </span>
        );
      })}
    </div>
  );
}

export function ModelSelector({
  label,
  name,
  value,
  onChange,
  models,
  disabled = false,
  placeholder = "Seleziona un modello",
  className = "",
}: {
  label: string;
  name?: string;
  value: string;
  onChange: (modelId: string) => void;
  models: GatewayModel[];
  disabled?: boolean;
  placeholder?: string;
  className?: string;
}) {
  const selected = models.find((m) => m.id === value) ?? null;

  return (
    <div className={className}>
      <label htmlFor={name} className="label">
        {label}
      </label>
      <select
        id={name}
        name={name}
        className="input"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">{placeholder}</option>
        {models.map((m) => (
          // §8.1: llama-swap loads models ON DEMAND — "offline" means
          // "not currently loaded", NOT "unusable". Every listed model is
          // selectable; only the placeholder row is the empty choice.
          <option key={m.id} value={m.id}>
            {m.display_name ?? m.id}
          </option>
        ))}
      </select>
      {selected && <CapabilityChips model={selected} />}
    </div>
  );
}
