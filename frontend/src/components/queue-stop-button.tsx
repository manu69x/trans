"use client";

/**
 * Pulsante di arresto della coda job (§16): annulla TUTTI i job in coda
 * (segmentazione, traduzione, estrazione...). I job in esecuzione finiscono
 * il lavoro corrente. Il messaggio di esito resta qualche secondo.
 */

import { useState } from "react";

import { stopQueue } from "@/lib/api";

export default function QueueStopButton() {
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onStop() {
    if (
      !window.confirm(
        "Annullare tutti i job in coda? I job in esecuzione finiranno il lavoro corrente."
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      const res = await stopQueue();
      setMsg(
        res.cancelled > 0
          ? `Coda fermata: ${res.cancelled} job annullati`
          : "Nessun job in coda"
      );
    } catch {
      setMsg("Errore durante l'arresto della coda");
    } finally {
      setBusy(false);
      setTimeout(() => setMsg(null), 5000);
    }
  }

  return (
    <div className="mx-2 mb-2">
      <button
        type="button"
        onClick={onStop}
        disabled={busy}
        title="Annulla tutti i job in coda (quelli in esecuzione finiscono)"
        className="w-full rounded border border-red-400/40 bg-red-500/10 px-2 py-1 text-xs font-medium text-red-200 hover:bg-red-500/25 disabled:opacity-40"
      >
        {busy ? "Arresto…" : "⏹ Ferma coda job"}
      </button>
      {msg && <p className="mt-1 text-[11px] text-slate-400">{msg}</p>}
    </div>
  );
}
