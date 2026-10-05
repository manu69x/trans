/**
 * Client API verso il backend Trans (T09) e il LLM Gateway.
 *
 * Il browser parla solo con la propria origine (PRD §13.1 local_only):
 * `/api/v1/*` è rewritato dal server Next verso il backend FastAPI e
 * `/api/gateway/*` è inoltrato dal route handler verso Gateway. Nessuna
 * configurazione CORS e nessun host interno esposto al client.
 */

import type {
  Job,
  GatewayModelsResponse,
  GatewayModel,
  PlanResponse,
  Project,
  Entity,
  EntityEvidence,
  EntityListResponse,
  ChapterEntitiesResponse,
  EntityVersionsResponse,
} from "./api-types";
import { getAccessToken, refreshAccessToken } from "./auth";

// Ri-esportati per il client (che importa i tipi dal modulo api).
export type { Entity, EntityEvidence, Project };

/** Base API relativa all'origine Next (proxy server-side verso il backend). */
export const API_BASE = "";
const API = "/api/v1";
/**
 * Backend DIRETTO per le richieste LUNGHE (azioni massive LLM): il proxy
 * rewrite di Next.js tronca le risposte a ~30s ("socket hang up"); chiamando
 * http://<host>:8000 direttamente il browser evita il limite. Funziona solo
 * con backend raggiungibile dal browser (stack local-only, CORS abilitato).
 * In LAN l'utente usa http://<ip-host>:3002 -> sostituiamo l'host corrente.
 */
function longApi(): string {
  if (typeof window === "undefined") return API; // SSR: usa il proxy
  return `${window.location.protocol}//${window.location.hostname}:8000/api/v1`;
}

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

async function handle<T>(res: Response | Promise<Response>): Promise<T> {
  const r = await res;
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = (await r.json()) as { detail?: unknown };
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* corpo non JSON: usa lo status text */
    }
    throw new ApiError(r.status, detail);
  }
  return (await r.json()) as T;
}

/** Riferimento al fetch globale (authFetch non deve ricadere su se stesso). */
const rawFetch: typeof fetch = (...args) => fetch(...args);

/**
 * fetch con sessione (PRD §2.1): allega l'access token e, su 401, prova un
 * singolo refresh del token e ripete la richiesta. Se il refresh fallisce la
 * sessione è scaduta: si torna al login. Le rotte /auth/* non passano da qui
 * (aperte per definizione).
 */
export async function authFetch(
  input: string,
  init?: RequestInit
): Promise<Response> {
  const buildInit = (token: string | null): RequestInit => {
    const headers = new Headers(init?.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return { ...init, headers };
  };
  let res = await rawFetch(input, buildInit(getAccessToken()));
  const isAuthRoute = input.includes("/auth/");
  // AUTH_DISABLED (2026-10-01, portale senza login): il backend dichiara la
  // modalità aperta in /health; in quel caso un 401 non redirige al login,
  // la richiesta viene riprovata senza header (l'utente anonimo è admin).
  if (res.status === 401 && !isAuthRoute && typeof window !== "undefined") {
    if (authDisabled()) {
      res = await rawFetch(input, buildInit(null));
      return res;
    }
    const renewed = await refreshAccessToken();
    if (renewed) {
      res = await rawFetch(input, buildInit(getAccessToken()));
    } else {
      window.location.href = "/login";
    }
  }
  return res;
}

// Cache della modalità portale aperto (leggita una sola volta da /health).
let _authDisabled: boolean | null = null;

/** True quando il backend gira con AUTH_DISABLED=1 (nessun login richiesto). */
export function authDisabled(): boolean {
  if (typeof window === "undefined") return false;
  if (_authDisabled === null) {
    // Lettura sincrona lazy: popolata da probeAuthMode() al bootstrap.
    return sessionStorage.getItem("trans.auth_disabled") === "1";
  }
  return _authDisabled;
}

/** Interroga /health e memorizza la modalità auth (chiamare al bootstrap). */
export async function probeAuthMode(): Promise<boolean> {
  if (typeof window === "undefined") return false;
  try {
    // NB: sotto /api/v1 (il rewrite Next mappa SOLO /api/v1/*; /health di
    // root non è raggiungibile dal browser e il probe takeva 404 ->
    // redirect al login nonostante AUTH_DISABLED, fix 2026-10-01).
    const r = await rawFetch(`${API}/health`, { cache: "no-store" });
    if (r.ok) {
      const body = (await r.json()) as { auth_disabled?: boolean };
      _authDisabled = Boolean(body.auth_disabled);
      sessionStorage.setItem("trans.auth_disabled", _authDisabled ? "1" : "0");
      return _authDisabled;
    }
  } catch {
    /* fallback: modalità classica */
  }
  _authDisabled = false;
  return false;
}

function jsonHeaders(): HeadersInit {
  return { "Content-Type": "application/json" };
}

// --- Progetti (PRD §12.1) ---------------------------------------------------

export async function listProjects(): Promise<Project[]> {
  const res = await authFetch(`${API}/projects`, { cache: "no-store" });
  return handle<Project[]>(res);
}

export async function getProject(projectId: string): Promise<Project> {
  const res = await authFetch(`${API}/projects/${projectId}`, { cache: "no-store" });
  return handle<Project>(res);
}

/** Deletes the project and all its child rows (§12.1). Returns void on 204. */
export async function deleteProject(projectId: string): Promise<void> {
  const res = await authFetch(`${API}/projects/${projectId}`, {
    method: "DELETE",
    cache: "no-store",
  });
  if (res.status === 204) return;
  await handle<void>(res);
}

export interface CreateProjectInput {
  title: string;
  genre_profile: string;
  source_language?: string;
  target_language?: string;
}

export async function createProject(
  input: CreateProjectInput
): Promise<Project> {
  const res = await authFetch(`${API}/projects`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({
      source_language: "en",
      target_language: "it",
      ...input,
    }),
  });
  return handle<Project>(res);
}

export async function listProjectJobs(projectId: string): Promise<Job[]> {
  const res = await authFetch(`${API}/projects/${projectId}/jobs`, {
    cache: "no-store",
  });
  return handle<Job[]>(res);
}

// --- Documenti: upload PDF (PRD §12.1, §5.2, §13.2) -------------------------

/** Risposta di POST /api/v1/projects/{id}/documents (contratto T09). */
export interface UploadResult {
  /** "uploaded" per un nuovo documento, "duplicate" se hash già presente. */
  status: "uploaded" | "duplicate";
  documentId: string;
  sha256: string | null;
  sizeBytes: number | null;
  pageCount: number | null;
  jobId: string | null;
  projectId: string;
}

export interface UploadDocumentInput {
  projectId: string;
  file: File;
  copyrightConfirmed: boolean;
  /** Callback di avanzamento: percentuale 0–100. */
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;
}

/**
 * Upload con progresso reale via XMLHttpRequest (fetch non espone il
 * progresso di upload). `copyright_confirmed` è obbligatorio (§13.2).
 */
