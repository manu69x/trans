"use client";

/**
 * Viewer PDF (§11.1 pannello viewer).
 *
 * L'<object>/<iframe> del browser NON può inviare l'header Authorization:
 * da quando le API richiedono il token (§2.1) l'URL diretto /documents/.../file
 * risponde 401 dentro il viewer. Questo componente scarica il PDF con
 * authFetch (token incluso, con refresh automatico) e lo serve al viewer da
 * un blob URL locale: nessun byte lascia la macchina (§13.1).
 */

import { useEffect, useState } from "react";

import { authFetch } from "@/lib/api";

interface PdfViewerProps {
  projectId: string | null;
  documentId: string | null;
  filename: string;
  pageNumber?: number;
  className?: string;
}

export default function PdfViewer({
  projectId,
  documentId,
  filename,
  pageNumber,
  className,
}: PdfViewerProps) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId || !documentId) return;
    let alive = true;
    let revoke: string | null = null;
    setUrl(null);
    setError(null);
    (async () => {
      try {
        const res = await authFetch(
          `/api/v1/projects/${projectId}/documents/${documentId}/file`
        );
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const blob = await res.blob();
        const u = URL.createObjectURL(blob);
        revoke = u;
        if (alive) setUrl(u);
      } catch (e) {
        if (alive) {
          setError(e instanceof Error ? e.message : "caricamento fallito");
        }
      }
    })();
    return () => {
      alive = false;
      if (revoke) URL.revokeObjectURL(revoke);
    };
  }, [projectId, documentId]);

  if (!projectId || !documentId) return null;

  if (error) {
    return (
      <div
        className={`${className ?? ""} flex items-center justify-center p-4`}
      >
        <p className="text-sm text-slate-400">
          PDF non caricabile ({error}): riprova o ricarica la pagina.
        </p>
      </div>
    );
  }
  if (!url) {
    return (
      <div
        className={`${className ?? ""} flex items-center justify-center p-4`}
      >
        <p className="text-sm text-slate-500">Caricamento PDF…</p>
      </div>
    );
  }
  return (
    <object
      key={pageNumber ?? 1}
      data={pageNumber ? `${url}#page=${pageNumber}` : url}
      type="application/pdf"
      className={className}
      aria-label={`PDF del documento ${filename}`}
    >
      <p className="p-4 text-sm text-slate-400">
        Il browser non mostra il PDF incorporato:{" "}
        <a className="text-indigo-300 underline" href={url} target="_blank" rel="noreferrer">
          apri il file
        </a>
        .
      </p>
    </object>
  );
}
