"use client";

/**
 * Scheda "Anteprima" — simulazione del libro con scorrimento per pagina
 * (2026-10-01): spread bilingue, ITALIANO a sinistra (il testo che verrà
 * esportato), INGLESE a destra (sorgente corrispondente). Nero su bianco,
 * impaginazione lato server sulla colonna IT (~1.800 caratteri/pagina, mai
 * un segmento spezzato, capitolo nuovo = pagina nuova); scorrimento con
 * frecce/PageUp/PageDown/Home/Fine, salto ai capitoli e numero di pagina.
 */

import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ErrorNote, Loading } from "@/components/ui";
import {
  getPreviewBook,
  listProjects,
  type BookFlowSegment,
} from "@/lib/api";

const CENTERED = new Set(["scene_break", "epigraph", "back_matter", "front_matter"]);

/** Converte i marker <i>/<em> del testo in corsivi (escape React incluso). */
function RichText({ text }: { text: string }) {
  const parts = useMemo(() => {
    const out: { it: boolean; t: string }[] = [];
    const re = /<i>|<em>|<\/i>|<\/em>/g;
    let italic = false;
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) out.push({ it: italic, t: text.slice(last, m.index) });
      italic = m[0].startsWith("<i") || m[0] === "<em>";
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push({ it: italic, t: text.slice(last) });
    return out;
  }, [text]);
  return (
    <>
      {parts.map((p, i) =>
        p.it ? <em key={i}>{p.t}</em> : <span key={i}>{p.t}</span>
      )}
    </>
  );
}