export function uploadDocument(
  input: UploadDocumentInput
): Promise<UploadResult> {
  const { projectId, file, copyrightConfirmed, onProgress, signal } = input;

  return new Promise<UploadResult>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(
      "POST",
      `${API}/projects/${encodeURIComponent(projectId)}/documents`
    );
    xhr.responseType = "json";
    if (signal) {
      // XHR non supporta AbortSignal nativamente: ponte manuale.
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }

    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };

    xhr.onload = () => {
      const body = xhr.response as Record<string, unknown> | null;
      const documentId =
        body && typeof body.document_id === "string"
          ? body.document_id
          : body && typeof body.id === "string"
            ? (body.id as string)
            : null;
      if (xhr.status >= 200 && xhr.status < 300 && documentId) {
        resolve({
          status:
            body?.status === "duplicate"
              ? "duplicate"
              : "uploaded",
          documentId,
          sha256: body && typeof body.sha256 === "string" ? body.sha256 : null,
          sizeBytes:
            body && typeof body.size_bytes === "number"
              ? body.size_bytes
              : null,
          pageCount:
            body && typeof body.page_count === "number"
              ? body.page_count
              : null,
          jobId:
            body && typeof body.job_id === "string" ? body.job_id : null,
          projectId,
        });
      } else {
        const detail =
          body && typeof body.detail === "string"
            ? body.detail
            : `upload fallito (HTTP ${xhr.status})`;
        reject(new ApiError(xhr.status, detail));
      }
    };
    xhr.onerror = () => reject(new Error("errore di rete durante l'upload"));
    xhr.onabort = () => reject(new Error("upload annullato"));

    const form = new FormData();
    form.append("file", file, file.name);
    form.append("copyright_confirmed", String(copyrightConfirmed));
    xhr.send(form);
  });
}

// --- Struttura (PRD §5.3, §11.1, §12.2) --------------------------------------

/** Nodo struttura allineato all'output §5.3 (backend/structure_routes). */
export interface StructureNode {
  node_id: string;
  parent_id: string | null;
  kind: "front_matter" | "part" | "chapter" | "scene" | "back_matter"
  | "footnote" | string;
  source_label: string | null;
  normalized_title: string | null;
  start_page: number | null;
  end_page: number | null;
  start_char: number | null;
  end_char: number | null;
  confidence: number | null;
  detection_method: string[];
  status: "proposed" | "user_confirmed" | string;
  ordinal: number | null;
}

/** Risposta di GET /api/v1/projects/{id}/structure. */
export interface StructureResponse {
  project_id: string;
  nodes: StructureNode[];
  proposed: number;
  user_confirmed: number;
}

export async function getStructure(
  projectId: string
): Promise<StructureResponse> {
  const res = await authFetch(`${API}/projects/${projectId}/structure`, {
    cache: "no-store",
  });
  return handle<StructureResponse>(res);
}

/** POST /api/v1/projects/{id}/structure/nodes (§5.3 "creare"). */
export async function createNode(
  projectId: string,
  input: {
    kind: string;
    source_label: string;
    start_page: number;
    end_page?: number | null;
    normalized_title?: string | null;
    parent_id?: string | null;
  }
): Promise<unknown> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/nodes`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify(input),
    }
  );
  return handle(res);
}

/** POST .../structure/{nodeId}/rename (§5.3 "rinominare"). */
export async function renameNode(
  projectId: string,
  nodeId: string,
  sourceLabel: string
): Promise<unknown> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/${nodeId}/rename`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ source_label: sourceLabel }),
    }
  );
  return handle(res);
}

/** POST .../structure/{nodeId}/split (§5.3 "dividere"). */
export async function splitNode(
  projectId: string,
  nodeId: string,
  splitPage: number,
  labelPrefix?: string | null
): Promise<{ new_node_id: string }> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/${nodeId}/split`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        split_page: splitPage,
        label_prefix: labelPrefix ?? null,
      }),
    }
  );
  return handle(res);
}

/** POST .../structure/{nodeId}/merge (§5.3 "unire"). */
export async function mergeNode(
  projectId: string,
  nodeId: string,
  targetId: string
): Promise<unknown> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/${nodeId}/merge`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ target_id: targetId }),
    }
  );
  return handle(res);
}

/** POST .../structure/{nodeId}/move (§5.3 "spostare"). */
export async function moveNode(
  projectId: string,
  nodeId: string,
  direction: "up" | "down"
): Promise<unknown> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/${nodeId}/move`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ direction }),
    }
  );
  return handle(res);
}

/** DELETE .../structure/{nodeId} — scarta una proposta / nodo manuale. */
export async function deleteNode(
  projectId: string,
  nodeId: string
): Promise<unknown> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/${nodeId}`,
    { method: "DELETE" }
  );
  return handle(res);
}

/** POST .../structure/{nodeId}/boundary — correzione confine (§15.1). */
export async function setBoundary(
  projectId: string,
  nodeId: string,
  pages: { start_page?: number | null; end_page?: number | null }
): Promise<unknown> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/${nodeId}/boundary`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify(pages),
    }
  );
  return handle(res);
}

/** PATCH .../structure/{nodeId} — modifica generica (titolo/pagine/stato). */
export async function patchNode(
  projectId: string,
  nodeId: string,
  changes: Record<string, unknown>
): Promise<StructureNode> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/${nodeId}`,
    {
      method: "PATCH",
      headers: jsonHeaders(),
      body: JSON.stringify(changes),
    }
  );
  return handle<StructureNode>(res);
}

/** POST .../structure/{nodeId}/confirm (§5.3 user_confirmed). */
export async function confirmNode(
  projectId: string,
  nodeId: string
): Promise<StructureNode> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/${nodeId}/confirm`,
    { method: "POST" }
  );
  return handle<StructureNode>(res);
}

/** POST .../structure/undo — annulla l'ultima modifica (AC3). */
export async function undoStructureEdit(projectId: string): Promise<unknown> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/undo`,
    { method: "POST" }
  );
  return handle(res);
}

/** POST .../structure/resegment — §12.2, rigenera solo i dipendenti. */
export async function resegmentStructure(
  projectId: string,
  nodeId?: string | null
): Promise<{ job_id: string; status: string }> {
  const query = nodeId ? `?node_id=${encodeURIComponent(nodeId)}` : "";
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/resegment${query}`,
    { method: "POST" }
  );
  return handle(res);
}

/** POST .../structure/{nodeId}/segment — segmentazione CAT §5.4 di un capitolo. */
export async function segmentChapter(
  projectId: string,
  nodeId: string,
  sentencesPerSegment: number = 1
): Promise<{ job_id: string; status: string }> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/${nodeId}/segment?sentences_per_segment=${sentencesPerSegment}`,
    { method: "POST" }
  );
  return handle(res);
}

