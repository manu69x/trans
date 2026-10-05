"use client";

/**
 * Import e struttura (PRD §11.1, §5.3, §12.2, §15.1).
 *
 * Layout a due pannelli sincronizzati per pagina: a sinistra il PDF
 * originale (PdfViewer: blob locale scaricato con authFetch — il viewer
 * nativo non può inviare l'header Authorization), a destra il testo
 * estratto della stessa pagina (§15.1: testo per pagina con confidenza e
 * flag OCR sospetto). Accanto l'albero parti/capitoli/scene con confidence
 * e detection_method e le operazioni dell'editor visuale §5.3 (creare,
 * unire, dividere, spostare, rinominare, correzione confini) con undo;
 * il pannello di pulizia header/footer con preview/apply/rollback (§5.3).
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { ErrorNote, Loading, formatBytes } from "@/components/ui";
import PdfViewer from "@/components/pdf-viewer";
import {
  ApiError,
  applyCleaning,
  confirmNode,
  createNode,
  deleteNode,
  detectStructure,
  getCleaningPreview,
  getDocumentPage,
  getProject,
  getStructure,
  listDocuments,
  listProjects,
  mergeNode,
  moveNode,
  patchNode,
  renameNode,
  resegmentStructure,
  rollbackCleaning,
  setBoundary,
  splitNode,
  undoStructureEdit,
  type CleaningPreview,
  type DocumentInfo,
  type DocumentPage,
  type StructureNode as StructureNodeDto,
} from "@/lib/api";

const KIND_LABEL_IT: Record<string, string> = {
  front_matter: "Preliminari",
  part: "Parte",
  chapter: "Capitolo",
  scene: "Scena",
  back_matter: "Appendice",
  footnote: "Nota",
};

const METHOD_LABEL_IT: Record<string, string> = {
  pdf_toc: "segnalibro PDF",
  toc_page: "indice",
  font: "tipografia",
  regex: "pattern",
  narrative: "narrativo",
  user: "manuale",
};

function kindLabel(kind: string): string {
  return KIND_LABEL_IT[kind] ?? kind;
}

function methodLabel(method: string): string {
  return METHOD_LABEL_IT[method] ?? method;
}

/** Albero §5.3: i nodi scena (con parent_id) annidati sotto il capitolo. */
function buildTree(nodes: StructureNodeDto[]): TreeNode[] {
  const chapters = nodes
    .filter((n) => !n.parent_id)
    .sort((a, b) => (a.ordinal ?? 0) - (b.ordinal ?? 0));
  return chapters.map((node) => ({
    node,
    scenes: nodes
      .filter((n) => n.parent_id === node.node_id)
      .sort((a, b) => (a.ordinal ?? 0) - (b.ordinal ?? 0)),
  }));
}

interface TreeNode {
  node: StructureNodeDto;
  scenes: StructureNodeDto[];
}