export default function AnteprimaPage() {
  const [projectId, setProjectId] = useState("");
  const [includeDrafts, setIncludeDrafts] = useState(false);
  const [watermark, setWatermark] = useState(false);
  const [page, setPage] = useState(0);
  const [tocOpen, setTocOpen] = useState(false);

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

  const options = { format: "pdf" as const, include_drafts: includeDrafts, watermark };

  const bookQuery = useQuery({
    queryKey: ["anteprima-book", projectId, includeDrafts, watermark],
    queryFn: () => getPreviewBook(projectId, options),
    enabled: Boolean(projectId),
    // Il libro intero (~2.5MB) non cambia finché non cambiano le opzioni:
    // resta in cache per tutta la sessione di lettura.
    staleTime: 5 * 60 * 1000,
  });

  const book = bookQuery.data;

  const slice = useCallback(
    (s: number, e: number): BookFlowSegment[] =>
      book ? book.flow.slice(s, e) : [],
    [book]
  );

  // Navigazione da tastiera: frecce / PageUp / PageDown / Home / Fine.
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (!book) return;
      if (ev.key === "ArrowRight" || ev.key === "PageDown") {
        setPage((p) => Math.min(book.total_pages - 1, p + 1));
      } else if (ev.key === "ArrowLeft" || ev.key === "PageUp") {
        setPage((p) => Math.max(0, p - 1));
      } else if (ev.key === "Home") {
        setPage(0);
      } else if (ev.key === "End") {
        setPage(book.total_pages - 1);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [book]);

  // Primo indice di pagina di ogni capitolo (per il salto ai capitoli).
  const chapters = useMemo(() => {
    if (!book) return [];
    const seen = new Set<string>();
    const out: { title: string; page: number }[] = [];
    for (const p of book.pages) {
      if (!seen.has(p.chapter_title)) {
        seen.add(p.chapter_title);
        out.push({ title: p.chapter_title, page: p.number - 1 });
      }
    }
    return out;
  }, [book]);

  const safePage = Math.min(page, Math.max(0, (book?.total_pages ?? 1) - 1));
  const current = book?.pages[safePage];
  const leftSegs = current ? slice(current.segment_start, current.segment_end) : [];
  const rightSegs = leftSegs; // stesso intervallo di segmenti, colonna EN

  if (!projectId) {
    return (
      <div className="mx-auto max-w-xl">
        <h1 className="text-2xl font-bold text-white">Anteprima</h1>
        {projectsQuery.isLoading ? (
          <Loading label="Caricamento progetti…" />
        ) : (
          <ErrorNote message="Nessun progetto disponibile." />
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl">
      {/* Barra controlli */}
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-white">Anteprima</h1>
        <select
          value={projectId}
          onChange={(e) => {
            setProjectId(e.target.value);
            setPage(0);
          }}
          className="rounded bg-slate-800 px-2 py-1 text-sm text-white"
          aria-label="Progetto"
        >
          {(projectsQuery.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>{p.title}</option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm text-slate-400">
          <input
            type="checkbox"
            checked={includeDrafts}
            onChange={(e) => {
              setIncludeDrafts(e.target.checked);
              setPage(0);
            }}
          />
          Includi bozze
        </label>
        {includeDrafts && (
          <label className="flex items-center gap-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={watermark}
              onChange={(e) => setWatermark(e.target.checked)}
            />
            Watermark [BOZZA]
          </label>
        )}
        <span className="ml-auto text-xs text-slate-500">
          ← → / PagSu / PagGiù / Inizio / Fine per sfogliare
        </span>
      </div>

      {bookQuery.isLoading ? (
        <Loading label="Impagino il libro…" />
      ) : bookQuery.isError ? (
        <ErrorNote message="Impossibile impaginare il libro." />
      ) : book?.error ? (
        <div className="mt-4 rounded border border-amber-800/60 bg-amber-950/30 px-4 py-3 text-sm text-amber-200">
          {book.message}
        </div>
      ) : book && current ? (
        <>
          {/* Navigazione pagine */}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              onClick={() => setPage(0)}
              disabled={safePage === 0}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-40"
            >
              ⏮ Inizio
            </button>
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={safePage === 0}
              className="rounded bg-slate-800 px-3 py-1 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-40"
            >
              ← Precedente
            </button>
            <input
              type="number"
              min={1}
              max={book.total_pages}
              value={safePage + 1}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (Number.isFinite(n)) {
                  setPage(Math.max(0, Math.min(book.total_pages - 1, n - 1)));
                }
              }}
              className="w-20 rounded bg-slate-800 px-2 py-1 text-center text-sm text-white"
              aria-label="Pagina"
            />
            <span className="text-sm text-slate-400">di {book.total_pages}</span>
            <button
              onClick={() => setPage((p) => Math.min(book.total_pages - 1, p + 1))}
              disabled={safePage >= book.total_pages - 1}
              className="rounded bg-slate-800 px-3 py-1 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-40"
            >
              Successivo →
            </button>
            <button
              onClick={() => setPage(book.total_pages - 1)}
              disabled={safePage >= book.total_pages - 1}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-40"
            >
              Fine ⏭
            </button>
            <button
              onClick={() => setTocOpen((v) => !v)}
              className="ml-auto rounded bg-slate-800 px-3 py-1 text-sm text-slate-200 hover:bg-slate-700"
            >
              {tocOpen ? "Chiudi indice" : "📑 Capitoli"}
            </button>
          </div>

          {/* Indice a tendina */}
          {tocOpen && (
            <div className="mt-2 max-h-72 overflow-y-auto rounded-md border border-slate-800 bg-slate-900/80 p-2">
              {chapters.map((c) => (
                <button
                  key={c.title + c.page}
                  onClick={() => {
                    setPage(c.page);
                    setTocOpen(false);
                  }}
                  className={`block w-full rounded px-2 py-1 text-left text-xs ${
                    current.chapter_title === c.title
                      ? "bg-indigo-500/25 text-indigo-100"
                      : "text-slate-400 hover:bg-slate-800"
                  }`}
                >
                  {c.title}{" "}
                  <span className="text-[10px] text-slate-500">
                    → pag. {c.page + 1}
                  </span>
                </button>
              ))}
            </div>
          )}

          {/* Spread bilingue: pagina ITALIANA a sinistra, INGLESE a destra */}
          <div className="mt-3 grid grid-cols-1 gap-0 md:grid-cols-2">
            <div className="book-page rounded-l-md border border-neutral-300 bg-white p-10 shadow-inner md:min-h-[75vh]">
              <p className="mb-6 text-center text-[10px] uppercase tracking-[0.2em] text-neutral-400">
                {current.chapter_title}
              </p>
              <div className="space-y-3 font-serif text-[15px] leading-7 text-black">
                {leftSegs.map((s) => (
                  <p
                    key={s.segment_id}
                    className={
                      s.kind && CENTERED.has(s.kind)
                        ? "text-center"
                        : "indent-8 text-justify"
                    }
                  >
                    <RichText text={s.target} />
                    {s.is_draft && watermark && (
                      <span className="ml-1 rounded bg-red-100 px-1 text-[10px] font-semibold text-red-700">
                        [BOZZA]
                      </span>
                    )}
                  </p>
                ))}
              </div>
              <p className="mt-8 text-center text-[11px] text-neutral-400">
                — {current.number} —
              </p>
            </div>

            <div className="book-page rounded-r-md border border-l-0 border-neutral-300 bg-white p-10 shadow-inner md:min-h-[75vh]">
              <p className="mb-6 text-center text-[10px] uppercase tracking-[0.2em] text-neutral-400">
                {current.chapter_title}
              </p>
              <div className="space-y-3 font-serif text-[15px] leading-7 text-neutral-700">
                {rightSegs.map((s) => (
                  <p
                    key={s.segment_id}
                    className={
                      s.kind && CENTERED.has(s.kind)
                        ? "text-center"
                        : "indent-8 text-justify"
                    }
                  >
                    <RichText text={s.source} />
                  </p>
                ))}
              </div>
              <p className="mt-8 text-center text-[11px] text-neutral-300">
                {current.number}
              </p>
            </div>
          </div>

          <p className="mt-2 text-xs text-slate-500">
            Paginazione calcolata sul testo italiano (colonna sinistra, ~
            {book.page_size_chars} caratteri/pagina; la colonna inglese mostra
            gli stessi segmenti). L&apos;export PDF impagina in modo
            indipendente (A5, ~3.400 caratteri/pagina).
          </p>
        </>
      ) : null}
    </div>
  );
}