/** GET .../segments/overview — scheda Segmenti: token per capitolo/segmento. */
export interface SegmentOverviewSeg {
  segment_id: string;
  ordinal: number;
  /** Numero progressivo unico del segmento nel progetto (§11.2). */
  numero: number | null;
  status: string;
  tokens: number;
  chars: number;
  over_limit: boolean;
  text: string;
}
export interface SegmentOverviewChapter {
  node_id: string;
  kind: string;
  title: string;
  status: string;
  segment_count: number;
  /** Paragrafi sorgente del capitolo (null se il testo non è ancora estratto). */
  paragraph_count: number | null;
  total_tokens: number;
  fits_one_block: boolean;
  over_limit: boolean;
  max_block_tokens: number;
  usable_block_tokens: number;
  segments: SegmentOverviewSeg[];
}
export interface SegmentsOverview {
  project_id: string;
  max_block_tokens: number;
  usable_block_tokens: number;
  context_reserve_tokens: number;
  output_reserve_tokens: number;
  chapters: SegmentOverviewChapter[];
}
export async function getSegmentsOverview(
  projectId: string
): Promise<SegmentsOverview> {
  const res = await authFetch(`${API}/projects/${projectId}/segments/overview`, {
    cache: "no-store",
  });
  return handle<SegmentsOverview>(res);
}

/** GET .../translation-progress — indicatore globale di avanzamento traduzione. */
export interface TranslationProgress {
  active: boolean;
  project_id?: string;
  total_units?: number;
  translated_units?: number;
  jobs_total?: number;
  jobs_completed?: number;
  jobs_failed?: number;
  jobs_pending?: number;
  avg_job_seconds?: number | null;
  eta_seconds?: number | null;
}

export async function getTranslationProgress(): Promise<TranslationProgress> {
  const res = await authFetch(`${API}/translation-progress`, {
    cache: "no-store",
  });
  return handle<TranslationProgress>(res);
}

/** POST .../translation-progress/stop — annulla i job translate in coda. */
export async function stopTranslation(): Promise<{
  cancelled: number;
  running_left: number;
}> {
  const res = await authFetch(`${API}/translation-progress/stop`, {
    method: "POST",
  });
  return handle<{ cancelled: number; running_left: number }>(res);
}

/** POST .../translation-progress/resume — riaccoda i segmenti non tradotti. */
export async function resumeTranslation(): Promise<{
  enqueued: number;
  segments: number;
}> {
  const res = await authFetch(`${API}/translation-progress/resume`, {
    method: "POST",
  });
  return handle<{ enqueued: number; segments: number }>(res);
}

/** POST .../queue/stop — annulla tutti i job in coda (qualsiasi tipo). */
export async function stopQueue(): Promise<{
  cancelled: number;
  running_left: number;
}> {
  const res = await authFetch(`${API}/queue/stop`, { method: "POST" });
  return handle<{ cancelled: number; running_left: number }>(res);
}

/**
 * GET .../segments/verify — verifica carattere-per-carattere fra il testo
 * originale del libro e la sequenza dei segmenti. Chiamata DIRETTA al
 * backend (:8000): la scansione di un libro intero supera il timeout del
 * rewrite proxy di Next.js (~30s), come per le azioni massive (§5.4 UI).
 */
export interface VerifyDiffRegion {
  type: string;
  position: number;
  original_len: number;
  segment_len: number;
  original_snippet: string;
  segment_snippet: string;
}
export interface VerifyIssue {
  type: string;
  detail: string;
  regions?: VerifyDiffRegion[];
}
export interface VerifyChapter {
  node_id: string;
  kind: string;
  title: string;
  start_page: number | null;
  end_page: number | null;
  segment_count: number;
  original_chars: number;
  segment_chars: number;
  char_delta: number;
  ok: boolean;
  issues: VerifyIssue[];
  warnings: VerifyIssue[];
}
export interface VerifyReport {
  project_id: string;
  ok: boolean;
  generated_at: string;
  summary: {
    chapters: number;
    chapters_ok: number;
    chapters_with_issues: number;
    segments_total: number;
    original_chars: number;
    segment_chars: number;
    char_delta: number;
    uncovered_pages: number;
    uncovered_chars: number;
    chapters_whitespace_only: number;
  };
  chapters: VerifyChapter[];
  uncovered_pages: { page: number; chars: number; preview: string }[];
}
export async function verifySegmentCorrespondence(
  projectId: string
): Promise<VerifyReport> {
  const res = await authFetch(
    `${longApi()}/projects/${projectId}/segments/verify`,
    { cache: "no-store" }
  );
  return handle<VerifyReport>(res);
}

/**
 * Scheda "Anteprima" (2026-10-01): indice + capitolo renderizzato del libro,
 * stesse opzioni §15.4 dell'export (l'anteprima mostra ciò che partirebbe).
 */
export interface PreviewOutlineChapter {
  index: number;
  title: string;
  segments: number;
  chars: number;
  est_pages: number;
}
export interface PreviewOutline {
  project_id: string;
  project_title: string;
  include_drafts: boolean;
  watermark: boolean;
  chapters: PreviewOutlineChapter[];
  total_chapters: number;
  total_segments: number;
  total_chars: number;
  est_pages: number;
  error?: string;
  message?: string;
}
export interface PreviewParagraph {
  segment_id: string;
  ordinal: number;
  kind: string | null;
  status: string;
  is_draft: boolean;
  source_excerpt: string;
  target_excerpt: string;
  chars: number;
}
export interface PreviewChapter {
  project_id: string;
  include_drafts: boolean;
  watermark: boolean;
  chapter_index: number;
  total_chapters: number;
  chapter: { title: string; segments: number; est_pages: number };
  paragraphs: PreviewParagraph[];
  error?: string;
  message?: string;
}

export async function getPreviewOutline(
  projectId: string,
  options: ExportOptions
): Promise<PreviewOutline> {
  const res = await authFetch(
    `${API}/projects/${encodeURIComponent(projectId)}/preview/outline`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        format: options.format,
        include_drafts: options.include_drafts ?? false,
        watermark: options.watermark ?? false,
        page_numbers: options.page_numbers ?? true,
      }),
      cache: "no-store",
    }
  );
  return handle<PreviewOutline>(res);
}

export async function getPreviewChapter(
  projectId: string,
  options: ExportOptions,
  chapterIndex: number
): Promise<PreviewChapter> {
  const res = await authFetch(
    `${API}/projects/${encodeURIComponent(projectId)}/preview/chapter` +
      `?chapter_index=${chapterIndex}`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        format: options.format,
        include_drafts: options.include_drafts ?? false,
        watermark: options.watermark ?? false,
        page_numbers: options.page_numbers ?? true,
      }),
      cache: "no-store",
    }
  );
  return handle<PreviewChapter>(res);
}

/** Segmento del flusso del libro (viewer bilingue IT|EN). */
export interface BookFlowSegment {
  segment_id: string;
  chapter_title: string;
  new_chapter: boolean;
  is_first: boolean;
  kind: string | null;
  is_draft: boolean;
  target: string;
  source: string;
  chars: number;
}
export interface BookPage {
  number: number;
  chapter_title: string;
  chapter_start: boolean;
  segment_start: number;
  segment_end: number;
  chars: number;
}
export interface BookPages {
  project_id: string;
  project_title: string;
  include_drafts: boolean;
  watermark: boolean;
  page_size_chars: number;
  total_pages: number;
  total_segments: number;
  pages: BookPage[];
  flow: BookFlowSegment[];
  error?: string;
  message?: string;
}

