/** Tipi delle entità dell'API Trans (T09 — /api/v1) allineati ai backend/routes. */

export type ProjectStatus =
  | "DRAFT"
  | "IMPORTING"
  | "PARSED"
  | "STRUCTURE_REVIEW"
  | "ENTITY_REVIEW"
  | "READY_FOR_TRANSLATION"
  | "TRANSLATING"
  | "QA_REVIEW"
  | "APPROVED"
  | "EXPORTED";

export interface Project {
  id: string;
  title: string;
  source_language: string;
  target_language: string;
  genre_profile: string;
  status: ProjectStatus;
  copyright_confirmed: boolean;
  created_at: string;
  updated_at: string;
  // §8.2: i due modelli scelti (analisi/testo + traduzione).
  translation_model_id: string | null;
  text_model_id: string | null;
  // §8.2: impostazioni avanzate per modello, persistite per progetto.
  model_settings: ModelSettings | null;
}

export interface Job {
  id: string;
  job_type: string;
  status: "pending" | "running" | "completed" | "failed" | string;
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

/** Risposta di POST /api/v1/projects/{id}/documents (T09). */
export interface DocumentUpload {
  id: string;
  project_id: string;
  filename: string;
  content_type: string | null;
  size_bytes: number | null;
  sha256: string | null;
  page_count: number | null;
  copyright_confirmed: boolean;
  created_at: string;
}

/**
 * Una riga della capability matrix (§8.1 / ADR-001 §3.3): ogni modello
 * esposto da LLM Gateway con le sue reali capacità. Popolata da
 * `GET /api/v1/gateway/models` (nessun nome hardcodato).
 */
export interface GatewayModel {
  id: string;
  display_name: string;
  provider: string;
  context_window: number | null;
  max_output: number | null;
  supports_json_schema: boolean;
  supports_streaming: boolean;
  supports_reasoning: boolean;
  supports_seed: boolean;
  languages: string[];
  locality: string;
  status: "available" | "degraded" | "offline" | string;
  latency_ms: number | null;
  vram_host: string | null;
}

/** Risposta di GET /api/v1/gateway/models (matrice capability §8.1). */
export interface GatewayModelsResponse {
  count: number;
  models: GatewayModel[];
}

/** Impostazioni avanzate per modello (§8.2 / PRD §16.3). */
export interface ModelSettings {
  translation: ModelFieldSettings;
  text: ModelFieldSettings;
}

/** Un sottoinsieme delle impostazioni avanzate di un singolo modello. */
export interface ModelFieldSettings {
  temperature?: number | null;
  top_p?: number | null;
  seed?: number | null;
  reasoning?: "on" | "off" | null;
  reasoning_budget?: number | null;
  timeout?: number | null;
  retry?: number | null;
  max_output?: number | null;
  prompt_template?: string | null;
}

/** Profili di genere (PRD §9.1). */
export type GenreProfile = "fantascienza" | "horror" | "rosa" | "saggio";

/** Risposta di GET /api/v1/projects/{id}/chapters/{node_id}/plan (§5.4). */
export interface PlanBlock {
  block_index: number;
  instruction: string;
  context: {
    marker: string;
    items: {
      segment_id: string;
      role: "preceding" | "following";
      marker: string;
      text: string;
    }[];
  };
  segments: { segment_id: string; source_text: string }[];
  token_budget: {
    source: number;
    context_reserve: number;
    output_reserve: number;
    total: number;
    limit: number;
    total_2?: number;
    payload_overhead_tokens?: number;
  };
}

/**
 * Una entità del progetto (PRD §6.2–6.6 / §15.2). Specifica e allineata
 * a `backend/entity_routes._entity_summary`: ogni campo qui è un campo
 * servito da `/projects/{id}/entities` e `/projects/{id}/entities/{id}`.
 */
export interface Entity {
  id: string;
  project_id: string;
  canonical_source: string;
  canonical_target: string | null;
  entity_type: string;
  status: "proposed" | "verified" | "approved" | "deprecated" | "merged" | string;
  referential_gender: string;
  referential_gender_evidence: string | null;
  italian_grammatical_gender: string;
  grammatical_number: string;
  translation_policy: string;
  definition: string | null;
  notes: string | null;
  confidence: number | null;
  aliases: string[];
  forbidden_targets: string[];
  priority: "block_batch" | "warn" | "normal" | string | null;
  never_translate: boolean;
  allow_inflection: boolean;
  version: number;
  mention_count: number;
  first_evidence: EvidenceSummary | null;
  created_at: string | null;
  updated_at: string | null;
  /** Solo su dettaglio (/entities/{id}) ed evidenze (/entities/{id}/evidence). */
  evidence?: EntityEvidence[];
}

/** Una menzione/evidenza con quote e pagina (§15.2). */
export interface EvidenceSummary {
  page_number: number | null;
  quote_text: string | null;
  evidence_type: string | null;
  extractor: string | null;
  ocr_suspect?: boolean;
}

/**
 * Una menzione con ±2 paragrafi di contesto (§6.6 / §12.3).
 * ``ocr_suspect``: badge "OCR sospetto" quando la pagina/segmento sorgente
 * è stato marcato come OCR incerto (PRD §5.2, §13, §11.2).
 */
export interface EntityEvidence {
  id: string;
  page_number: number | null;
  chapter_id: string | null;
  quote_text: string | null;
  evidence_type: string | null;
  confidence: number | null;
  extractor: string | null;
  segment_id: string | null;
  context: { ordinal: number; source_text: string }[] | null;
  ocr_suspect?: boolean;
}

/** Risposta di GET /projects/{id}/entities (paginata, PRD §6.6). */
export interface EntityListResponse {
  total: number;
  page: number;
  per_page: number;
  entities: Entity[];
}

/** Risposta di GET /projects/{id}/entities/chapter/{node_id} (§6.6). */
export interface ChapterEntitiesResponse {
  chapter_id: string;
  introduced_count: number;
  new_count: number;
  introduced: Entity[];
  delta: Entity[];
  returned: Entity[];
}

/** Risposta di GET /projects/{id}/entities/{id}/versions (§15.4). */
export interface EntityVersionsResponse {
  entity_id: string;
  versions: {
    version: number;
    action: string;
    snapshot: Entity;
    created_at: string | null;
  }[];
}

export interface PlanResponse {
  project_id: string;
  chapter_id: string;
  node_kind: string;
  segments: number;
  tokenizer: string;
  verification: {
    blocks: number;
    limit: number;
    max_block_total: number;
    all_within_budget: boolean;
    oversized_blocks: number[];
  };
  blocks: PlanBlock[];
}
