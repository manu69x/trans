"use client";

/**
 * Scheda "Verifica Libro" (2026-10-02): confronto affiancato pagina per
 * pagina tra il libro ORIGINALE (sinistra) e il CLONE tradotto (destra).
 *
 * Tre slide:
 * - MAIN: sfoglia ENTRAMBE le pagine insieme (stesso indice)
 * - ORIG: sfoglia solo l'originale (indipendente)
 * - CLONE: sfoglia solo il clone tradotto (indipendente)
 *
 * Il clone viene generato dal backend alla prima richiesta e cachato;
 * il bottone "Rigenera clone" invalida la cache dopo nuove traduzioni.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ErrorNote, Loading } from "@/components/ui";
import {
  clearCloneCache,
  clonePageUrl,
  getTypoInfo,
  listProjects,
  originalPageUrl,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

function PageSlider({
  title,
  subtitle,
  src,
  page,
  onPage,
  max,
  tone,
}: {
  title: string;
  subtitle: string;
  src: string;
  page: number;
  onPage: (p: number) => void;
  max: number;
  tone: "orig" | "clone";
}) {
  const { t } = useI18n();
  return (
    <div className="flex min-w-0 flex-1 flex-col rounded-md border border-slate-800 bg-slate-900/60">
      <div className="flex items-center justify-between border-b border-slate-800 px-3 py-1">
        <span className="text-xs font-semibold text-slate-400">
          {title}{" "}
          <span className="font-normal text-slate-500">· {subtitle}</span>
        </span>
        <span
          className={`rounded px-2 py-0.5 text-[10px] font-semibold ${
            tone === "orig"
              ? "bg-sky-500/20 text-sky-300"
              : "bg-emerald-500/20 text-emerald-300"
          }`}
        >
          {tone === "orig"
            ? t("bookCheck.originalBadge")
            : t("bookCheck.cloneBadge")}
        </span>
      </div>
      <div className="flex min-h-0 flex-1 items-center justify-center bg-neutral-200 p-2">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={t("bookCheck.pageAlt", { title, n: page + 1 })}
          className="max-h-[68vh] max-w-full object-contain shadow-md"
        />
      </div>
      <div className="flex items-center gap-2 border-t border-slate-800 px-3 py-1.5">
        <button
          onClick={() => onPage(Math.max(0, page - 1))}
          disabled={page === 0}
          className="rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-40"
        >
          ←
        </button>
        <input
          type="range"
          min={0}
          max={max}
          value={page}
          onChange={(e) => onPage(Number(e.target.value))}
          className="min-w-0 flex-1 accent-indigo-500"
        />
        <button
          onClick={() => onPage(Math.min(max, page + 1))}
          disabled={page >= max}
          className="rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-40"
        >
          →
        </button>
        <span className="w-16 text-right text-xs text-slate-400">
          {page + 1}/{max + 1}
        </span>
      </div>
    </div>
  );
}

export default function VerificaLibroPage() {
  const { t } = useI18n();
  const [projectId, setProjectId] = useState("");
  const [mainPage, setMainPage] = useState(0);
  const [origPage, setOrigPage] = useState(0);
  const [clonePage, setClonePage] = useState(0);

  const [cacheBust, setCacheBust] = useState(0);

  const projectsQuery = useQuery({
    queryKey: ["progetti"],
    queryFn: listProjects,
    enabled: typeof window !== "undefined",
  });

  useEffect(() => {
    if (!projectId && projectsQuery.data?.length) {
      setProjectId(projectsQuery.data[0].id);
    }
  }, [projectId, projectsQuery.data]);

  const infoQuery = useQuery({
    queryKey: ["typo-info", projectId],
    queryFn: () => getTypoInfo(projectId),
    enabled: Boolean(projectId),
  });

  // Frecce tastiera: MAIN sfoglia entrambe le pagine insieme.
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "ArrowRight" || ev.key === "PageDown") {
        goMain(1);
      } else if (ev.key === "ArrowLeft" || ev.key === "PageUp") {
        goMain(-1);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const origCount = infoQuery.data?.original_pages ?? 0;

  // Totale pagine del clone: REALE, dal backend (/typography/info genera
  // e cachetta il clone). Fix 2026-10-02: prima c'era un hardcap 700 e le
  // pagine oltre il totale sembravano "duplicate" (l'ultima immagine valida
  // restava fissa sullo slider).
  const cloneMax = Math.max(0, (infoQuery.data?.clone_pages ?? 1) - 1);

  // MAIN: avanza/retrocede ENTRAMBE le pagine insieme (2026-10-02); ogni
  // libro e' limitato dal proprio totale, nessuno va oltre la fine.
  const goMain = (delta: number) => {
    // MAIN avanza/retrocede ENTRAMBE le pagine di +1/-1 RELATIVO: i due
    // libri possono trovarsi in punti diversi (totale pagine diverso) e
    // ognuno si muove dal SUO punto attuale, fermandosi alla propria fine.
    setMainPage((mp) => Math.max(0, mp + delta));
    setOrigPage((op) => Math.max(0, Math.min(origCount - 1, op + delta)));
    setClonePage((cp) => Math.max(0, Math.min(cloneMax, cp + delta)));
  };

  if (!projectId) {
    return (
      <div className="mx-auto max-w-xl">
        <h1 className="text-2xl font-bold text-white">
          {t("bookCheck.title")}
        </h1>
        {projectsQuery.isLoading ? (
          <Loading label={t("projects.loading")} />
        ) : (
          <ErrorNote message={t("bookCheck.noProjects")} />
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[1600px]">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-white">
          {t("bookCheck.title")}
        </h1>
        <select
          value={projectId}
          onChange={(e) => {
            setProjectId(e.target.value);
            setMainPage(0);
            setOrigPage(0);
            setClonePage(0);
          }}
          className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
          aria-label={t("importPage.project")}
        >
          {(projectsQuery.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>{p.title}</option>
          ))}
        </select>
        <button
          onClick={async () => {
            await clearCloneCache(projectId);
            setCacheBust((v) => v + 1);
          }}
          title={t("bookCheck.regenerateTitle")}
          className="rounded bg-amber-500/20 px-3 py-1 text-xs text-amber-200 hover:bg-amber-500/30"
        >
          {t("bookCheck.regenerate")}
        </button>
        <span className="ml-auto text-xs text-slate-500">
          {t("bookCheck.pagesInfo", { n: origCount || "…" })}
        </span>
      </div>

      {infoQuery.isLoading ? (
        <Loading label={t("bookCheck.analyzing")} />
      ) : infoQuery.isError || !infoQuery.data ? (
        <ErrorNote message={t("bookCheck.pdfError")} />
      ) : (
        <>
          {/* Slide MAIN: sfoglia entrambe insieme */}
          <div className="mt-3 flex items-center gap-2 rounded-md border border-indigo-800/60 bg-indigo-950/40 px-4 py-2">
            <span className="text-xs font-semibold text-indigo-200">
              {t("bookCheck.mainBar")}
            </span>
            <button
              onClick={() => goMain(-1)}
              disabled={mainPage === 0}
              className="rounded bg-slate-800 px-3 py-0.5 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-40"
            >
              ←
            </button>
            <button
              onClick={() => goMain(1)}
              disabled={mainPage >= Math.max(origCount, 1) - 1}
              className="rounded bg-slate-800 px-3 py-0.5 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-40"
            >
              →
            </button>
            <span className="text-xs text-slate-500">
              {t("bookCheck.mainHint")}
            </span>
          </div>

          <div className="mt-3 flex flex-col gap-3 lg:flex-row">
            <PageSlider
              title={t("bookCheck.origPanel")}
              subtitle={t("bookCheck.origSubtitle")}
              src={`${originalPageUrl(projectId, origPage + 1)}?v=${cacheBust}`}
              page={origPage}
              onPage={setOrigPage}
              max={Math.max(0, origCount - 1)}
              tone="orig"
            />
            <PageSlider
              title={t("bookCheck.clonePanel")}
              subtitle={t("bookCheck.cloneSubtitle")}
              src={`${clonePageUrl(projectId, clonePage + 1)}?v=${cacheBust}`}
              page={clonePage}
              onPage={setClonePage}
              max={cloneMax}
              tone="clone"
            />
          </div>
        </>
      )}
    </div>
  );
}