/** POST .../preview/book — tutto il libro impaginato per il viewer IT|EN. */
export async function getPreviewBook(
  projectId: string,
  options: ExportOptions,
  pageSize = 1800
): Promise<BookPages> {
  const res = await authFetch(
    `${API}/projects/${encodeURIComponent(projectId)}/preview/book` +
      `?page_size=${pageSize}`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        format: options.format,
        include_drafts: options.include_drafts ?? false,
        watermark: options.watermark ?? false,
        page_numbers: options.page_numbers ?? true,
      }),
      cache: "no-store",
    }
  );
  return handle<BookPages>(res);
}

// --- Verifica Libro (2026-10-02): pagine originale vs clone ----------------
export interface TypoInfo {
  original_pages: number;
  /** Pagine del clone (genera/cacha il PDF se assente). */
  clone_pages?: number;
}

export async function getTypoInfo(projectId: string): Promise<TypoInfo> {
  const res = await authFetch(
    `${API}/projects/${encodeURIComponent(projectId)}/typography/info`,
    { cache: "no-store" }
  );
  return handle<TypoInfo>(res);
}

export async function clearCloneCache(projectId: string): Promise<void> {
  await authFetch(
    `${API}/projects/${encodeURIComponent(projectId)}/typography/clone-cache`,
    { method: "DELETE" }
  );
}

/** URL pagina originale N (PNG, per <img src>). */
export function originalPageUrl(projectId: string, page: number): string {
  return `${API}/projects/${encodeURIComponent(
    projectId
  )}/typography/original-page/${page}`;
}

/** URL pagina clone N (PNG; rigenera il clone se la cache e' scaduta). */
export function clonePageUrl(projectId: string, page: number): string {
  return `${API}/projects/${encodeURIComponent(
    projectId
  )}/typography/clone-page/${page}`;
}

/** POST .../segments/clear — rimuove la segmentazione dei capitoli selezionati. */
export async function clearSegmentation(
  projectId: string,
  nodeIds: string[]
): Promise<{
  deleted: number;
  kept_approved: number;
  chapters_all_approved: string[];
}> {
  const res = await authFetch(`${API}/projects/${projectId}/segments/clear`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ node_ids: nodeIds }),
  });
  return handle(res);
}

/** POST .../segments/{id}/split — risegmentazione manuale di un segmento. */
export async function splitSegment(
  projectId: string,
  segmentId: string,
  parts: number = 2
): Promise<{ segment_id: string; new_segment_id: string; parts: number }> {
  const res = await authFetch(
    `${API}/projects/${projectId}/segments/${segmentId}/split`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ parts }),
    }
  );
  return handle(res);
}