export default function ImportPage() {
  const queryClient = useQueryClient();

  const projectsQuery = useQuery({
    queryKey: ["progetti"],
    queryFn: listProjects,
  });
  const [projectId, setProjectId] = useState<string | null>(null);
  useEffect(() => {
    if (!projectId && projectsQuery.data?.length) {
      setProjectId(projectsQuery.data[0].id);
    }
  }, [projectsQuery.data, projectId]);

  const projectQuery = useQuery({
    queryKey: ["progetto", projectId],
    queryFn: () => getProject(projectId as string),
    enabled: Boolean(projectId),
  });

  const documentsQuery = useQuery({
    queryKey: ["documenti", projectId],
    queryFn: () => listDocuments(projectId as string),
    enabled: Boolean(projectId),
  });
  const [documentId, setDocumentId] = useState<string | null>(null);
  useEffect(() => {
    if (documentsQuery.data?.length) {
      if (!documentId || !documentsQuery.data.some((d) => d.id === documentId)) {
        setDocumentId(documentsQuery.data[0].id);
      }
    } else if (documentsQuery.isSuccess) {
      setDocumentId(null);
    }
  }, [documentsQuery.data, documentsQuery.isSuccess, documentId]);

  const document = useMemo<DocumentInfo | null>(
    () => documentsQuery.data?.find((d) => d.id === documentId) ?? null,
    [documentsQuery.data, documentId]
  );

  const structureQuery = useQuery({
    queryKey: ["struttura", projectId],
    queryFn: () => getStructure(projectId as string),
    enabled: Boolean(projectId),
  });

  const [pageNumber, setPageNumber] = useState(1);
  const totalPages = document?.page_count ?? 0;
  useEffect(() => {
    if (totalPages && pageNumber > totalPages) setPageNumber(totalPages);
  }, [totalPages, pageNumber]);

  // PDF e testo restano sulla stessa pagina: il pannello testo consuma
  // esattamente il page_number mostrato nel viewer (AC2, §11.1).
  const pageQuery = useQuery({
    queryKey: ["pagina", documentId, pageNumber],
    queryFn: () =>
      getDocumentPage(projectId as string, documentId as string, pageNumber),
    enabled: Boolean(projectId && documentId),
  });

  const cleaningQuery = useQuery({
    queryKey: ["pulizia", projectId, documentId],
    queryFn: () =>
      getCleaningPreview(projectId as string, documentId as string),
    enabled: Boolean(projectId && documentId),
  });

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [splitPage, setSplitPage] = useState<string>("");
  const [newNode, setNewNode] = useState({
    label: "",
    start: "",
    end: "",
    kind: "chapter",
  });
  const [checkedKeys, setCheckedKeys] = useState<string[]>([]);

  const selected = useMemo<StructureNodeDto | null>(
    () =>
      structureQuery.data?.nodes.find((n) => n.node_id === selectedId) ?? null,
    [structureQuery.data, selectedId]
  );

  // il capitolo selezionato guida il viewer: la pagina richiesta deve
  // cadere nel range del capitolo
  useEffect(() => {
    if (selected?.start_page) {
      setPageNumber(selected.start_page);
    }
  }, [selected?.node_id, selected?.start_page]);

  async function run(action: () => Promise<string | void>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const message = await action();
      if (message) setNotice(message);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["struttura", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["pulizia", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["lavori", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["pagina"] }),
      ]);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Operazione non riuscita"
      );
    } finally {
      setBusy(false);
    }
  }

  const tree = useMemo(
    () => buildTree(structureQuery.data?.nodes ?? []),
    [structureQuery.data]
  );

  if (projectsQuery.isLoading) {
    return <Loading label="Caricamento progetti…" />;
  }
  if (projectsQuery.isError) {
    return <ErrorNote message="Impossibile caricare i progetti." />;
  }

  return (
    <div className="mx-auto max-w-7xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Import e struttura</h1>
          <p className="mt-1 text-sm text-slate-400">
            Viewer PDF + testo estratto sincronizzati, albero capitoli e
            correzione confini (PRD §11.1, §5.3).
          </p>
        </div>
        <div className="flex items-end gap-2">
          <div>
            <label className="label" htmlFor="import-project">
              Progetto
            </label>
            <select
              id="import-project"
              className="input min-w-56"
              value={projectId ?? ""}
              onChange={(e) => setProjectId(e.target.value || null)}
            >
              {(projectsQuery.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="import-document">
              Documento
            </label>
            <select
              id="import-document"
              className="input min-w-56"
              value={documentId ?? ""}
              onChange={(e) => setDocumentId(e.target.value || null)}
            >
              {(documentsQuery.data ?? []).length === 0 && (
                <option value="">Nessun documento</option>
              )}
              {(documentsQuery.data ?? []).map((d) => (
                <option key={d.id} value={d.id}>
                  {d.filename}
                </option>
              ))}
            </select>
          </div>
        </div>
      </header>

      {!projectId && (
        <div className="mt-6">
          <ErrorNote message="Nessun progetto presente: creane uno da Progetti." />
        </div>
      )}

      {projectId && (
        <>
          {(error || notice) && (
            <div className="mt-4 space-y-2">
              {error && <ErrorNote message={error} />}
              {notice && (
                <div
                  className="rounded-md border border-emerald-800 bg-emerald-950/40 px-4 py-3 text-sm text-emerald-300"
                  role="status"
                >
                  {notice}
                </div>
              )}
            </div>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn-secondary"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  const res = await detectStructure(projectId);
                  return `Rilevamento struttura avviato (job ${res.job_id.slice(0, 8)}…).`;
                })
              }
            >
              Rileva struttura
            </button>
            <button
              type="button"
              className="btn-secondary"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  const res = await resegmentStructure(projectId);
                  return `Risegmentazione selettiva avviata (job ${res.job_id.slice(0, 8)}…).`;
                })
              }
              title="Rigenera solo i segmenti dei capitoli il cui confine è cambiato (§15.1)"
            >
              Risegmenta dipendenti
            </button>
            <button
              type="button"
              className="btn-secondary"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await undoStructureEdit(projectId);
                  return "Ultima modifica annullata.";
                })
              }
            >
              Annulla modifica (undo)
            </button>
            {projectQuery.data && (
              <span className="ml-auto text-xs text-slate-500">
                Stato progetto: {projectQuery.data.status}
              </span>
            )}
          </div>

          {/* --- viewer sincronizzato (AC2) ------------------------------ */}
          {document ? (
            <section className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
              <div className="card">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <h2 className="text-sm font-semibold text-slate-200">
                    PDF originale
                  </h2>
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      className="btn-secondary px-2 py-1 text-xs"
                      disabled={pageNumber <= 1 || busy}
                      onClick={() => setPageNumber((p) => Math.max(1, p - 1))}
                    >
                      ←
                    </button>
                    <span className="min-w-20 text-center text-xs text-slate-400">
                      Pag. {pageNumber}
                      {totalPages ? ` / ${totalPages}` : ""}
                    </span>
                    <button
                      type="button"
                      className="btn-secondary px-2 py-1 text-xs"
                      disabled={Boolean(totalPages) && pageNumber >= totalPages}
                      onClick={() =>
                        setPageNumber((p) => p + 1)
                      }
                    >
                      →
                    </button>
                  </div>
                </div>
                <PdfViewer
                  projectId={projectId}
                  documentId={documentId}
                  filename={document.filename}
                  pageNumber={pageNumber}
                  className="h-[520px] w-full rounded-md border border-slate-800 bg-slate-950"
                />
              </div>

              <div className="card flex flex-col">
                <div className="mb-2 flex items-center justify-between">
                  <h2 className="text-sm font-semibold text-slate-200">
                    Testo estratto — pagina {pageNumber}
                  </h2>
                  {pageQuery.data?.ocr_suspect && (
                    <span className="badge bg-amber-500/20 text-amber-300">
                      OCR sospetto
                    </span>
                  )}
                </div>
                {pageQuery.isLoading && <Loading label="Caricamento pagina…" />}
                {pageQuery.isError && (
                  <ErrorNote message="Pagina non disponibile (estrazione L1/OCR non ancora eseguita?)." />
                )}
                {pageQuery.data && (
                  <>
                    <pre className="max-h-[480px] flex-1 overflow-auto whitespace-pre-wrap rounded-md border border-slate-800 bg-slate-950 p-3 text-sm text-slate-300">
                      {pageQuery.data.normalized_text?.trim() ||
                        "(pagina senza testo estratto)"}
                    </pre>
                    <p className="mt-2 text-xs text-slate-500">
                      Livello: {pageQuery.data.level ?? "—"} · Confidenza:{" "}
                      {pageQuery.data.confidence != null
                        ? (pageQuery.data.confidence * 100).toFixed(1) + "%"
                        : "—"}
                    </p>
                  </>
                )}
              </div>
            </section>
          ) : (
            documentsQuery.isSuccess && (
              <div className="mt-4">
                <ErrorNote message="Nessun documento caricato su questo progetto." />
              </div>
            )
          )}

          <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
            {/* --- albero struttura (§5.3) -------------------------------- */}
            <section className="card xl:col-span-2">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-200">
                  Struttura del libro
                </h2>
                <span className="text-xs text-slate-500">
                  {structureQuery.data
                    ? `${structureQuery.data.user_confirmed} confermati · ${structureQuery.data.proposed} proposti`
                    : "—"}
                </span>
              </div>
              {structureQuery.isLoading && <Loading />}
              {structureQuery.isError && (
                <ErrorNote message="Impossibile caricare la struttura." />
              )}
              {structureQuery.isSuccess &&
                structureQuery.data.nodes.length === 0 && (
                  <p className="py-6 text-center text-sm text-slate-400">
                    Nessun nodo: esegui il rilevamento della struttura.
                  </p>
                )}
              <ul className="space-y-1">
                {tree.map(({ node, scenes }) => (
                  <li key={node.node_id}>
                    <NodeRow
                      node={node}
                      selected={selectedId === node.node_id}
                      onSelect={() =>
                        setSelectedId(node.node_id === selectedId ? null : node.node_id)
                      }
                      onPageJump={(p) => setPageNumber(p)}
                    />
                    {scenes.length > 0 && (
                      <ul className="ml-6 border-l border-slate-800 pl-3">
                        {scenes.map((scene) => (
                          <li key={scene.node_id}>
                            <NodeRow
                              node={scene}
                              selected={selectedId === scene.node_id}
                              onSelect={() =>
                                setSelectedId(
                                  scene.node_id === selectedId ? null : scene.node_id
                                )
                              }
                              onPageJump={(p) => setPageNumber(p)}
                            />
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>

              {/* --- operazioni sul nodo selezionato (§5.3) --------------- */}
              {selected && (
                <div className="mt-4 rounded-md border border-slate-800 bg-slate-950/60 p-3">
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                    {kindLabel(selected.kind)} selezionato ·{" "}
                    {selected.source_label ?? "(senza titolo)"}
                  </p>
                  <div className="mt-3 flex flex-wrap items-end gap-2">
                    <button
                      type="button"
                      className="btn-secondary px-3 py-1 text-xs"
                      disabled={busy || selected.kind === "scene"}
                      onClick={() =>
                        run(async () => {
                          await confirmNode(projectId, selected.node_id);
                          return "Nodo confermato.";
                        })
                      }
                    >
                      Conferma
                    </button>
                    <button
                      type="button"
                      className="btn-secondary px-3 py-1 text-xs"
                      disabled={busy}
                      onClick={() => {
                        setRenamingId(selected.node_id);
                        setRenameValue(selected.source_label ?? "");
                      }}
                    >
                      Rinomina
                    </button>
                    <button
                      type="button"
                      className="btn-secondary px-3 py-1 text-xs"
                      disabled={busy || selected.kind === "scene"}
                      onClick={() =>
                        run(async () => {
                          await moveNode(projectId, selected.node_id, "up");
                          return "Capitolo spostato sopra.";
                        })
                      }
                    >
                      ↑ Sposta su
                    </button>
                    <button
                      type="button"
                      className="btn-secondary px-3 py-1 text-xs"
                      disabled={busy || selected.kind === "scene"}
                      onClick={() =>
                        run(async () => {
                          await moveNode(projectId, selected.node_id, "down");
                          return "Capitolo spostato sotto.";
                        })
                      }
                    >
                      ↓ Sposta giù
                    </button>
                    {selected.kind !== "scene" && (
                      <div className="flex items-end gap-1">
                        <div>
                          <label
                            className="mb-1 block text-[11px] text-slate-500"
                            htmlFor="split-page"
                          >
                            Dividi a pagina
                          </label>
                          <input
                            id="split-page"
                            className="input w-24 px-2 py-1 text-xs"
                            value={splitPage}
                            onChange={(e) => setSplitPage(e.target.value)}
                            placeholder={
                              selected.start_page != null
                                ? String(selected.start_page + 1)
                                : ""
                            }
                          />
                        </div>
                        <button
                          type="button"
                          className="btn-secondary px-3 py-1 text-xs"
                          disabled={busy || !Number(splitPage)}
                          onClick={() =>
                            run(async () => {
                              await splitNode(
                                projectId,
                                selected.node_id,
                                Number(splitPage)
                              );
                              setSplitPage("");
                              return "Capitolo diviso.";
                            })
                          }
                        >
                          Dividi
                        </button>
                      </div>
                    )}
                    {selected.kind !== "scene" && (
                      <button
                        type="button"
                        className="btn-secondary px-3 py-1 text-xs"
                        disabled={busy}
                        title="Unisci col capitolo precedente"
                        onClick={() =>
                          run(async () => {
                            const nodes = structureQuery.data?.nodes ?? [];
                            const chapters = nodes.filter(
                              (n) => n.kind !== "scene"
                            );
                            const idx = chapters.findIndex(
                              (n) => n.node_id === selected.node_id
                            );
                            if (idx <= 0) {
                              throw new ApiError(
                                409,
                                "Nessun capitolo precedente da unire."
                              );
                            }
                            await mergeNode(
                              projectId,
                              selected.node_id,
                              chapters[idx - 1].node_id
                            );
                            setSelectedId(null);
                            return "Capitolo unito col precedente.";
                          })
                        }
                      >
                        Unisci col precedente
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn px-3 py-1 text-xs text-red-300 hover:bg-red-950/40"
                      disabled={busy}
                      onClick={() =>
                        run(async () => {
                          await deleteNode(projectId, selected.node_id);
                          setSelectedId(null);
                          return "Nodo eliminato (annullabile con undo).";
                        })
                      }
                    >
                      Elimina
                    </button>
                  </div>
                  {renamingId === selected.node_id && (
                    <div className="mt-3 flex items-end gap-2">
                      <div className="flex-1">
                        <label
                          className="mb-1 block text-[11px] text-slate-500"
                          htmlFor="rename-input"
                        >
                          Nuova etichetta
                        </label>
                        <input
                          id="rename-input"
                          className="input"
                          value={renameValue}
                          onChange={(e) => setRenameValue(e.target.value)}
                        />
                      </div>
                      <button
                        type="button"
                        className="btn-primary px-3 py-1 text-xs"
                        disabled={busy || !renameValue.trim()}
                        onClick={() =>
                          run(async () => {
                            await renameNode(
                              projectId,
                              selected.node_id,
                              renameValue.trim()
                            );
                            setRenamingId(null);
                            return "Nodo rinominato.";
                          })
                        }
                      >
                        Salva
                      </button>
                      <button
                        type="button"
                        className="btn-secondary px-3 py-1 text-xs"
                        onClick={() => setRenamingId(null)}
                      >
                        Annulla
                      </button>
                    </div>
                  )}
                </div>
              )}

              {/* --- crea nodo manuale (§5.3 "creare") -------------------- */}
              <div className="mt-4 rounded-md border border-dashed border-slate-800 p-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                  Nuovo nodo manuale
                </p>
                <div className="mt-2 flex flex-wrap items-end gap-2">
                  <div>
                    <label className="mb-1 block text-[11px] text-slate-500" htmlFor="new-kind">
                      Tipo
                    </label>
                    <select
                      id="new-kind"
                      className="input w-36 px-2 py-1 text-xs"
                      value={newNode.kind}
                      onChange={(e) =>
                        setNewNode((s) => ({ ...s, kind: e.target.value }))
                      }
                    >
                      <option value="chapter">Capitolo</option>
                      <option value="part">Parte</option>
                      <option value="front_matter">Preliminari</option>
                      <option value="back_matter">Appendice</option>
                      <option value="footnote">Nota</option>
                    </select>
                  </div>
                  <div className="flex-1">
                    <label className="mb-1 block text-[11px] text-slate-500" htmlFor="new-label">
                      Etichetta
                    </label>
                    <input
                      id="new-label"
                      className="input"
                      value={newNode.label}
                      onChange={(e) =>
                        setNewNode((s) => ({ ...s, label: e.target.value }))
                      }
                      placeholder="CHAPTER 12"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-[11px] text-slate-500" htmlFor="new-start">
                      Pagina inizio
                    </label>
                    <input
                      id="new-start"
                      className="input w-24 px-2 py-1 text-xs"
                      value={newNode.start}
                      onChange={(e) =>
                        setNewNode((s) => ({ ...s, start: e.target.value }))
                      }
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-[11px] text-slate-500" htmlFor="new-end">
                      Pagina fine
                    </label>
                    <input
                      id="new-end"
                      className="input w-24 px-2 py-1 text-xs"
                      value={newNode.end}
                      onChange={(e) =>
                        setNewNode((s) => ({ ...s, end: e.target.value }))
                      }
                    />
                  </div>
                  <button
                    type="button"
                    className="btn-primary px-3 py-1 text-xs"
                    disabled={
                      busy ||
                      !newNode.label.trim() ||
                      !Number(newNode.start)
                    }
                    onClick={() =>
                      run(async () => {
                        await createNode(projectId, {
                          kind: newNode.kind,
                          source_label: newNode.label.trim(),
                          start_page: Number(newNode.start),
                          end_page: newNode.end
                            ? Number(newNode.end)
                            : Number(newNode.start),
                        });
                        setNewNode({ label: "", start: "", end: "", kind: "chapter" });
                        return "Nodo creato (confermato, annullabile con undo).";
                      })
                    }
                  >
                    Crea
                  </button>
                </div>
              </div>
            </section>

            {/* --- pulizia header/footer + OCR (§5.3, §15.1) --------------- */}
            <section className="card">
              <h2 className="text-sm font-semibold text-slate-200">
                Pulizia header/footer
              </h2>
              <p className="mt-1 text-xs text-slate-500">
                Le righe ripetute sono eliminabili solo dopo conferma
                algoritmica (§5.3); ogni applicazione conserva lo snapshot per
                il rollback.
              </p>
              {cleaningQuery.isLoading && <Loading />}
              {cleaningQuery.isError && (
                <div className="mt-3">
                  <ErrorNote message="Preview di pulizia non disponibile per questo documento." />
                </div>
              )}
              {cleaningQuery.data && (
                <CleaningPanel
                  preview={cleaningQuery.data}
                  checked={checkedKeys}
                  onToggle={(key, checkedState) =>
                    setCheckedKeys((keys) =>
                      checkedState
                        ? [...keys, key]
                        : keys.filter((k) => k !== key)
                    )
                  }
                  busy={busy}
                  onApply={() =>
                    run(async () => {
                      await applyCleaning(projectId, documentId as string, checkedKeys);
                      setCheckedKeys([]);
                      return "Pulizia applicata (rollback disponibile).";
                    })
                  }
                  onRollback={() =>
                    run(async () => {
                      await rollbackCleaning(projectId, documentId as string);
                      setCheckedKeys([]);
                      return "Testo ripristinato dallo snapshot.";
                    })
                  }
                />
              )}

              <h3 className="mt-6 text-sm font-semibold text-slate-200">
                Pagina corrente
              </h3>
              {pageQuery.data?.ocr_suspect ? (
                <div className="mt-2">
                  <ErrorNote message="Questa pagina è marcata OCR sospetto: verifica il testo prima di tradurla." />
                </div>
              ) : (
                <p className="mt-2 text-xs text-slate-500">
                  Nessun problema OCR segnalato sulla pagina {pageNumber}.
                </p>
              )}
              {selected && selected.start_page != null && (
                <BoundaryEditor
                  projectId={projectId as string}
                  node={selected}
                  busy={busy}
                  onBoundary={(pages) =>
                    run(async () => {
                      await setBoundary(projectId, selected.node_id, pages);
                      return "Confine corretto: esegui «Risegmenta dipendenti» per rigenerare i soli segmenti toccati (§15.1).";
                    })
                  }
                />
              )}
            </section>
          </div>
        </>
      )}
    </div>
  );
}

/** Riga dell'albero: titolo, pagine, confidence e detection_method (§5.3). */
function NodeRow({
  node,
  selected,
  onSelect,
  onPageJump,
}: {
  node: StructureNodeDto;
  selected: boolean;
  onSelect: () => void;
  onPageJump: (page: number) => void;
}) {
  const confidencePct =
    node.confidence != null ? Math.round(node.confidence * 100) : null;
  return (
    <div
      className={`group flex cursor-pointer items-center justify-between gap-2 rounded px-2 py-1.5 text-sm ${
        selected ? "bg-indigo-500/15 text-white" : "text-slate-300 hover:bg-slate-800/50"
      }`}
      onClick={onSelect}
    >
      <span className="flex min-w-0 items-center gap-2">
        <span className="badge bg-slate-700 text-slate-300">
          {kindLabel(node.kind)}
        </span>
        <span className="truncate">
          {node.normalized_title ?? node.source_label ?? "(senza titolo)"}
        </span>
        {node.status === "user_confirmed" ? (
          <span className="badge bg-emerald-500/15 text-emerald-300">confermato</span>
        ) : (
          <span className="badge bg-violet-500/15 text-violet-300">proposto</span>
        )}
      </span>
      <span className="flex shrink-0 items-center gap-2 text-xs text-slate-500">
        {node.start_page != null && (
          <button
            type="button"
            className="underline decoration-dotted hover:text-indigo-300"
            onClick={(e) => {
              e.stopPropagation();
              onPageJump(node.start_page as number);
            }}
            title="Vai alla pagina nel viewer"
          >
            pp. {node.start_page}
            {node.end_page != null && node.end_page !== node.start_page
              ? `–${node.end_page}`
              : ""}
          </button>
        )}
        {confidencePct != null && <span>{confidencePct}%</span>}
        <span className="flex gap-1">
          {(node.detection_method ?? []).map((m) => (
            <span
              key={m}
              className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400"
            >
              {methodLabel(m)}
            </span>
          ))}
        </span>
      </span>
    </div>
  );
}

/** Editor del confine del nodo selezionato (§15.1). */
function BoundaryEditor({
  projectId,
  node,
  busy,
  onBoundary,
}: {
  projectId: string;
  node: StructureNodeDto;
  busy: boolean;
  onBoundary: (pages: { start_page?: number | null; end_page?: number | null }) => void;
}) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  useEffect(() => {
    setStart(node.start_page != null ? String(node.start_page) : "");
    setEnd(node.end_page != null ? String(node.end_page) : "");
  }, [node.node_id, node.start_page, node.end_page]);
  return (
    <div className="mt-4 rounded-md border border-slate-800 bg-slate-950/60 p-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">
        Correzione confine · {node.normalized_title ?? node.source_label}
      </p>
      <div className="mt-2 flex items-end gap-2">
        <div>
          <label className="mb-1 block text-[11px] text-slate-500" htmlFor="boundary-start">
            Inizio
          </label>
          <input
            id="boundary-start"
            className="input w-20 px-2 py-1 text-xs"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-[11px] text-slate-500" htmlFor="boundary-end">
            Fine
          </label>
          <input
            id="boundary-end"
            className="input w-20 px-2 py-1 text-xs"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </div>
        <button
          type="button"
          className="btn-primary px-3 py-1 text-xs"
          disabled={busy || (!start && !end)}
          onClick={() =>
            onBoundary({
              start_page: start ? Number(start) : null,
              end_page: end ? Number(end) : null,
            })
          }
        >
          Applica confine
        </button>
      </div>
    </div>
  );
}

/** Pannello candidati header/footer: checkbox + apply/rollback (§5.3). */
function CleaningPanel({
  preview,
  checked,
  onToggle,
  busy,
  onApply,
  onRollback,
}: {
  preview: CleaningPreview;
  checked: string[];
  onToggle: (key: string, checked: boolean) => void;
  busy: boolean;
  onApply: () => void;
  onRollback: () => void;
}) {
  const candidates = preview.candidates ?? [];
  if (candidates.length === 0 && !preview.rollback_available) {
    return (
      <p className="mt-3 text-xs text-slate-500">
        Nessuna riga ripetuta rilevata su {preview.total_pages} pagine.
      </p>
    );
  }
  return (
    <div className="mt-3 space-y-2">
      <ul className="space-y-1">
        {candidates.map((c) => {
          const checkedState = checked.includes(c.text);
          return (
            <li
              key={c.text}
              className="flex items-center gap-2 rounded border border-slate-800 px-2 py-1.5 text-xs"
            >
              <input
                type="checkbox"
                className="accent-indigo-500"
                checked={checkedState}
                disabled={!c.algorithmically_confirmed}
                onChange={(e) => onToggle(c.text, e.target.checked)}
                aria-label={`Elimina "${c.text}"`}
              />
              <span className="min-w-0 flex-1 truncate text-slate-300" title={c.text}>
                {c.text}
              </span>
              <span className="shrink-0 text-slate-500">
                {c.pages ?? "?"} pag.
              </span>
              {c.algorithmically_confirmed ? (
                <span className="badge bg-emerald-500/10 text-emerald-400">
                  confermata
                </span>
              ) : (
                <span className="badge bg-slate-700/50 text-slate-400">
                  non confermata
                </span>
              )}
            </li>
          );
        })}
      </ul>
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="btn-primary px-3 py-1 text-xs"
          disabled={busy || checked.length === 0}
          onClick={onApply}
        >
          Elimina selezionate
        </button>
        <button
          type="button"
          className="btn-secondary px-3 py-1 text-xs"
          disabled={busy || !preview.rollback_available}
          onClick={onRollback}
        >
          Rollback
        </button>
      </div>
      {preview.applied.length > 0 && (
        <p className="text-[11px] text-slate-500">
          Applicate: {preview.applied.join(", ")}
        </p>
      )}
    </div>
  );
}
