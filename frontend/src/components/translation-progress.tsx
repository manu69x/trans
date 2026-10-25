"use client";

/**
 * Indicatore di avanzamento traduzione (sidebar, sempre visibile).
 *
 * Polla ogni 5 s il progresso globale: segmenti tradotti sul totale,
 * avanzamento percentuale e stima dei tempi calcolata sul ritmo osservato
 * (segmenti/minute dall'avvio della campagna, affinata a ogni job
 * completato). Visibile solo mentre ci sono job in corso o in coda.
 */

import { useEffect, useState } from "react";

import {
  getTranslationProgress,
  resumeTranslation,
  stopTranslation,
  type TranslationProgress,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export default function TranslationProgress() {
  const { locale, t } = useI18n();
  const intlLocale = locale === "it" ? "it-IT" : "en-GB";
  const [p, setP] = useState<TranslationProgress | null>(null);
  const [busy, setBusy] = useState<"stop" | "resume" | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const d = await getTranslationProgress();
        if (alive) setP(d);
      } catch {
        /* backend momentaneamente non raggiungibile: ritenta al prossimo tick */
      }
    };
    tick();
    const t = setInterval(tick, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  async function onStop() {
    setBusy("stop");
    try {
      const res = await stopTranslation();
      setP((prev) =>
        prev
          ? { ...prev, jobs_pending: res.running_left }
          : prev
      );
    } catch {
      /* il poll successivo aggiorna comunque */
    } finally {
      setBusy(null);
    }
  }

  async function onResume() {
    setBusy("resume");
    try {
      await resumeTranslation();
      const d = await getTranslationProgress();
      setP(d);
    } catch {
      /* il poll successivo aggiorna comunque */
    } finally {
      setBusy(null);
    }
  }

  // visibile per 24h dall'ultima attività di traduzione: in corso se ci sono
  // job in coda/esecuzione, altrimenti "in pausa" (con Riprendi a portata)
  if (!p?.active) return null;
  const inCorso = (p.jobs_pending ?? 0) > 0;

  const total = p.total_units ?? 0;
  const done = p.translated_units ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const etaMin = p.eta_seconds != null ? Math.max(1, Math.round(p.eta_seconds / 60)) : null;
  const fine = p.eta_seconds != null
    ? new Date(Date.now() + p.eta_seconds * 1000).toLocaleTimeString(intlLocale, {
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  return (
    <div className="mx-2 mb-2 rounded-md border border-indigo-500/30 bg-indigo-500/10 p-3 text-xs">
      <p className="font-semibold text-indigo-200">
        {inCorso ? t("progress.running") : t("progress.paused")}
      </p>
      <p className="mt-1 text-slate-300">
        {t("progress.segmentsCount", {
          done: done.toLocaleString(intlLocale),
          total: total.toLocaleString(intlLocale),
        })}
        {" · "}{pct}%
      </p>
      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded bg-slate-700">
        <div
          className="h-full rounded bg-indigo-400 transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
      {inCorso && etaMin != null ? (
        <p className="mt-1.5 text-slate-400">
          {t("progress.eta", { min: etaMin, end: fine ?? "—" })}
        </p>
      ) : !inCorso ? (
        <p className="mt-1.5 text-slate-500">{t("progress.pausedDetail")}</p>
      ) : (
        <p className="mt-1.5 text-slate-500">{t("progress.etaPending")}</p>
      )}
      {!inCorso && (p.jobs_failed ?? 0) > 0 && (
        <p className="mt-1 text-amber-300">
          {p.jobs_failed}{" "}
          {p.jobs_failed === 1
            ? t("progress.failedOne")
            : t("progress.failedMany")}
          {t("progress.failedHint")}
        </p>
      )}
      <p className="mt-1 text-slate-500">
        {p.jobs_pending === 1
          ? t("progress.queuedOne")
          : t("progress.queuedMany", { n: p.jobs_pending ?? 0 })}
        {" · "}{t("progress.completedJobs", { n: p.jobs_completed ?? 0 })}
      </p>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {inCorso && (
          <button
            type="button"
            onClick={onStop}
            disabled={busy !== null}
            className="rounded border border-red-400/40 bg-red-500/15 px-2 py-1 font-medium text-red-200 hover:bg-red-500/25 disabled:opacity-40"
          >
            {busy === "stop" ? t("progress.stopping") : t("progress.stop")}
          </button>
        )}
        <button
          type="button"
          onClick={onResume}
          disabled={busy !== null}
          className="rounded border border-emerald-400/40 bg-emerald-500/15 px-2 py-1 font-medium text-emerald-200 hover:bg-emerald-500/25 disabled:opacity-40"
        >
          {busy === "resume" ? t("progress.queueing") : t("progress.resume")}
        </button>
      </div>
    </div>
  );
}
