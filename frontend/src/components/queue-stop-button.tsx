"use client";

/**
 * Pulsante di arresto della coda job (§16): annulla TUTTI i job in coda
 * (segmentazione, traduzione, estrazione...). I job in esecuzione finiscono
 * il lavoro corrente. Il messaggio di esito resta qualche secondo.
 */

import { useState } from "react";

import { stopQueue } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export default function QueueStopButton() {
  const { t } = useI18n();
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onStop() {
    if (!window.confirm(t("queueStop.confirm"))) {
      return;
    }
    setBusy(true);
    try {
      const res = await stopQueue();
      setMsg(
        res.cancelled > 0
          ? t("queueStop.stoppedCount", { n: res.cancelled })
          : t("queueStop.empty")
      );
    } catch {
      setMsg(t("queueStop.error"));
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
        title={t("queueStop.title")}
        className="w-full rounded border border-red-400/40 bg-red-500/10 px-2 py-1 text-xs font-medium text-red-200 hover:bg-red-500/25 disabled:opacity-40"
      >
        {busy ? t("queueStop.stopping") : t("queueStop.button")}
      </button>
      {msg && <p className="mt-1 text-[11px] text-slate-400">{msg}</p>}
    </div>
  );
}
