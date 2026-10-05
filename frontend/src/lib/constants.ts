/** Costanti condivise del frontend. */

/** Chiavi di cache TanStack Query. */
export const QUERY_KEY_PROJECTS = ["progetti"] as const;
export const QUERY_KEY_JOBS = (projectId: string) => [
  "lavori",
  projectId,
] as const;
export const QUERY_KEY_MODELS = ["modelli"] as const;

/** Profili di genere (PRD §9.1) — valore API → etichetta italiana. */
export const GENRE_OPTIONS: { value: string; label: string; hint: string }[] = [
  {
    value: "fantascienza",
    label: "Romanzo di fantascienza",
    hint: "Coerenza worldbuilding, neologismi, tecnologia, unità di misura",
  },
  {
    value: "horror",
    label: "Romanzo horror",
    hint: "Tensione, ambiguità, ritmo, immagini sensoriali",
  },
  {
    value: "rosa",
    label: "Romanzo rosa",
    hint: "Voce, intimità, dialogo, registro relazionale",
  },
  {
    value: "saggio",
    label: "Saggio",
    hint: "Accuratezza concettuale, citazioni, terminologia disciplinare",
  },
];

/** Etichette italiane per i tipi di job noti. */
export const JOB_TYPE_LABEL_IT: Record<string, string> = {
  parse: "Analisi documento",
  parse_l1: "Estrazione testo (L1)",
  ocr_document: "OCR documento",
  detect_structure: "Rilevamento struttura",
  segment_chapter: "Segmentazione capitolo",
  entity_extract: "Estrazione entità",
  translate_batch: "Traduzione batch",
  qa_run: "Verifica QA",
  export: "Esportazione",
};

export const JOB_STATUS_LABEL_IT: Record<string, string> = {
  pending: "In coda",
  running: "In corso",
  completed: "Completato",
  failed: "Fallito",
};

/** Stato di un'entità (PRD §6.2.5 / §15.2) — valore API → etichetta IT. */
export const ENTITY_STATUS_LABEL_IT: Record<string, string> = {
  proposed: "Proposto",
  verified: "Verificato",
  approved: "Approvato",
  deprecated: "Deprecato",
  merged: "Unito",
};

/** Tipo di entità (PRD §6.3 — categorie) → etichetta IT. */
export const ENTITY_TYPE_LABEL_IT: Record<string, string> = {
  PERSON: "Persona",
  ROLE: "Ruolo",
  CREATURE_SPECIES: "Specie",
  OBJECT_ARTIFACT: "Oggetto",
  LOCATION: "Luogo",
  ORG_FACTION: "Organizzazione",
  WORK_MEDIA: "Opera/Media",
  EVENT: "Evento",
  CONCEPT_TERM: "Concetto/Termine",
  TITLE_HONORIFIC: "Titolo/Onorifico",
};

/** Genere referenziale (PRD §9.1) → etichetta IT. */
export const REFERENTIAL_GENDER_LABEL_IT: Record<string, string> = {
  male: "Maschile",
  female: "Femminile",
  nonbinary: "Non binario",
  mixed: "Misto",
  unknown: "Sconosciuto",
  not_applicable: "Non applicabile",
};

/** Genere grammaticale italiano → etichetta IT. */
export const ITALIAN_GENDER_LABEL_IT: Record<string, string> = {
  masculine: "Maschile",
  feminine: "Femminile",
  common: "Comune",
  variable: "Variabile",
  not_applicable: "Non applicabile",
};

/** Numero grammaticale → etichetta IT. */
export const GRAMMATICAL_NUMBER_LABEL_IT: Record<string, string> = {
  singular: "Singolare",
  plural: "Plurale",
  invariant: "Invariante",
  collective: "Collettivo",
  unknown: "Sconosciuto",
};

/** Policy di traduzione → etichetta IT. */
export const TRANSLATION_POLICY_LABEL_IT: Record<string, string> = {
  keep_source: "Mantieni sorgente",
  translate: "Traduci",
  transliterate: "Traslitera",
  contextual: "Contestuale",
  undecided: "Indeciso",
};

/** Priorità (PRD §6.6) → etichetta IT. */
export const ENTITY_PRIORITY_LABEL_IT: Record<string, string> = {
  block_batch: "Blocca batch",
  warn: "Warn",
  normal: "Normale",
};

/** MQM — gruppi di categoria §10.4 → etichetta IT (ordine di rendering). */
export const MQM_GROUP_LABEL_IT: Record<string, string> = {
  accuracy: "Accuratezza",
  terminology: "Terminologia",
  italian: "Italiano",
  style: "Stile",
  locale: "Locale",
  source: "Sorgente",
};

/** MQM — categoria §10.4 → etichetta IT. */
export const MQM_CATEGORY_LABEL_IT: Record<string, string> = {
  mistranslation: "Traduzione errata",
  omission: "Omissione",
  addition: "Aggiunta",
  untranslated: "Non tradotto",
  wrong_term: "Termine errato",
  forbidden_term: "Termine proibito",
  term_inconsistency: "Terminologia incoerente",
  grammar: "Grammatica",
  spelling: "Ortografia",
  punctuation: "Punteggiatura",
  collocation: "Collocazione",
  awkward: "Improprio",
  register: "Registro",
  voice: "Voce",
  tone: "Tono",
  calco: "Calco",
  repetition: "Ripetizione",
  formatting: "Formattazione",
  quotes: "Virgolette",
  units_data: "Misure/date",
  ocr_suspected: "OCR sospetto",
  ambiguous_source: "Sorgente ambigua",
};

/** MQM — gravità §10.4 → etichetta IT + classe colore. */
export const MQM_SEVERITY_LABEL_IT: Record<string, string> = {
  minor: "Minore",
  major: "Maggiore",
  critical: "Critico",
};

export const MQM_SEVERITY_CLASS: Record<string, string> = {
  minor: "bg-slate-700 text-slate-200",
  major: "bg-amber-500/20 text-amber-300",
  critical: "bg-red-500/20 text-red-300",
};

/** Origine dell'issue (colonna ``kind``) → etichetta IT. */
export const QA_KIND_LABEL_IT: Record<string, string> = {
  human: "Annotazione",
  qa: "Deterministico",
  critic: "Critic",
  qe: "QE",
  ocr: "OCR",
  entity: "Entità",
};