/** POST .../translation/run — avvia un blocco di traduzione (§10.1/§8.4). */
export async function runTranslation(
  projectId: string,
  segmentIds: string[],
  opts: {
    model?: string | null;
    temperature?: number;
    blockSource?: string;
  } = {}
): Promise<{ job_id: string; job_type: string; status: string }> {
  // UN SEGMENTO PER RICHIESTA (2026-09-25, decisione utente): i blocchi
  // da 20 facevano fallire l'intero blocco per un solo segmento problematico
  // (validazione all-or-nothing). Singolo = il fallimento resta confinato.
  // SEQUENZIALI: llama-server dietro GatewayServe i blocchi con
  // --parallel 1, quindi le chiamate parallele si accodano e vanno in
  // timeout lato client (misurato: 200 OK in 47s singolo, ReadTimeout a 3+).
  //
  // Chiave di idempotenza NUOVA ad ogni click: un rilancio manuale dell'
  // utente vuole una traduzione RIFATTA, non la risposta in cache di una
  // run precedente con lo stesso run_ref (§8.4 resta per resume/retry dei
  // job: la chiave vive nel payload del job, quindi i suoi retry riusano
  // la cache; un nuovo click invece no).
  const idempotencyKey =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `manual-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const jobs: { job_id: string; job_type: string; status: string }[] = [];
  for (const segmentId of segmentIds) {
    // il rate limiter del backend puo' rispondere 429: si riprova lo stesso
    // segmento con attesa crescente invece di far fallire tutto il batch
    let res: Response | null = null;
    for (let attempt = 0; attempt < 6; attempt++) {
      res = await authFetch(`${API}/projects/${projectId}/translation/run`, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({
          segment_ids: [segmentId],
          idempotency_key: idempotencyKey,
          ...(opts.model ? { model: opts.model } : {}),
          ...(opts.temperature !== undefined
            ? { temperature: opts.temperature }
            : {}),
        }),
      });
      if (res.status !== 429) break;
      await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)));
    }
    jobs.push(await handle(res as Response));
  }
  return jobs[jobs.length - 1];
}

/** POST .../structure/detect — rilancia il rilevamento §5.3. */
export async function detectStructure(
  projectId: string
): Promise<{ job_id: string; status: string }> {
  const res = await authFetch(
    `${API}/projects/${projectId}/structure/detect`,
    { method: "POST" }
  );
  return handle(res);
}

// --- Editor bilingue (PRD §11.2) --------------------------------------------

/** Stato di un segmento (translation_units) allineato ai router editor. */
export interface EditorSegment {
  segment_id: string;
  project_id: string;
  chapter_id: string | null;
  ordinal: number;
  /** Numero progressivo unico del segmento nel progetto (§11.2). */
  numero: number | null;
  source_text: string;
  target_text: string | null;
  status: "untranslated" | "machine_draft" | "approved" | string;
  source_hash: string;
  source_flags: Record<string, unknown>;
  has_markup: boolean;
  page: number | null;
  /** Verifica QE §10.2-bis: p(yes) "Is this text written in Italian?". */
  is_italian: number | null;
  /** Verifica QE §10.2-bis: p(yes) sulla coppia EN->IT (testo ridotto). */
  is_translated: number | null;
  /** Verifica QE §10.2-bis: p(yes) "Is this text written in English?". */
  is_english: number | null;
  /** DIFF = is_italian - is_english (<= 0: bozza probabilmente inglese). */
  qe_diff: number | null;
  /** QA issues critici non risolti su questo segmento. */
  qa_critical: number;
  has_qa: boolean;
}

export interface SegmentListResponse {
  project_id: string;
  chapter_id: string | null;
  segments: EditorSegment[];
  total: number;
  approved: number;
  untranslated: number;
  machine_draft: number;
}

/** GET /projects/{id}/segments — i segmenti con i filtri dell'editor (§11.2). */
export function listSegments(
  projectId: string,
  filters: {
    chapter_id?: string | null;
    status?: string | null;
    only_untranslated?: boolean | null;
    only_approved?: boolean | null;
    only_critical_qa?: boolean | null;
    only_ocr_suspect?: boolean | null;
  } = {}
): Promise<SegmentListResponse> {
  const query = new URLSearchParams();
  if (filters.chapter_id) query.set("chapter_id", filters.chapter_id);
  if (filters.status) query.set("status", filters.status);
  if (filters.only_untranslated)
    query.set("only_untranslated", "1");
  if (filters.only_approved)
    query.set("only_approved", "1");
  if (filters.only_critical_qa)
    query.set("only_critical_qa", "1");
  if (filters.only_ocr_suspect)
    query.set("only_ocr_suspect", "1");
  const qs = query.toString();
  const url = qs ? `${API}/projects/${projectId}/segments?${qs}`
    : `${API}/projects/${projectId}/segments`;
  return handle<SegmentListResponse>(authFetch(url, { cache: "no-store" }));
}

/**
 * POST /projects/{id}/segments/bulk-status — approva o riporta in bozza
 * molti segmenti in una transazione (§11.2). status: "approved"|"machine_draft".
 */
export async function bulkSegmentStatus(
  projectId: string,
  segmentIds: string[],
  status: "approved" | "machine_draft"
): Promise<{ updated: number; skipped: number; status: string }> {
  const res = await authFetch(
    `${API}/projects/${projectId}/segments/bulk-status`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ segment_ids: segmentIds, status }),
    }
  );
  return handle<{ updated: number; skipped: number; status: string }>(res);
}

/**
 * POST /projects/{id}/verify-translations — avvia la verifica QE massiva
 * (Open-QE DeBERTa-v3-large su GPU0): per ogni segmento tradotto calcola
 * is_italian e is_translated (§10.2-bis).
 */
export async function verifyTranslations(
  projectId: string,
  segmentIds?: string[]
): Promise<{ job_id: string; job_type: string; status: string }> {
  // Con segment_ids verifica SOLO la selezione (tasto bulk, come gli altri);
  // senza, tutti i segmenti tradotti del progetto.
  const res = await authFetch(`${API}/projects/${projectId}/verify-translations`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(
      segmentIds && segmentIds.length > 0
        ? { segment_ids: segmentIds }
        : {}
    ),
  });
  return handle<{ job_id: string; job_type: string; status: string }>(res);
}

/** POST /projects/{id}/segments/{id}/approve — Ctrl+Enter (§11.2 / AC1). */
export async function approveSegment(
  projectId: string,
  segmentId: string,
  payload: { reviewer?: string | null; qa_score?: number | null } = {}
): Promise<{
  segment_id: string; status: string; tm_entry_id: string; created: boolean;
}> {
  return handle(
    authFetch(`${API}/projects/${projectId}/segments/${segmentId}/approve`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify(payload),
    })
  );
}

/** POST /projects/{id}/segments/{id}/reject. */
export async function rejectSegment(
  projectId: string,
  segmentId: string,
  reason?: string | null
): Promise<{ segment_id: string; status: string; reason: string | null }> {
  return handle(
    authFetch(`${API}/projects/${projectId}/segments/${segmentId}/reject`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ reason }),
    })
  );
}

/** Diff a livello di token (per il pannello refine / §10.5). */
export interface DiffOp {
  op: "unchanged" | "removed" | "added";
  text: string;
}
export interface DiffResult {
  ops: DiffOp[];
}

/** POST /projects/{id}/segments/{id}/refine — con diff prima dell'applicazione. */
export async function refineSegment(
  projectId: string,
  segmentId: string,
  targetText: string,
  reason?: string | null
): Promise<{
  segment_id: string; status: string; target_text: string;
  diff: DiffResult; before_text: string;
}> {
  return handle(
    authFetch(`${API}/projects/${projectId}/segments/${segmentId}/refine`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ target_text: targetText, reason }),
    })
  );
}

export interface SegmentVersion {
  id: string;
  before: boolean;
  target_text: string;
  action: string;
  diff: DiffResult;
  created_at: string | null;
}

/** GET /projects/{id}/segments/{id}/versions — version history side-by-side. */
export async function listSegmentVersions(
  projectId: string,
  segmentId: string
): Promise<{ segment_id: string; history: SegmentVersion[]; count: number }> {
  return handle(
    authFetch(`${API}/projects/${projectId}/segments/${segmentId}/versions`, {
      cache: "no-store",
    })
  );
}

export interface QaIssue {
  id: string;
  segment_id: string;
  severity: "minor" | "major" | "critical";
  kind: string;
  category: string | null;
  message: string;
  span: string | null;
  comment: string | null;
  resolved: boolean;
  created_at: string | null;
}

export interface QaIssuesResponse {
  project_id: string;
  issues: QaIssue[];
  critical_unresolved: number;
}

export interface QaMqmReport {
  project_id: string;
  n_source_words: number;
  total_issues: number;
  per_1000_all: number;
  by_category: Record<string, number>;
  by_severity: Record<string, number>;
  by_category_severity: Record<string, Record<string, number>>;
}

/** GET /projects/{id}/qa/issues — con filtri (severità/capitolo/tipo, §11.1). */
export function listQaIssues(
  projectId: string,
  filters: {
    severity?: string | null;
    kind?: string | null;
    category?: string | null;
    group?: string | null;
    resolved?: boolean | null;
    chapter_id?: string | null;
  } = {}
): Promise<QaIssuesResponse> {
  const query = new URLSearchParams();
  if (filters.severity) query.set("severity", filters.severity);
  if (filters.kind) query.set("kind", filters.kind);
  if (filters.category) query.set("category", filters.category);
  if (filters.group) query.set("group", filters.group);
  if (filters.resolved !== null)
    query.set("resolved", filters.resolved ? "1" : "0");
  if (filters.chapter_id) query.set("chapter_id", filters.chapter_id);
  const qs = query.toString();
  const url = qs ? `${API}/projects/${projectId}/qa/issues?${qs}`
    : `${API}/projects/${projectId}/qa/issues`;
  return handle<QaIssuesResponse>(authFetch(url, { cache: "no-store" }));
}

/** POST /projects/{id}/qa/issues/{issueId}/resolve — toggle risolto. */
export async function resolveQaIssue(
  projectId: string,
  issueId: string
): Promise<{ id: string; resolved: boolean }> {
  const res = await authFetch(
    `${API}/projects/${projectId}/qa/issues/${issueId}/resolve`,
    { method: "POST", cache: "no-store" }
  );
  return handle<{ id: string; resolved: boolean }>(res);
}

export interface QaTaxonomy {
  groups: Record<string, string[]>;
  categories: string[];
  severities: string[];
}

/** GET /projects/{id}/qa/taxonomy — vocabolario MQM §10.4 (single-sourced). */
export function getQaTaxonomy(
  projectId: string
): Promise<QaTaxonomy> {
  const res = authFetch(`${API}/projects/${projectId}/qa/taxonomy`, {
    cache: "no-store",
  });
  return handle<QaTaxonomy>(res);
}

export interface CreateQaIssueInput {
  unit_id: string;
  severity: "minor" | "major" | "critical";
  category?: string | null;
  kind?: "human" | "qa" | "critic" | "qe" | "ocr" | "entity";
  message: string;
  span?: string | null;
  comment?: string | null;
}

/** POST /projects/{id}/qa/issues — annotazione MQM umana su uno span (§10.4). */
export async function createQaIssue(
  projectId: string,
  input: CreateQaIssueInput
): Promise<QaIssue> {
  const res = await authFetch(`${API}/projects/${projectId}/qa/issues`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
    cache: "no-store",
  });
  return handle<QaIssue>(res);
}

/** GET /projects/{id}/qa/mqm — report MQM per 1.000 parole (§18.2 / AC2). */
export function listQaMqm(
  projectId: string,
  category: string | null = null,
  severity: string | null = null
): Promise<QaMqmReport> {
  const query = new URLSearchParams();
  if (category) query.set("category", category);
  if (severity) query.set("severity", severity);
  const qs = query.toString();
  const url = qs ? `${API}/projects/${projectId}/qa/mqm?${qs}`
    : `${API}/projects/${projectId}/qa/mqm`;
  return handle<QaMqmReport>(authFetch(url, { cache: "no-store" }));
}

export interface SearchReplacement {
  segment_id: string;
  ordinal: number;
  old: string;
  new: string;
}

export interface SearchResponse {
  project_id: string;
  scope: string;
  query: string;
  replace_with: string;
  applied: boolean;
  matches: number;
  replacements: SearchReplacement[];
  /** Occorrenze in segmenti approvati (§5.1: mai riscritti). */
  readonly_matches?: number;
  readonly_replacements?: SearchReplacement[];
}

/** POST /projects/{id}/search — ricerca/sostituzione con scope e preview. */
export async function searchReplace(
  projectId: string,
  input: {
    query: string;
    replace_with: string;
    scope?: "segment" | "chapter" | "project";
    chapter_id?: string | null;
    case_sensitive?: boolean;
    apply?: boolean;
  }
): Promise<SearchResponse> {
  return handle(
    authFetch(`${API}/projects/${projectId}/search`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify(input),
    })
  );
}

// --- Documenti per il viewer (PRD §11.1) -------------------------------------

export interface DocumentInfo {
  id: string;
  filename: string;
  content_type: string | null;
  size_bytes: number | null;
  sha256: string | null;
  page_count: number | null;
  status: string;
  created_at: string | null;
}

export async function listDocuments(projectId: string): Promise<DocumentInfo[]> {
  const res = await authFetch(`${API}/projects/${projectId}/documents`, {
    cache: "no-store",
  });
  return handle<DocumentInfo[]>(res);
}

/** Pagina estratta (testo sincronizzato col PDF, PRD §11.1). */
export interface DocumentPage {
  document_id: string;
  page_number: number;
  level: string | null;
  ocr_suspect: boolean;
  confidence: number | null;
  normalized_text: string | null;
  text_sha256: string | null;
}

export async function getDocumentPage(
  projectId: string,
  documentId: string,
  pageNumber: number
): Promise<DocumentPage> {
  const res = await authFetch(
    `${API}/projects/${projectId}/documents/${documentId}/pages/${pageNumber}`,
    { cache: "no-store" }
  );
  return handle<DocumentPage>(res);
}

// --- Pulizia header/footer (PRD §5.3, §15.1) ---------------------------------

export interface CleaningCandidate {
  text: string;
  pages: number | null;
  algorithmically_confirmed: boolean;
}

export interface CleaningPreview {
  document_id: string;
  total_pages: number;
  candidates: CleaningCandidate[];
  applied: string[];
  rollback_available: boolean;
}

export async function getCleaningPreview(
  projectId: string,
  documentId: string
): Promise<CleaningPreview> {
  const res = await authFetch(
    `${API}/projects/${projectId}/documents/${documentId}/cleaning/preview`,
    { cache: "no-store" }
  );
  return handle<CleaningPreview>(res);
}

export async function applyCleaning(
  projectId: string,
  documentId: string,
  texts: string[]
): Promise<unknown> {
  const res = await authFetch(
    `${API}/projects/${projectId}/documents/${documentId}/cleaning/apply`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ texts }),
    }
  );
  return handle(res);
}

export async function rollbackCleaning(
  projectId: string,
  documentId: string
): Promise<unknown> {
  const res = await authFetch(
    `${API}/projects/${projectId}/documents/${documentId}/cleaning/rollback`,
    { method: "POST" }
  );
  return handle(res);
}

// --- LLM Gateway (matrice capability §8.1) --------------------------------

/**
 * Matrice delle capability (§8.1 / ADR-001 §3.3) popolata dinamicamente da
 * LLM Gateway: nessun nome hardcodato. Via `/api/v1/gateway/models`
 * (rewritato sul backend FastAPI, che a sua volta parla con Gateway).
 *
 * Se Gateway è irraggiungibile la richiesta fallisce: il caller decide come
 * gestirlo (la dashboard mostra "nessun modello", i selettori restano
 * utilizzabili).
 */
export async function listGatewayModels(): Promise<GatewayModelsResponse> {
  const res = await authFetch(`${API}/gateway/models`, { cache: "no-store" });
  if (!res.ok) {
    throw new ApiError(res.status, res.statusText || "Gateway irraggiungibile");
  }
  return handle<GatewayModelsResponse>(res);
}

/**
 * Imposta i due sceltori modello + le impostazioni avanzate per progetto
 * (§8.2). PATCH /api/v1/projects/{id}.
 */
export async function updateProject(
  projectId: string,
  changes: {
    translation_model_id?: string | null;
    text_model_id?: string | null;
    model_settings?: Project["model_settings"] | null;
  }
): Promise<Project> {
  const res = await authFetch(`${API}/projects/${projectId}`, {
    method: "PATCH",
    headers: jsonHeaders(),
    body: JSON.stringify(changes),
  });
  return handle<Project>(res);
}

// --- Entità e glossario (PRD §6.6 / §15.2 / §12.3) -------------------------

/** Parametri di filtro per GET /projects/{id}/entities (§6.6). */
export interface EntityFilters {
  status?: string | null;
  entity_type?: string | null;
  referential_gender?: string | null;
  grammatical_number?: string | null;
  chapter_id?: string | null;
  /** filtro Alias server-side (§6.6): upper|lower */
  alias?: "upper" | "lower" | "";
  /** filtro Traduzione server-side (§6.6) */
  translation?: "" | "identical" | "shares-word" | "same-as-en-and-alias";
  /** ordinamento server-side (§6.6): colonna + direzione */
  sort?: string | null;
  order?: "asc" | "desc" | null;
  page?: number;
  per_page?: number;
}

/**
 * Elenco entità filtrato e paginata (§6.6: tabella filtrabile per
 * stato/tipo/genere/numero/capitolo). Popolata da
 * GET /projects/{id}/entities.
 */
export function listEntities(
  projectId: string,
  filters: EntityFilters = {}
): Promise<EntityListResponse> {
  const query = new URLSearchParams();
  if (filters.status) query.set("status", filters.status);
  if (filters.entity_type) query.set("entity_type", filters.entity_type);
  if (filters.referential_gender)
    query.set("referential_gender", filters.referential_gender);
  if (filters.grammatical_number)
    query.set("grammatical_number", filters.grammatical_number);
  if (filters.chapter_id)
    query.set("chapter_id", filters.chapter_id);
  if (filters.alias) query.set("alias", filters.alias);
  if (filters.translation) query.set("translation", filters.translation);
  if (filters.sort) query.set("sort", filters.sort);
  if (filters.order) query.set("order", filters.order);
  if (filters.page) query.set("page", String(filters.page));
  if (filters.per_page) query.set("per_page", String(filters.per_page));
  const qs = query.toString();
  const url = qs ? `${API}/projects/${projectId}/entities?${qs}`
    : `${API}/projects/${projectId}/entities`;
  return handle<EntityListResponse>(
    authFetch(url, { cache: "no-store" })
  );
}

/** Dettaglio entità con ogni evidenza (§15.2). */
export function getEntity(
  projectId: string,
  entityId: string
): Promise<Entity> {
  return handle<Entity>(
    authFetch(`${API}/projects/${projectId}/entities/${entityId}`,
      { cache: "no-store" })
  );
}

/** Evidenze con ±2 paragrafi di contesto (§6.6 / §12.3). */
export function getEntityEvidence(
  projectId: string,
  entityId: string
): Promise<{
  entity_id: string;
  canonical_source: string;
  mention_count: number;
  evidence: EntityEvidence[];
}> {
  return handle(
    authFetch(`${API}/projects/${projectId}/entities/${entityId}/evidence`,
      { cache: "no-store" })
  );
}

/**
 * Modifica un'entità (generi, numero, policy, forma IT approvata, stato,
 * checkbox e priorità) — PATCH /projects/{id}/entities/{id} (§6.4 / §6.6 /
 * §15.2).
 */
export async function updateEntity(
  projectId: string,
  entityId: string,
  changes: {
    canonical_target?: string | null;
    entity_type?: string | null;
    referential_gender?: string | null;
    italian_grammatical_gender?: string | null;
    grammatical_number?: string | null;
    translation_policy?: string | null;
    definition?: string | null;
    notes?: string | null;
    status?: string | null;
    forbidden_targets?: string[] | null;
    priority?: string | null;
    never_translate?: boolean | null;
    allow_inflection?: boolean | null;
  }
): Promise<Entity> {
  return handle<Entity>(
    authFetch(`${API}/projects/${projectId}/entities/${entityId}`, {
      method: "PATCH",
      headers: jsonHeaders(),
      body: JSON.stringify(changes),
    })
  );
}

/** Aggiunge un'alias a un'entità (§6.5). */
export async function addEntityAlias(
  projectId: string,
  entityId: string,
  sourceAlias: string,
  targetAlias?: string | null,
  aliasType = "synonym"
): Promise<Entity> {
  return handle<Entity>(
    authFetch(`${API}/projects/${projectId}/entities/${entityId}/aliases`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ source_alias: sourceAlias, target_alias: targetAlias, alias_type: aliasType }),
    })
  );
}

/** Rimuove un'alias da un'entità (§6.5). */
export async function removeEntityAlias(
  projectId: string,
  entityId: string,
  aliasId: string
): Promise<Entity> {
  return handle<Entity>(
    authFetch(`${API}/projects/${projectId}/entities/${entityId}/aliases/${aliasId}`, {
      method: "DELETE",
    })
  );
}

/** Unisce un'entità in un'altra (§15.2 / AC1). */
export async function mergeEntity(
  projectId: string,
  entityId: string,
  targetId: string
): Promise<Entity> {
  return handle<Entity>(
    authFetch(`${API}/projects/${projectId}/entities/${entityId}/merge`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ target_id: targetId }),
    })
  );
}

/** Divide un'alias in una nuova entità (§6.6). */
export async function splitEntity(
  projectId: string,
  entityId: string,
  sourceAlias: string,
  canonicalSource: string,
  canonicalTarget?: string | null,
  entityType = "CONCEPT_TERM"
): Promise<Entity> {
  return handle<Entity>(
    authFetch(`${API}/projects/${projectId}/entities/${entityId}/split`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        source_alias: sourceAlias,
        canonical_source: canonicalSource,
        canonical_target: canonicalTarget,
        entity_type: entityType,
      }),
    })
  );
}

/**
 * Scarica TUTTE le entità (tutte le pagine, fino a `maxEntities`) rispettando
 * i filtri correnti. Serve alle azioni massive e alla selezione "tutte le
 * pagine" della tabella (§6.6).
 */
export async function listAllEntities(
  projectId: string,
  filters: EntityFilters = {},
  maxEntities = 5000
): Promise<Entity[]> {
  const PAGE = 500;
  const first = await listEntities(projectId, { ...filters, page: 1, per_page: PAGE });
  const out = [...first.entities];
  const totalPages = Math.ceil(Math.min(first.total, maxEntities) / PAGE);
  for (let p = 2; p <= totalPages; p++) {
    const next = await listEntities(projectId, { ...filters, page: p, per_page: PAGE });
    out.push(...next.entities);
  }
  return out;
}

/**
 * Cambia lo stato di molte entità in UNA richiesta (§6.2.5, azioni massive
 * su tutte le pagine). Una sola transazione server-side al posto di N PATCH
 * parallele (che su migliaia di righe superano il timeout del fetch).
 */
export async function bulkStatusEntities(
  projectId: string,
  ids: string[],
  status: "proposed" | "verified" | "approved"
): Promise<{ updated: string[]; count: number }> {
  return handle(
    authFetch(`${longApi()}/projects/${projectId}/entities/bulk-status`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ ids, status }),
    })
  );
}

/**
 * "Traduci Nomi": traduce canonical_source (EN) -> canonical_target (IT)
 * per le entità selezionate, usando il modello di traduzione del progetto
 * (§8.1, via Gateway). I nomi non traducibili (forma identica) lasciano
 * canonical_target vuoto.
 */
export async function translateEntityNames(
  projectId: string,
  ids: string[]
): Promise<{ translated: number; untranslatable: number; failed: number }> {
  return handle(
    authFetch(`${longApi()}/projects/${projectId}/entities/translate-names`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ ids }),
    })
  );
}

/** Approva in massa una lista di entità (§6.2.5 / approval massiva). */
export async function approveEntities(
  projectId: string,
  ids: string[]
): Promise<{ approved: string[]; count: number }> {
  return handle(
    authFetch(`${API}/projects/${projectId}/entities/approve`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ ids }),
    })
  );
}

/**
 * Esporta le entità come CSV o TBX (§12.3 / AC3). Ritorna il blob grezzo
 * (con headers di download) così la pagina può lanciare lo scaricamento.
 */
export async function exportEntities(
  projectId: string,
  fmt: "csv" | "tbx" = "csv"
): Promise<{ blob: Blob; filename: string }> {
  const res = await authFetch(
    `${API}/projects/${projectId}/entities/export?fmt=${fmt}`,
    { cache: "no-store" }
  );
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch { /* status text */ }
    throw new ApiError(res.status, detail);
  }
  const blob = await res.blob();
  const disp = res.headers.get("content-disposition") ?? "";
  const m = /filename="?([^"]+)"?/.exec(disp);
  const filename = (m ? m[1] : `entities.${fmt}`)
    .replace(/\.csv$|\.tbx$/i, "")
    .replace(/[^a-z0-9_-]+/gi, "_")
    .replace(/_+$/i, "") || `entities.${fmt}`;
  return { blob, filename };
}

/**
 * Importa entità da CSV/TBX (§12.3 / AC3 round-trip). Il file viene
 * inviato via FormData al backend che la incapsula in un Blob.
 */
export async function importEntities(
  projectId: string,
  file: Blob,
  filename: string,
  fmt: "csv" | "tbx" | "auto" = "auto"
): Promise<{ imported: number; errors: string[]; error_count: number; total: number }> {
  const form = new FormData();
  form.append("file", file, filename);
  form.append("fmt", fmt);
  return handle(
    authFetch(`${API}/projects/${projectId}/entities/import`, {
      method: "POST",
      body: form,
    })
  );
}

/**
 * Entità introdotte in un capitolo e delta vs il capitolo precedente
 * (§6.6).
 */
export function getChapterEntities(
  projectId: string,
  nodeId: string
): Promise<ChapterEntitiesResponse> {
  return handle<ChapterEntitiesResponse>(
    authFetch(`${API}/projects/${projectId}/entities/chapter/${nodeId}`,
      { cache: "no-store" })
  );
}

/** Storico immutabile delle modifiche di un'entità (§15.4). */
export function getEntityVersions(
  projectId: string,
  entityId: string
): Promise<EntityVersionsResponse> {
  return handle<EntityVersionsResponse>(
    authFetch(`${API}/projects/${projectId}/entities/${entityId}/versions`,
      { cache: "no-store" })
  );
}

// --- Traduzione: piano blocchi / budget token (§5.4 / §15.3) ---------------

/**
 * Dry-run del planner (§5.4): blocchi stimati + budget token per capitolo.
 * POST /api/v1/projects/{id}/chapters/{node_id}/plan (richiede la
 * segmentazione, vedi §5.4.2).
 */
export async function getChapterPlan(
  projectId: string,
  chapterId: string
): Promise<PlanResponse> {
  const res = await authFetch(
    `${API}/projects/${encodeURIComponent(projectId)}/chapters/${chapterId}/plan`,
    { cache: "no-store" }
  );
  return handle<PlanResponse>(res);
}

// --- Export (PRD §15.4, §11.1, §13.1) ----------------------------------------

/** Opzioni di export (§15.4). */
export interface ExportOptions {
  /** `docx` (editoriale), `epub`, `pdf` o `html`. */
  format: "docx" | "epub" | "html" | "pdf";
  /** Include le bozze (§15.4); richiede `watermark` se presente almeno una bozza. */
  include_drafts?: boolean;
  /** Watermark `[BOZZA]` sulle bozze spedite (§13.1). */
  watermark?: boolean;
  /** Solo PDF: piedipagina con titolo libro + numero pagina (default: on). */
  page_numbers?: boolean;
  /** Clona l'aspetto del libro originale (font/allineamenti/immagini/pagine). */
  clone_structure?: boolean;
  /** Clone: rapporto pagine tradotte / pagine originali (default 1.0). */
  page_ratio?: number;
}

/** §15.4 manifest restituito da /export/plan e /export. */
export interface ExportManifest {
  format: string;
  project_id: string;
  title: string;
  version: string;
  generated_at: string;
  source_language: string;
  target_language: string;
  genre_profile: string;
  project_status: string;
  models: string[];
  counts: {
    total_segments: number;
    approved: number;
    drafts: number;
    with_target: number;
  };
  include_drafts: boolean;
  watermarked: boolean;
  glossary_snapshot_id: string | null;
  tm_snapshot_id: string | null;
  audit_id: string | null;
}

/** Risposta di POST /projects/{id}/export/plan. */
export interface ExportPlanResponse {
  project_id: string;
  format: string;
  include_drafts: boolean;
  watermark: boolean;
  selected_segments: number;
  counts: ExportManifest["counts"];
  chapters: string[];
  manifest: ExportManifest;
}

/**
 * Preview del piano export (§15.4): convalida le opzioni e riporta i conteggi
 * verificabili, i capitoli e il manifest senza generare il file.
 */
export async function getExportPlan(
  projectId: string,
  options: ExportOptions
): Promise<ExportPlanResponse> {
  // longApi(): il clone export supera i ~30s del rewrite proxy Next
  // (chiamata diretta :8000, CORS LAN gia' aperto — 2026-10-01).
  const res = await authFetch(
    `${longApi()}/projects/${encodeURIComponent(projectId)}/export/plan`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        format: options.format,
        include_drafts: options.include_drafts ?? false,
        watermark: options.watermark ?? false,
        page_numbers: options.page_numbers ?? true,
        clone_structure: options.clone_structure ?? false,
        page_ratio: options.page_ratio ?? 1.0,
      }),
      cache: "no-store",
    }
  );
  return handle<ExportPlanResponse>(res);
}

/**
 * Genera il file export e lo ritorna come Blob per il download nel browser
 * (§11.1 pagina Export; §13.1: i byte non lasciano la rete locale — il file
 * passa solo origin→backend→origin). Ritorna anche l'header
 * `X-Export-Snapshot-Id` del snapshot immutabile associato all'export.
 */
export async function runExport(
  projectId: string,
  options: ExportOptions
): Promise<{ blob: Blob; filename: string; snapshotId: string | null }> {
  // longApi() DIRETTO al backend: la generazione del PDF clone impiega
  // 45-65s e il rewrite proxy Next.js tronca a ~30s con 500 (2026-10-01).
  const res = await authFetch(
    `${longApi()}/projects/${encodeURIComponent(projectId)}/export`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        format: options.format,
        include_drafts: options.include_drafts ?? false,
        watermark: options.watermark ?? false,
        page_numbers: options.page_numbers ?? true,
        clone_structure: options.clone_structure ?? false,
        page_ratio: options.page_ratio ?? 1.0,
      }),
      cache: "no-store",
    }
  );
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* corpo non JSON */
    }
    throw new ApiError(res.status, detail);
  }
  const blob = await res.blob();
  const disp = res.headers.get("content-disposition") ?? "";
  const m = /filename="?([^";]+)"?/.exec(disp);
  const fallback = "libro";
  const name = (m ? m[1] : fallback)
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/_+$/g, "") || fallback;
  return {
    blob,
    filename: name,
    snapshotId: res.headers.get("X-Export-Snapshot-Id"),
  };
}

/** Uno snapshot immutabile di export precedentemente generato (§15.4). */
export interface ExportSnapshot {
  id: string;
  format: string | null;
  include_drafts: boolean | null;
  watermark: boolean | null;
  item_count: number;
  created_at: string | null;
}

/** GET /projects/{id}/export/snapshots — storico degli export (§15.4). */
export function listExportSnapshots(
  projectId: string
): Promise<{ project_id: string; snapshots: ExportSnapshot[] }> {
  const res = authFetch(
    `${API}/projects/${encodeURIComponent(projectId)}/export/snapshots`,
    { cache: "no-store" }
  );
  return handle<{ project_id: string; snapshots: ExportSnapshot[] }>(res);
}
