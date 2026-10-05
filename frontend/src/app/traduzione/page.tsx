"use client";

/**
 * Editor bilingue a 3 pannelli (PRD §11.2, §11.3, §10.5 / F3).
 *
 * Sinistra: sorgente EN + stato/flags. Centro: target IT modificabile con
 * diff e storico. Destra: QA issues + bottoni approve/reject/refine.
 * Shortcut: Ctrl+Enter approva.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  approveSegment,
  bulkSegmentStatus,
  getStructure,
  listProjectJobs,
  listProjects,
  listQaIssues,
  listSegments,
  listSegmentVersions,
  refineSegment,
  rejectSegment,
  runTranslation,
  segmentChapter,
  searchReplace,
  stopQueue,
  verifyTranslations,
  type Project,
  type QaIssue,
  type SegmentVersion,
  type StructureNode,
} from "@/lib/api";
import type { Job } from "@/lib/api-types";
import { ErrorNote, Loading, StatusBadge } from "@/components/ui";
import { QeText } from "@/components/qe-highlight";
import { QUERY_KEY_JOBS } from "@/lib/constants";

/** Evidenzia le occorrenze della query nel testo (giallo, case-insensitive). */
function SearchText({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const lower = text.toLowerCase();
  const q = query.toLowerCase();
  const out: React.ReactNode[] = [];
  let pos = 0;
  let k = 0;
  while (true) {
    const i = lower.indexOf(q, pos);
    if (i < 0) {
      out.push(<span key={`t${k++}`}>{text.slice(pos)}</span>);
      break;
    }
    if (i > pos) out.push(<span key={`t${k++}`}>{text.slice(pos, i)}</span>);
    out.push(
      <mark
        key={`m${k++}`}
        className="rounded-sm bg-amber-400 px-0.5 font-semibold text-black"
      >
        {text.slice(i, i + query.length)}
      </mark>
    );
    pos = i + query.length;
  }
  return <>{out}</>;
}

export default function TranslationPage() {
  const queryClient = useQueryClient();
  const [projectId, setProjectId] = useState("");
  const [chapterId, setChapterId] = useState("");

  // Client-only: read ?projectId= / ?chapterId= after mount.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setProjectId(params.get("projectId") ?? "");
    setChapterId(params.get("chapterId") ?? "");
  }, []);

  const [activeSegment, setActiveSegment] = useState<string | null>(null);
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const [showVersions, setShowVersions] = useState(false);
  const [editingTarget, setEditingTarget] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchReplaceWith, setSearchReplaceWith] = useState("");
  const [searchScope, setSearchScope] = useState<
    "project" | "chapter" | "segment"
  >("project");
  const [searchPreview, setSearchPreview] = useState<null | {
    applied: boolean;
    matches: number;
    replacements: { segment_id: string; ordinal: number; old: string; new: string }[];
    readonly_matches?: number;
    readonly_replacements?: { segment_id: string; ordinal: number; old: string; new: string }[];
  }>(null);
  // Navigazione tra le occorrenze trovate (2026-10-01): elenco dei segmenti
  // con match (union di sostituibili + approvati), indice corrente, evidenzia
  // la query nel testo e apre il segmento nell'editor.
  const [searchHit, setSearchHit] = useState(-1);
  const [searchText, setSearchText] = useState("");
  // Filtri combinabili (2026-09-23): sorgente, target, DIFF QE e soglia TR.
  const [fSource, setFSource] = useState<"all" | "untranslated" | "ocr">(
    "all"
  );
  const [fTarget, setFTarget] = useState<
    "all" | "approved" | "draft" | "qa"
  >("all");
  const [fDiff, setFDiff] = useState<"all" | "pos" | "neg">("all");
  const [fTr, setFTr] = useState(0);
  // 1 = nessun filtro (tutti); sotto 1 mostra solo TR sotto la soglia.
  const [fTrMax, setFTrMax] = useState(1);
  // Ordinamento della lista segmenti.
  const [sortKey, setSortKey] = useState<
    "numero" | "it" | "tr" | "en" | "diff"
  >("numero");
  const [sortAsc, setSortAsc] = useState(true);
  const [toast, setToast] = useState<string | null>(null);
  // Selezione segmenti (checkbox) per la traduzione massiva.
  const [selectedSegments, setSelectedSegments] = useState<Set<string>>(
    new Set()
  );
  const [segAllPages, setSegAllPages] = useState(false);
  const queryClientRef = useRef(queryClient);
  queryClientRef.current = queryClient;

  // Elenco progetti per il selettore (client-only: attivo dopo il mount).
  const projectsQuery = useQuery({
    queryKey: ["progetti"],
    queryFn: listProjects,
    enabled: typeof window !== "undefined",
  });

  // Struttura (capitoli) per la segmentazione dallo stato vuoto.
  const structureQuery = useQuery({
    queryKey: ["struttura", projectId],
    queryFn: () => getStructure(projectId),
    enabled: Boolean(projectId) && typeof window !== "undefined",
  });

  /** Cambia progetto: aggiorna l'URL e azzera lo stato del segmento attivo. */
  function switchProject(id: string) {
    const url = new URL(window.location.href);
    url.searchParams.set("projectId", id);
    url.searchParams.delete("chapterId");
    window.history.replaceState(null, "", url.toString());
    setProjectId(id);
    setChapterId("");
    setActiveSegment(null);
    setTarget("");
  }

  const segmentsQuery = useQuery({
    queryKey: ["segmenti", projectId, chapterId],
    queryFn: () =>
      listSegments(projectId, { chapter_id: chapterId || null }),
    enabled: Boolean(projectId),
  });

  const activeQuery = useQuery({
    queryKey: ["segmente-attivo", projectId, activeSegment],
    queryFn: () => listSegments(projectId, { chapter_id: chapterId || null }),
    enabled: false,
  });

  const versionsQuery = useQuery({
    queryKey: ["versioni", projectId, activeSegment],
    queryFn: () =>
      activeSegment
        ? listSegmentVersions(projectId, activeSegment)
        : Promise.resolve({ segment_id: "", history: [], count: 0 }),
    enabled: showVersions && Boolean(activeSegment),
  });

  const qaQuery = useQuery({
    queryKey: ["qa", projectId, chapterId],
    queryFn: () => listQaIssues(projectId, { chapter_id: chapterId || null }),
    enabled: Boolean(projectId),
  });

  const approveMutation = useMutation({
    mutationFn: () =>
      activeSegment
        ? approveSegment(projectId, activeSegment)
        : Promise.reject(new Error("nessun segmento attivo")),
    onSuccess: () => {
      setToast("Segmento approvato");
      queryClientRef.current.invalidateQueries({
        queryKey: QUERY_KEY_JOBS(projectId),
      });
      refresh();
    },
  });

  const rejectMutation = useMutation({
    mutationFn: () =>
      activeSegment
        ? rejectSegment(projectId, activeSegment, reason || null)
        : Promise.reject(new Error("nessun segmento attivo")),
    onSuccess: () => {
      setToast("Segmento rifiutato");
      refresh();
    },
  });

  const refineMutation = useMutation({
    mutationFn: () =>
      activeSegment && target
        ? refineSegment(projectId, activeSegment, target, reason || null)
        : Promise.reject(new Error("inserisci una traduzione")),
    onSuccess: () => {
      setToast("Modifica salvata");
      setEditingTarget(false);
      refresh();
    },
    onError: (e: Error) =>
      setToast(`Errore salvataggio: ${e.message}`),
  });

  const searchMutation = useMutation({
    mutationFn: () =>
      searchQuery
        ? searchReplace(projectId, {
            query: searchQuery,
            replace_with: searchReplaceWith,
            scope: searchScope,
            chapter_id: searchScope === "chapter" ? chapterId || null : null,
            apply: false,
          })
        : Promise.reject(new Error("inserisci un termine da cercare")),
    onSuccess: (data) => {
      setSearchPreview(data);
      // Navigazione occorrenze: unione di sostituibili + approvati, in ordine.
      setSearchText(searchQuery);
      setSearchHit(data.replacements.length > 0 ? 0 : -1);
    },
  });

  // Segmenti con match, in ordine di comparso nella risposta: prima i
  // sostituibili (bozze), poi quelli approvati (sola lettura).
  const searchHits = useMemo(() => {
    if (!searchPreview) return [] as { segment_id: string; readonly: boolean }[];
    const out = searchPreview.replacements.map((r) => ({
      segment_id: r.segment_id,
      readonly: false,
    }));
    for (const r of searchPreview.readonly_replacements ?? []) {
      out.push({ segment_id: r.segment_id, readonly: true });
    }
    return out;
  }, [searchPreview]);

  // All'occorrenza corrente: seleziona il segmento (apre l'editor) e
  // scorre la lista fino a renderlo visibile.
  useEffect(() => {
    if (searchHit < 0 || searchHits.length === 0) return;
    const hit = searchHits[Math.min(searchHit, searchHits.length - 1)];
    if (!hit) return;
    setActiveSegment(hit.segment_id);
    const el = document.getElementById(`seg-row-${hit.segment_id}`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [searchHit, searchHits]);

  // Segmentazione di un capitolo (§5.4): abilita i segmenti per l'editor.
  const segmentChapterMutation = useMutation({
    mutationFn: (nodeId: string) => segmentChapter(projectId, nodeId),
    onSuccess: () => {
      setToast("Segmentazione avviata…");
      queryClientRef.current.invalidateQueries({
        queryKey: ["segmenti", projectId, chapterId],
      });
    },
    onError: (e: Error) => setToast(`Errore segmentazione: ${e.message}`),
  });

  // Traduzione dei segmenti selezionati (§10.1): un job per blocco di 20.
  const translateSelectionMutation = useMutation({
    mutationFn: (ids: string[]) => runTranslation(projectId, ids),
    onSuccess: () => {
      setToast("Traduzione avviata: i segmenti appariranno come bozza LLM");
      setSelectedSegments(new Set());
      setSegAllPages(false);
      queryClientRef.current.invalidateQueries({
        queryKey: QUERY_KEY_JOBS(projectId),
      });
      refresh();
    },
    onError: (e: Error) => setToast(`Errore: ${e.message}`),
  });

  // Verifica QE massiva (§10.2-bis): job asincrono, un forward pass per
  // segmento sul modello Open-QE (GPU0). Poll dello stato fino al termine.
  const verifyMutation = useMutation({
    mutationFn: async () => {
      // Come gli altri tasti bulk: agisce sulla selezione corrente.
      const res = await verifyTranslations(projectId, [...selectedSegments]);
      for (let i = 0; i < 200; i++) {
        const jobs = await listProjectJobs(projectId);
        const j = jobs.find((x) => x.id === res.job_id);
        if (j && (j.status === "completed" || j.status === "failed")) {
          return j;
        }
        await new Promise((r) => setTimeout(r, 4000));
      }
      throw new Error("timeout verifica QE");
    },
    onSuccess: (job) => {
      refresh();
      const result = (job.result ?? {}) as {
        verify?: { total?: number };
      };
      if (job.status === "completed") {
        const n = result.verify?.total ?? "?";
        setToast(`Verifica QE completata su ${n} segmenti`);
      } else {
        setToast(`Verifica QE fallita: ${job.error ?? "?"}`);
      }
    },
    onError: (e: Error) => setToast(`Errore verifica QE: ${e.message}`),
  });

  // Azioni massive di stato sui segmenti selezionati (§11.2): approvazione
  // e riporto in bozza, in una singola chiamata bulk lato backend.
  const bulkStatusMutation = useMutation({
    mutationFn: (input: { ids: string[]; status: "approved" | "machine_draft" }) =>
      bulkSegmentStatus(projectId, input.ids, input.status),
    onSuccess: (res) => {
      setToast(
        `${res.status === "approved" ? "Approvati" : "In bozza"}: ${res.updated} segmenti`
      );
      setSelectedSegments(new Set());
      setSegAllPages(false);
      refresh();
    },
    onError: (e: Error) => setToast(`Errore: ${e.message}`),
  });

  /** Toggle checkbox di un segmento. */
  function toggleSegment(id: string) {
    setSelectedSegments((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  /** Seleziona/deseleziona tutti i segmenti della lista corrente. */
  function toggleAllSegments() {
    setSelectedSegments((prev) => {
      const allSelected = segments.every((s) => prev.has(s.segment_id));
      if (allSelected) return new Set();
      return new Set(segments.map((s) => s.segment_id));
    });
  }

  function refresh() {
    queryClientRef.current.invalidateQueries({
      queryKey: ["segmenti", projectId, chapterId],
    });
    if (activeSegment) {
      void activeQuery.refetch();
    }
  }

  // Popila il centro con il primo segmento / quello cliccato.
  useEffect(() => {
    if (segmentsQuery.data && segmentsQuery.data.segments.length > 0) {
      if (!activeSegment) {
        const first = segmentsQuery.data.segments[0];
        setActiveSegment(first.segment_id);
        setTarget(first.target_text ?? "");
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [segmentsQuery.data]);

  // Shortcut: Ctrl+Enter approva.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.ctrlKey && e.key === "Enter") {
        e.preventDefault();
        if (activeSegment) {
          approveMutation.mutate();
        }
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [activeSegment, approveMutation]);

  // Reset stato interno quando cambia il segmento attivo.
  useEffect(() => {
    const seg = segmentsQuery.data?.segments.find(
      (s) => s.segment_id === activeSegment
    );
    if (seg) {
      setTarget(seg.target_text ?? "");
      setReason("");
      setShowVersions(false);
      setEditingTarget(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSegment]);

  // Toast auto-dismiss.
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(t);
  }, [toast]);

  const activeSegmentData = useMemo(
    () =>
      segmentsQuery.data?.segments.find(
        (s) => s.segment_id === activeSegment
      ) ?? null,
    [segmentsQuery.data, activeSegment]
  );

  if (!projectId) {
    const projects = projectsQuery.data ?? [];
    return (
      <div className="mx-auto max-w-xl">
        <h1 className="text-2xl font-bold text-white">
          Scegli il progetto da tradurre
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          Seleziona un progetto per aprire l&apos;editor di traduzione.
        </p>
        {projectsQuery.isLoading ? (
          <Loading label="Caricamento progetti…" />
        ) : projectsQuery.isError ? (
          <ErrorNote message="Impossibile caricare i progetti dal backend." />
        ) : projects.length === 0 ? (
          <p className="mt-4 text-sm text-slate-400">
            Nessun progetto presente. Creane uno dalla pagina{" "}
            <a href="/progetti/nuovo" className="text-indigo-300 underline">
              Progetti
            </a>
            .
          </p>
        ) : (
          <ul className="mt-4 space-y-2">
            {projects.map((p: Project) => (
              <li key={p.id}>
                <button
                  onClick={() => switchProject(p.id)}
                  className="card w-full text-left hover:bg-slate-800/60"
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-medium text-white">{p.title}</span>
                    <StatusBadge status={p.status} />
                  </div>
                  <p className="mt-1 text-xs text-slate-500">
                    {p.source_language.toUpperCase()} →{" "}
                    {p.target_language.toUpperCase()} · profilo {p.genre_profile}
                  </p>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  if (segmentsQuery.isLoading) {
    return <Loading label="Caricamento segmenti…" />;
  }
  if (segmentsQuery.isError) {
    return (
      <ErrorNote message="Impossibile caricare i segmenti dal backend." />
    );
  }
  const allSegments = segmentsQuery.data?.segments ?? [];
  // Niente useMemo: questo punto sta dopo gli early return di loading/errore
  // e un hook qui violerebbe le regole di React (crash client-side).
  const segments = allSegments.filter((s) => {
    if (fSource === "untranslated" && s.status !== "untranslated") return false;
    if (
      fSource === "ocr" &&
      !(s.source_flags as { ocr_suspect?: boolean } | null)?.ocr_suspect
    )
      return false;
    if (fTarget === "approved" && s.status !== "approved") return false;
    if (fTarget === "draft" && s.status !== "machine_draft") return false;
    if (fTarget === "qa" && !(s.has_qa || s.qa_critical > 0)) return false;
    if (fDiff === "pos" && !((s.qe_diff ?? -1) > 0)) return false;
    if (fDiff === "neg" && !((s.qe_diff ?? 1) <= 0)) return false;
    if (fTr > 0 && !((s.is_translated ?? -1) > fTr)) return false;
    if (fTrMax < 1 && !((s.is_translated ?? 2) < fTrMax)) return false;
    return true;
  });
  // Ordinamento della lista secondo la tendina "ordinamento per"
  // (numero, IT, TR, EN, DIFF); i valori mancanti restano in coda.
  const sortValue = (s: (typeof allSegments)[number]): number | null => {
    switch (sortKey) {
      case "numero":
        return s.numero ?? null;
      case "it":
        return s.is_italian;
      case "tr":
        return s.is_translated;
      case "en":
        return s.is_english;
      case "diff":
        return s.qe_diff;
    }
  };
  segments.sort((a, b) => {
    const va = sortValue(a);
    const vb = sortValue(b);
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    return sortAsc ? va - vb : vb - va;
  });
  if (allSegments.length === 0) {
    // Stato vuoto: propone la segmentazione dei capitoli proposti (§5.3→§5.4).
    const nodes: StructureNode[] = structureQuery.data?.nodes ?? [];
    const segmentable = nodes.filter(
      (n) => n.kind === "chapter" || n.kind === "front_matter" || n.kind === "part"
    );
    return (
      <div className="mx-auto max-w-2xl">
        <h1 className="text-2xl font-bold text-white">
          Nessun segmento: segmenta prima un capitolo
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          I segmenti CAT (§5.4) si generano dalla struttura rilevata. Scegli un
          capitolo da segmentare, poi torna qui per tradurre.
        </p>
        {structureQuery.isLoading ? (
          <Loading label="Caricamento struttura…" />
        ) : segmentable.length === 0 ? (
          <ErrorNote message="Nessun capitolo nella struttura: importa un documento e rileva la struttura prima." />
        ) : (
          <ul className="mt-4 max-h-96 space-y-2 overflow-y-auto">
            {segmentable.map((n) => (
              <li key={n.node_id} className="flex items-center gap-3">
                <span className="flex-1 truncate text-sm text-slate-200">
                  {n.normalized_title || n.source_label}
                  <span className="ml-2 text-xs text-slate-500">
                    ({n.kind === "chapter"
                      ? "capitolo"
                      : n.kind === "part"
                      ? "parte"
                      : "preliminari"}
                    {n.status === "user_confirmed" ? " · confermato" : ""})
                  </span>
                </span>
                <button
                  onClick={() => segmentChapterMutation.mutate(n.node_id)}
                  disabled={segmentChapterMutation.isPending}
                  className="rounded bg-indigo-500/25 px-3 py-1 text-xs text-indigo-200 hover:bg-indigo-500/35 disabled:opacity-40"
                >
                  Segmenta
                </button>
              </li>
            ))}
          </ul>
        )}
        {toast && (
          <div className="mt-3 rounded bg-indigo-600 px-3 py-1 text-sm text-white">
            {toast}
          </div>
        )}
      </div>
    );
  }

  const STATUS_LABEL: Record<string, string> = {
    untranslated: "Non tradotto",
    machine_draft: "Bozza LLM",
    approved: "Approvato",
  };

  return (
    <div className="flex h-[calc(100vh-5rem)] flex-col">
      {/* Header filtri + selettore progetto */}
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 px-4 py-2">
        <label className="flex items-center gap-2 text-xs text-slate-500">
          <span>Progetto:</span>
          <select
            value={projectId}
            onChange={(e) => switchProject(e.target.value)}
            className="rounded bg-slate-800 px-2 py-1 text-xs text-white"
          >
            {(projectsQuery.data ?? []).some((p) => p.id === projectId) ? (
              (projectsQuery.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}
                </option>
              ))
            ) : (
              <option value={projectId}>…</option>
            )}
          </select>
        </label>
        <span className="text-xs font-semibold text-slate-200">
          {segments.length} segmenti
        </span>
        <span className="text-xs text-slate-500">Filtri:</span>
        <label className="flex items-center gap-1 text-xs text-slate-400">
          Sorgente
          <select
            value={fSource}
            onChange={(e) =>
              setFSource(e.target.value as typeof fSource)
            }
            className="rounded bg-slate-800 px-1 py-1 text-xs text-white"
          >
            <option value="all">Tutti</option>
            <option value="untranslated">Solo non tradotto</option>
            <option value="ocr">Solo OCR sospetto</option>
          </select>
        </label>
        <label className="flex items-center gap-1 text-xs text-slate-400">
          Target
          <select
            value={fTarget}
            onChange={(e) =>
              setFTarget(e.target.value as typeof fTarget)
            }
            className="rounded bg-slate-800 px-1 py-1 text-xs text-white"
          >
            <option value="all">Tutti</option>
            <option value="approved">Solo approvati</option>
            <option value="draft">Solo bozze</option>
            <option value="qa">Solo con note Q/A</option>
          </select>
        </label>
        <label className="flex items-center gap-1 text-xs text-slate-400">
          QE DIFF
          <select
            value={fDiff}
            onChange={(e) => setFDiff(e.target.value as typeof fDiff)}
            className="rounded bg-slate-800 px-1 py-1 text-xs text-white"
          >
            <option value="all">Tutti</option>
            <option value="pos">DIFF positivo</option>
            <option value="neg">DIFF negativo</option>
          </select>
        </label>
        <label className="flex items-center gap-1 text-xs text-slate-400">
          TR maggiore di
          <select
            value={fTr}
            onChange={(e) => setFTr(Number(e.target.value))}
            className="rounded bg-slate-800 px-1 py-1 text-xs text-white"
          >
            {[0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9].map((v) => (
              <option key={v} value={v}>
                {v.toFixed(1)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1 text-xs text-slate-400">
          TR minore di
          <select
            value={fTrMax}
            onChange={(e) => setFTrMax(Number(e.target.value))}
            className="rounded bg-slate-800 px-1 py-1 text-xs text-white"
          >
            {[1, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1].map((v) => (
              <option key={v} value={v}>
                {v.toFixed(1)}
              </option>
            ))}
          </select>
        </label>
        <span className="ml-auto flex items-center gap-4">
          <button
            onClick={() => setSearchOpen((o) => !o)}
            className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
          >
            {searchOpen ? "Nascondi ricerca" : "🔍 Cerca/sostituisci"}
          </button>
          <span className="text-xs text-slate-500">
            {segmentsQuery.data?.machine_draft ?? 0} bozza ·{" "}
            {segmentsQuery.data?.approved ?? 0} approvati
          </span>
        </span>
      </div>

      {/* Barra ricerca */}
      {searchOpen && (
        <div className="flex flex-wrap items-end gap-2 border-b border-slate-800 bg-slate-900/40 px-4 py-2">
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            <span>Cerca</span>
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="rounded bg-slate-800 px-2 py-12 text-sm text-white"
              placeholder="termine EN"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            <span>Sostituisci</span>
            <input
              value={searchReplaceWith}
              onChange={(e) => setSearchReplaceWith(e.target.value)}
              className="rounded bg-slate-800 px-2 py-12 text-sm text-white"
              placeholder="testo IT"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            <span>Scope</span>
            <select
              value={searchScope}
              onChange={(e) =>
                setSearchScope(e.target.value as typeof searchScope)
              }
              className="rounded bg-slate-800 px-2 py-12 text-sm text-white"
            >
              <option value="project">Progetto</option>
              <option value="chapter">Capitolo</option>
              <option value="segment">Segmento</option>
            </select>
          </label>
          <button
            onClick={() => searchMutation.mutate()}
            className="rounded bg-indigo-500/25 px-2 py-12 text-sm text-indigo-200"
          >
            Anteprima
          </button>
          {searchPreview && (
            <div className="mt-1 max-w-xl text-xs text-slate-300">
              <div className="flex flex-wrap items-center gap-2">
                <span>
                  <span className="text-emerald-300">{searchPreview.matches}</span>{" "}
                  occorrenze
                  {(searchPreview.readonly_matches ?? 0) > 0 && (
                    <span className="text-amber-300">
                      {" "}
                      ({searchPreview.readonly_matches} in approvati)
                    </span>
                  )}
                </span>
                {searchHits.length > 0 && (
                  <>
                    <button
                      onClick={() =>
                        setSearchHit((h) =>
                          searchHits.length
                            ? (h - 1 + searchHits.length) % searchHits.length
                            : -1
                        )
                      }
                      className="rounded bg-slate-800 px-2 py-0.5 text-slate-200 hover:bg-slate-700"
                      title="Occorrenza precedente"
                    >
                      ↑ Prec
                    </button>
                    <span className="text-slate-400">
                      {Math.min(searchHit + 1, searchHits.length)}/
                      {searchHits.length}
                    </span>
                    <button
                      onClick={() =>
                        setSearchHit((h) => (h + 1) % searchHits.length)
                      }
                      className="rounded bg-indigo-500/30 px-2 py-0.5 font-semibold text-indigo-100 hover:bg-indigo-500/45"
                      title="Occorrenza successiva (segmento aperto nell'editor)"
                    >
                      ↓ Next
                    </button>
                  </>
                )}
              </div>
              {(searchPreview.readonly_matches ?? 0) > 0 && (
                <div className="mt-1 text-amber-300">
                  Le occorrenze in segmenti approvati sono visibili ma non
                  sostituibili (§5.1 immutabilità): riporta il segmento a bozza
                  per modificarle.
                </div>
              )}
              {searchPreview.matches === 0 &&
                (searchPreview.readonly_matches ?? 0) > 0 && (
                  <div className="mt-1 text-slate-500">
                    Nessuna sostituzione applicabile: tutte le occorrenze sono
                    in segmenti approvati.
                  </div>
                )}
            </div>
          )}
        </div>
      )}

      {/* Barra azioni selezione */}
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 bg-slate-900/40 px-4 py-2">
        <label className="flex items-center gap-2 text-xs text-slate-300">
          <input
            type="checkbox"
            checked={
              segments.length > 0 &&
              segments.every((s) => selectedSegments.has(s.segment_id))
            }
            onChange={toggleAllSegments}
          />
          Seleziona tutti
        </label>
        <button
          onClick={() =>
            translateSelectionMutation.mutate([...selectedSegments])
          }
          disabled={selectedSegments.size === 0 || translateSelectionMutation.isPending}
          className="rounded bg-emerald-500/25 px-3 py-1 text-xs text-emerald-200 hover:bg-emerald-500/35 disabled:opacity-40"
        >
          {translateSelectionMutation.isPending
            ? "Avvio…"
            : `▶ Traduci selezionati (${selectedSegments.size})`}
        </button>
        <button
          onClick={() =>
            bulkStatusMutation.mutate({
              ids: [...selectedSegments],
              status: "approved",
            })
          }
          disabled={selectedSegments.size === 0 || bulkStatusMutation.isPending}
          className="rounded bg-sky-500/25 px-3 py-1 text-xs text-sky-200 hover:bg-sky-500/35 disabled:opacity-40"
        >
          ✓ Approva selezionati ({selectedSegments.size})
        </button>
        <button
          onClick={() =>
            bulkStatusMutation.mutate({
              ids: [...selectedSegments],
              status: "machine_draft",
            })
          }
          disabled={selectedSegments.size === 0 || bulkStatusMutation.isPending}
          className="rounded bg-amber-500/20 px-3 py-1 text-xs text-amber-200 hover:bg-amber-500/30 disabled:opacity-40"
        >
          ↩ In bozza selezionati ({selectedSegments.size})
        </button>
        <button
          onClick={() => verifyMutation.mutate()}
          disabled={selectedSegments.size === 0 || verifyMutation.isPending}
          title="Verifica QE sui segmenti selezionati"
          className="rounded bg-violet-500/25 px-3 py-1 text-xs text-violet-200 hover:bg-violet-500/35 disabled:opacity-40"
        >
          {verifyMutation.isPending
            ? "Verifica…"
            : `🔍 Verifica QE selezionati (${selectedSegments.size})`}
        </button>
        <button
          onClick={async () => {
            if (
              !window.confirm(
                "Annullare tutti i job in coda? I job in esecuzione finiranno il lavoro corrente."
              )
            ) {
              return;
            }
            try {
              const res = await stopQueue();
              setToast(
                `Coda fermata: ${res.cancelled} job annullati, ${res.running_left} in esecuzione`
              );
            } catch (e) {
              setToast(`Errore stop coda: ${(e as Error).message}`);
            }
          }}
          className="rounded bg-red-500/20 px-3 py-1 text-xs text-red-200 hover:bg-red-500/30"
        >
          ⏹ Cancella coda job
        </button>
        {selectedSegments.size > 0 && (
          <button
            onClick={() => setSelectedSegments(new Set())}
            className="text-xs text-slate-400 underline hover:text-slate-200"
          >
            Deseleziona tutto
          </button>
        )}
      </div>

      {/* 3 pannelli */}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        {/* Pannello sinistro: lista segmenti EN */}
        <div className="flex w-full flex-col border-r border-slate-800 md:w-2/5 lg:w-2/5">
          <div className="flex items-center justify-between border-b border-slate-800 px-3 py-1 text-xs font-semibold text-slate-400">
            <span>SORGENTE (EN)</span>
            <span className="flex items-center gap-1 font-normal">
              <select
                value={sortKey}
                onChange={(e) =>
                  setSortKey(e.target.value as typeof sortKey)
                }
                title="Ordinamento per"
                className="rounded bg-slate-800 px-1 py-0.5 text-[11px] text-white"
              >
                <option value="numero">Numero</option>
                <option value="it">IT</option>
                <option value="tr">TR</option>
                <option value="en">EN</option>
                <option value="diff">DIFF</option>
              </select>
              <button
                onClick={() => setSortAsc((v) => !v)}
                title={
                  sortAsc
                    ? "Crescente — clicca per decrescente"
                    : "Decrescente — clicca per crescente"
                }
                className="rounded bg-slate-800 px-1.5 py-0.5 text-[11px] text-slate-300 hover:bg-slate-700"
              >
                {sortAsc ? "↑↓" : "↓↑"}
              </button>
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {segments.map((seg) => {
              const hitIdx = searchHits.findIndex(
                (h) => h.segment_id === seg.segment_id
              );
              const isCurrentHit =
                hitIdx >= 0 &&
                hitIdx === Math.min(searchHit, searchHits.length - 1);
              return (
              <div
                key={seg.segment_id}
                id={`seg-row-${seg.segment_id}`}
                className={`border-b border-slate-800/60 px-2 py-1 ${
                  isCurrentHit
                    ? "bg-amber-500/15 ring-1 ring-amber-400/60"
                    : activeSegment === seg.segment_id
                    ? "bg-indigo-500/10"
                    : ""
                }`}
              >
                <div className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    checked={selectedSegments.has(seg.segment_id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggleSegment(seg.segment_id)}
                    className="mt-1"
                  />
                  <button
                    onClick={() => setActiveSegment(seg.segment_id)}
                    className={`block w-full text-left ${
                      activeSegment === seg.segment_id
                        ? ""
                        : "hover:bg-slate-800/50"
                    }`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs text-slate-500">#{seg.ordinal}</span>
                      {typeof seg.numero === "number" && (
                        <span
                          className="rounded bg-indigo-500/20 px-1.5 py-0.5 text-[11px] font-semibold text-indigo-300"
                          title="Numero progressivo unico del segmento"
                        >
                          N° {seg.numero}
                        </span>
                      )}
                      <span className="text-xs text-slate-500">
                        {STATUS_LABEL[seg.status] ?? seg.status}
                      </span>
                      {seg.qa_critical > 0 && (
                        <span className="badge bg-red-500/20 text-red-300">
                          {seg.qa_critical}
                        </span>
                      )}
                      {(seg.source_flags as { ocr_suspect?: boolean })
                        .ocr_suspect && (
                        <span className="badge bg-amber-500/20 text-amber-300">
                          OCR
                        </span>
                      )}
                      {typeof seg.is_italian === "number" && (
                        <span
                          className="badge bg-indigo-500/20 text-indigo-300"
                          title="Verifica QE: probabilita' che il testo sia in italiano"
                        >
                          IT {seg.is_italian.toFixed(2)}
                        </span>
                      )}
                      {typeof seg.is_translated === "number" && (
                        <span
                          className="badge bg-indigo-500/20 text-indigo-300"
                          title="Verifica QE: probabilità che il target sia la traduzione del sorgente"
                        >
                          TR {seg.is_translated.toFixed(2)}
                        </span>
                      )}
                      {typeof seg.is_english === "number" && (
                        <span
                          className="badge bg-indigo-500/20 text-indigo-300"
                          title="Verifica QE: probabilità che il testo sia in inglese (alta = sospetto)"
                        >
                          EN {seg.is_english.toFixed(2)}
                        </span>
                      )}
                      {typeof seg.qe_diff === "number" && (
                        <span
                          className={`badge ${
                            seg.qe_diff > 0
                              ? "bg-emerald-500/20 text-emerald-300"
                              : "bg-red-500/20 text-red-300"
                          }`}
                          title="Verifica QE: DIFF = IT − EN (zero o negativa: bozza probabilmente inglese)"
                        >
                          DIFF {seg.qe_diff >= 0 ? "+" : ""}
                          {seg.qe_diff.toFixed(2)}
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-sm text-slate-200">
                      <SearchText text={seg.source_text ?? ""} query={searchText} />
                    </p>
                  </button>
                </div>
              </div>
              );
            })}
          </div>
        </div>

        {/* Pannello centrale: target IT */}
        <div className="flex w-full flex-col border-r border-slate-800 md:w-1/3 lg:w-[35%]">
          <div className="flex items-center justify-between border-b border-slate-800 px-3 py-1 text-xs font-semibold text-slate-400">
            <span>
              TARGET (IT) ·{" "}
              {activeSegmentData
                ? STATUS_LABEL[activeSegmentData.status] ??
                  activeSegmentData.status
                : ""}
            </span>
            <button
              onClick={() => setShowVersions((v) => !v)}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
            >
              {showVersions ? "Chiudi storico" : "🕗 Storico"}
            </button>
            {activeSegmentData?.status !== "approved" && !showVersions && (
              <button
                onClick={() => {
                  if (editingTarget) {
                    // SALVA: la modifica va registrata via API (refine),
                    // altrimenti resta solo nello stato locale del browser
                    // e si perde al refresh (bug 2026-10-01, segmento 236).
                    if (!target.trim()) {
                      setToast("Il testo non può essere vuoto");
                      return;
                    }
                    refineMutation.mutate();
                  } else {
                    setEditingTarget(true);
                  }
                }}
                disabled={refineMutation.isPending}
                className={`rounded px-2 py-1 text-xs ${
                  editingTarget
                    ? "bg-emerald-500/30 text-emerald-200 hover:bg-emerald-500/40 disabled:opacity-40"
                    : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                }`}
              >
                {editingTarget
                  ? refineMutation.isPending
                    ? "Salvataggio…"
                    : "💾 Salva"
                  : "✏️ Modifica"}
              </button>
            )}
          </div>
          {showVersions ? (
            <div className="flex min-h-0 flex-1 overflow-y-auto p-3">
              {versionsQuery.isLoading ? (
                <Loading label="Caricamento versione…" />
              ) : (
                <div className="w-full space-y-2">
                  {versionsQuery.data?.history
                    .slice()
                    .reverse()
                    .map((v: SegmentVersion) => (
                      <div
                        key={v.id}
                        className="rounded border border-slate-700 bg-slate-800/50 p-2"
                      >
                        <div className="text-xs text-slate-500">
                          {v.before ? "PRECEDENTE" : "NUOVO"} · {v.action} ·{" "}
                          {v.created_at
                            ? new Date(v.created_at).toLocaleString("it-IT")
                            : ""}
                        </div>
                        <p className="mt-1 text-sm text-white">
                          {v.target_text}
                        </p>
                        {v.diff && v.diff.ops && v.diff.ops.length > 0 && (
                          <div className="mt-1 flex flex-wrap gap-0 text-sm">
                            {v.diff.ops.map((op, i) => (
                              <span
                                key={i}
                                className={
                                  op.op === "added"
                                    ? "text-emerald-300"
                                    : op.op === "removed"
                                    ? "text-red-300 line-through"
                                    : "text-slate-300"
                                }
                              >
                                {op.text}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                </div>
              )}
            </div>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col p-3">
              {editingTarget ? (
                <textarea
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                  disabled={activeSegmentData?.status === "approved"}
                  placeholder="Traduzione IT…"
                  className="min-h-60 flex-1 resize-none rounded bg-slate-800/50 px-2 py-1 text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
                />
              ) : (
                <div className="min-h-60 flex-1 overflow-y-auto rounded bg-slate-800/50 px-2 py-1 text-sm text-slate-200">
                  <QeText text={target} />
                </div>
              )}
            </div>
          )}
        </div>

        {/* Pannello destro: QA + azioni */}
        <div className="flex w-full flex-col md:w-1/3 lg:w-1/4">
          <div className="border-b border-slate-800 px-3 py-1 text-xs font-semibold text-slate-400">
            QA & AZIONI
          </div>
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-3">
            {/* Bottoni azione */}
            <div className="space-y-15">
              <button
                onClick={() => approveMutation.mutate()}
                disabled={!activeSegment}
                className="w-full rounded bg-emerald-500/25 py-1 text-sm text-emerald-200 hover:bg-emerald-500/35 disabled:opacity-40"
              >
                ✓ Approva (Ctrl+Enter)
              </button>
              <button
                onClick={() => rejectMutation.mutate()}
                disabled={!activeSegment}
                className="w-full rounded bg-red-500/20 py-1 text-sm text-red-200 hover:bg-red-500/30 disabled:opacity-40"
              >
                ✕ Rifiuta
              </button>
            </div>

            {/* Motivo / note (usato da Rifiuta e dalla modifica traduzione) */}
            <div className="mt-3">
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Motivo / note (opzionale)…"
                rows={3}
                className="w-full resize-none rounded bg-slate-800 px-2 py-1 text-xs text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
              <span className="mt-1 block text-xs text-slate-500">
                Ctrl+Enter approva
              </span>
            </div>

            {/* QA issues */}
            <div className="mt-3">
              <h3 className="text-xs font-semibold text-slate-400">
                QA issues ({qaQuery.data?.critical_unresolved ?? 0} critici)
              </h3>
              <div className="mt-1 space-y-1">
                {qaQuery.data?.issues
                  .filter((i: QaIssue) => i.segment_id === activeSegment)
                  .map((i: QaIssue) => (
                    <div
                      key={i.id}
                      className={`rounded px-2 py-1 text-xs ${
                        i.severity === "critical"
                          ? "bg-red-950/40 text-red-200"
                          : "bg-amber-950/30 text-amber-200"
                      }`}
                    >
                      <span className="font-semibold">
                        {i.severity === "critical" ? "CRITICO" : "WARN"}
                      </span>{" "}
                      {i.message}
                    </div>
                  ))}
                {(!qaQuery.data?.issues ||
                  qaQuery.data.issues.filter(
                    (i: QaIssue) => i.segment_id === activeSegment
                  ).length === 0) && (
                  <p className="text-xs text-slate-500">
                    Nessun QA issue per questo segmento.
                  </p>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div className="fixed bottom-4 left-1/2 z-10 -translate-x-1/2 rounded bg-emerald-600 px-3 py-1 text-sm text-white shadow-lg">
          {toast}
        </div>
      )}
    </div>
  );
}
