"use client";

/**
 * Entità e glossario (§6.6 / §15.2 / §12.3).
 *
 * L'interfaccia è divisa in quattro zone:
 *   - barra strumenti: estrazione entità, import/export CSV/TBX;
 *   * vista "capitolo": entità introdotte + delta (§6.6);
 *   - tabella filtrable (stato/tipo/genere/numero/capitolo; paginata);
 *   - side panel di dettaglio con tutte le menzioni/evidenze (quote +
 *     pagina, ±2 paragrafi), merge/split di alias, forma IT approvata,
 *     generi, policy, checkbox e priorità.
 *
 * Tutte le modifiche (merge/split/approval/edit) sono riflesse subito nella
 * tabella tramite l'invalidazione delle query TanStack.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { ErrorNote, Loading } from "@/components/ui";
import {
  ApiError,
  addEntityAlias,
  approveEntities,
  getChapterEntities,
  getChapterPlan,
  getEntity,
  getEntityEvidence,
  getEntityVersions,
  getStructure,
  importEntities,
  listAllEntities,
  listEntities,
  listProjects,
  mergeEntity,
  removeEntityAlias,
  splitEntity,
  bulkStatusEntities,
  translateEntityNames,
  updateEntity,
  exportEntities,
  authFetch,
  type Entity as EntityDto,
  type EntityEvidence as EntityEvidenceDto,
  type StructureNode,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const STATUS_CLASS: Record<string, string> = {
  proposed: "bg-slate-700 text-slate-200",
  verified: "bg-sky-500/20 text-sky-300",
  approved: "bg-emerald-500/20 text-emerald-200",
  deprecated: "bg-slate-800 text-slate-400",
  merged: "bg-amber-500/20 text-amber-300",
};

const PRIORITY_CLASS: Record<string, string> = {
  block_batch: "bg-red-500/20 text-red-300",
  warn: "bg-amber-500/20 text-amber-300",
  normal: "bg-slate-700 text-slate-300",
};

/** Nomi delle entità estratte come nodi capitolo dall'API struttura. */
function chapterLabels(nodes: StructureNode[]): {
  id: string;
  label: string;
}[] {
  return nodes
    .filter((n) => n.kind === "chapter")
    .map((n) => ({ id: n.node_id, label: n.normalized_title ?? n.node_id }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

export default function EntitiesPage() {
  const queryClient = useQueryClient();
  const { locale, t, dict } = useI18n();
  const entityStatusLabels = dict.enums.entityStatus as Record<string, string>;
  const entityTypeLabels = dict.enums.entityType as Record<string, string>;
  const referentialGenderLabels = dict.enums.referentialGender as Record<
    string,
    string
  >;
  const italianGenderLabels = dict.enums.italianGender as Record<string, string>;
  const grammaticalNumberLabels = dict.enums.grammaticalNumber as Record<
    string,
    string
  >;
  const translationPolicyLabels = dict.enums.translationPolicy as Record<
    string,
    string
  >;
  const priorityLabels = dict.enums.entityPriority as Record<string, string>;

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

  const structureQuery = useQuery({
    queryKey: ["struttura", projectId],
    queryFn: () => getStructure(projectId as string),
    enabled: Boolean(projectId),
  });
  const chapters = useMemo(
    () => chapterLabels(structureQuery.data?.nodes ?? []),
    [structureQuery.data]
  );
  const [chapterFilter, setChapterFilter] = useState<string | null>(null);
  const [activeChapter, setActiveChapter] = useState<string | null>(null);

  // Filtri della tabella.
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  const [genderFilter, setGenderFilter] = useState<string | null>(null);
  const [numberFilter, setNumberFilter] = useState<string | null>(null);
  // Filtro sulla colonna Alias: "upper" = mostra solo entità con almeno un
  // alias monoparola con iniziale maiuscola; "lower" = entità con alias
  // monoparola ma nessuno in maiuscola; "" = tutti.
  const [aliasFilter, setAliasFilter] = useState<"upper" | "lower" | "">("");
  // Filtro "Traduzione": rapporti tra Nome (IT) e Nome (EN)/Alias.
  //  "" = tutti | "identical" = IT == EN | "shares-word" = IT condivide
  //  almeno una parola con EN | "same-as-en-and-alias" = IT == EN e IT
  //  coincide anche con (almeno uno dei) gli alias.
  const [translationFilter, setTranslationFilter] = useState<
    "" | "identical" | "shares-word" | "same-as-en-and-alias"
  >("");
  const [search, setSearch] = useState("");

  const [page, setPage] = useState(1);
  const PER_PAGE = 50;


  // Ordinamento della tabella (§6.6): colonna + direzione, toggle al click.
  type SortKey = "name" | "name_it" | "type" | "status" | "mentions" | "alias";
  const [sortKey, setSortKey] = useState<SortKey>("mentions");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  /**
   * Restituisce il primo alias monoparola GIÀ presente nel nome EN,
   * preferendo quello con iniziale maiuscola; in mancanza, il primo
   * alias monoparola comunque contenuto nel nome.
   */
  function pickAlias(
    entity: { aliases: string[]; canonical_source: string }
  ): string | undefined {
    const single = (a: string) => a.trim().split(/\s+/).length === 1;
    // solo alias monoparola GIÀ CONTENUTI nel nome EN (case-insensitive)
    const nameLower = entity.canonical_source.trim().toLocaleLowerCase("it");
    const inName = (a: string) =>
      nameLower.includes(a.trim().toLocaleLowerCase("it"));
    const candidates = entity.aliases.filter((a) => single(a) && inName(a));
    const byUpper = candidates.filter((a) => /^[A-Z]/.test(a));
    const pool = byUpper.length > 0 ? byUpper : candidates;
    return pool[0];
  }

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // default sensato per colonna: numeri decrescenti, testi crescenti
      setSortDir(key === "mentions" ? "desc" : "asc");
    }
    setPage(1);
  }

  const entitiesQuery = useQuery({
    queryKey: ["entità", projectId, statusFilter, typeFilter, genderFilter,
      numberFilter, chapterFilter, aliasFilter, translationFilter, page,
      sortKey, sortDir],
    queryFn: () =>
      listEntities(projectId as string, {
        status: statusFilter,
        entity_type: typeFilter,
        referential_gender: genderFilter,
        grammatical_number: numberFilter,
        chapter_id: chapterFilter,
        alias: aliasFilter,
        translation: translationFilter,
        sort: sortKey,
        order: sortDir,
        page,
        per_page: PER_PAGE,
      }),
    enabled: Boolean(projectId),
  });

  const chapterQuery = useQuery({
    queryKey: ["capitolo", projectId, activeChapter],
    queryFn: () =>
      activeChapter ? getChapterEntities(projectId as string, activeChapter)
        : Promise.resolve(null),
    enabled: Boolean(projectId && activeChapter),
  });

  // --- selezione righe (indipendente dallo stato approvazione) ---
  // Le azioni massive e il "select all" agiscono su TUTTE le pagine:
  // selectedRows può contenere id di entità non visibili nella pagina corrente.
  const [selectedRows, setSelectedRows] = useState<Set<string>>(new Set());
  const [allPagesMode, setAllPagesMode] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  function toggleRow(id: string) {
    setSelectedRows((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  /** Filtri correnti (senza paginazione) — usati da listAllEntities. */
  function currentFilters() {
    return {
      status: statusFilter,
      entity_type: typeFilter,
      referential_gender: genderFilter,
      grammatical_number: numberFilter,
      chapter_id: chapterFilter,
      alias: aliasFilter,
      translation: translationFilter,
    };
  }
  /** Seleziona/deseleziona su tutte le pagine (rispetta i filtri). */
  async function toggleAllPages() {
    if (allPagesMode) {
      setAllPagesMode(false);
      setSelectedRows(new Set());
      return;
    }
    setBulkBusy(true);
    try {
      const all = await listAllEntities(projectId as string, currentFilters());
      setSelectedRows(new Set(all.map((e) => e.id)));
      setAllPagesMode(true);
    } finally {
      setBulkBusy(false);
    }
  }

  // --- side panel (dettaglio entità) ---
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EntityDto | null>(null);
  const [evidence, setEvidence] = useState<EntityEvidenceDto[] | null>(null);
  const [versions, setVersions] = useState<
    | {
        version: number;
        action: string;
        snapshot: EntityDto;
        created_at: string | null;
      }[]
    | null
  >(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (!selectedId || !projectId) return;
      const [d, v] = await Promise.all([
        getEntity(projectId, selectedId),
        getEntityVersions(projectId, selectedId),
      ]);
      if (cancelled) return;
      setDetail(d);
      setVersions(v.versions ?? []);
      // carica le evidenze solo se l'entità ne ha
      if ((d.evidence?.length ?? 0) > 0) {
        const ev = await getEntityEvidence(projectId, selectedId);
        if (!cancelled) setEvidence(ev.evidence ?? []);
      } else {
        if (!cancelled) setEvidence(null);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [selectedId, projectId]);

  const invalidateAll = () => {
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["entità", projectId] }),
      queryClient.invalidateQueries({ queryKey: ["capitolo", projectId] }),
    ]);
  };

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      await invalidateAll();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : t("importPage.opFailed")
      );
    } finally {
      setBusy(false);
    }
  }

  // --- azioni tabella ---
  async function onExtract() {
    await run(async () => {
      await authFetch(`/api/v1/projects/${projectId}/entities/extract`, {
        method: "POST",
      });
    });
    setNotice(t("entitiesPage.extractStarted"));
  }

  async function onExport(fmt: "csv" | "tbx") {
    await run(async () => {
      const { blob, filename } = await exportEntities(projectId as string, fmt);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${filename}.${fmt}`;
      a.click();
      URL.revokeObjectURL(url);
    });
    setNotice(t("entitiesPage.exportReady", { fmt: fmt.toUpperCase() }));
  }

  async function onImport(file: File, fmt: "csv" | "tbx" | "auto") {
    await run(async () => {
      const { imported } = await importEntities(
        projectId as string, file, file.name, fmt
      );
      setNotice(t("entitiesPage.imported", { n: imported }));
    });
  }

  async function onBulkApprove(ids: string[]) {
    await run(async () => {
      await bulkStatusEntities(projectId as string, ids, "approved");
    });
    setNotice(t("entitiesPage.bulkApproved", { n: ids.length }));
    invalidateAll();
  }

  /** Riporta le entità selezionate allo stato "proposed" (§6.2.5 review). */
  async function onBulkPropose(ids: string[]) {
    await run(async () => {
      await bulkStatusEntities(projectId as string, ids, "proposed");
    });
    setNotice(t("entitiesPage.bulkProposed", { n: ids.length }));
    invalidateAll();
  }

  /** Approva su tutte le pagine (rispetta i filtri correnti). */
  async function bulkApproveAllPages() {
    setBulkBusy(true);
    try {
      const all = await listAllEntities(projectId as string, currentFilters());
      const ids = all.filter((e) => e.status !== "approved").map((e) => e.id);
      await bulkStatusEntities(projectId as string, ids, "approved");
      setNotice(t("entitiesPage.bulkApproved", { n: ids.length }));
      invalidateAll();
    } finally {
      setBulkBusy(false);
      setAllPagesMode(false);
      setSelectedRows(new Set());
    }
  }

  /**
   * "Traduci Nomi" sulle entità selezionate (usa il modello di traduzione).
   * Le chiamate LLM sono lente (~5-12s a blocco di 20 nomi): per liste
   * grandi il lavoro è spezzato in FETTE — ogni richiesta resta sotto i
   * timeout e l'utente vede l'avanzamento. Il commit è per blocco lato
   * server, quindi le fette già completate restano salvate.
   *
   * CHUNK = 40: il proxy rewrite di Next.js tronca le richieste a ~30s
   * ("socket hang up", ECONNRESET) — 100 entità ne impiegano ~50 e muoiono;
   * 40 entità ≈ 19s, margine sufficiente.
   */
  async function onTranslateNames(ids: string[]) {
    if (ids.length === 0) return;
    setBulkBusy(true);
    let translated = 0;
    let untranslatable = 0;
    let failed = 0;
    const CHUNK = 40;
    try {
      for (let i = 0; i < ids.length; i += CHUNK) {
        const slice = ids.slice(i, i + CHUNK);
        setNotice(
          t("entitiesPage.namesProgress", {
            done: Math.min(i + slice.length, ids.length),
            total: ids.length,
          })
        );
        const res = await translateEntityNames(
          projectId as string, slice
        );
        translated += res.translated;
        untranslatable += res.untranslatable;
        failed += res.failed;
        invalidateAll();
      }
      setNotice(
        t("entitiesPage.namesDone", {
          total: ids.length,
          translated,
          untranslatable,
          failed: failed ? t("entitiesPage.failedSuffix", { n: failed }) : "",
        })
      );
      invalidateAll();
    } finally {
      setBulkBusy(false);
    }
  }

  /** Propone su tutte le pagine (rispetta i filtri correnti). */
  async function bulkProposeAllPages() {
    setBulkBusy(true);
    try {
      const all = await listAllEntities(projectId as string, currentFilters());
      await bulkStatusEntities(
        projectId as string, all.map((e) => e.id), "proposed"
      );
      setNotice(t("entitiesPage.bulkProposed", { n: all.length }));
      invalidateAll();
    } finally {
      setBulkBusy(false);
      setAllPagesMode(false);
      setSelectedRows(new Set());
    }
  }

  // --- editing singolo (side panel) ---
  async function saveField(field: keyof EntityDto, value: unknown) {
    if (!detail) return;
    await run(async () => {
      const updated = await updateEntity(projectId as string, detail.id, {
        [field]: value,
      } as Record<string, unknown>);
      setDetail(updated);
    });
  }

  async function addAlias() {
    const alias = (document.getElementById("new-alias") as HTMLInputElement)
      ?.value.trim();
    if (!alias || !detail) return;
    await run(async () => {
      const updated = await addEntityAlias(
        projectId as string, detail.id, alias
      );
      setDetail(updated);
    });
    if (document.getElementById("new-alias"))
      (document.getElementById("new-alias") as HTMLInputElement).value = "";
  }

  async function removeAlias(aliasId: string) {
    if (!detail) return;
    await run(async () => {
      const updated = await removeEntityAlias(
        projectId as string, detail.id, aliasId
      );
      setDetail(updated);
    });
  }

  async function doMerge(targetId: string) {
    if (!detail) return;
    await run(async () => {
      const updated = await mergeEntity(projectId as string, detail.id, targetId);
      setDetail(updated);
      setSelectedId(null);
    });
  }

  async function doSplit() {
    const alias = (document.getElementById("split-alias") as HTMLInputElement)
      ?.value.trim();
    const target = (document.getElementById("split-target") as HTMLInputElement)
      ?.value.trim();
    if (!alias || !detail) return;
    await run(async () => {
      await splitEntity(
        projectId as string, detail.id, alias, alias, target || null
      );
    });
    if (document.getElementById("split-alias"))
      (document.getElementById("split-alias") as HTMLInputElement).value = "";
    if (document.getElementById("split-target"))
      (document.getElementById("split-target") as HTMLInputElement).value = "";
    setNotice(t("entitiesPage.splitStarted"));
  }
  // Ordinamento SERVER-SIDE (§6.6): il backend ordina tutte le righe prima
  // della paginazione, quindi l'ordine è corretto su tutte le pagine.
  // Il filtro alias (maiuscoli/minuscoli) resta client-side sulla pagina
  // corrente (i filtri di tabella server-side sono quelli sopra).
  const sortedEntities = entitiesQuery.data?.entities ?? [];

  // --- caricamento / stato ---
  if (projectsQuery.isLoading) {
    return <Loading label={t("projects.loading")} />;
  }
  if (projectsQuery.isError) {
    return <ErrorNote message={t("importPage.loadError")} />;
  }
  if (!projectId) {
    return <ErrorNote message={t("preview.noProjects")} />;
  }

  const allTypes = entitiesQuery.data
    ? [...new Set(entitiesQuery.data.entities.map((e) => e.entity_type))]
    : [];
  const allGenders = entitiesQuery.data
    ? [...new Set(entitiesQuery.data.entities.map((e) => e.referential_gender))]
    : [];
  const allNumbers = entitiesQuery.data
    ? [
        ...new Set(
          entitiesQuery.data.entities.map((e) => e.grammatical_number)
        ),
      ]
    : [];

  const selectedEntity = entitiesQuery.data?.entities.find(
    (e) => e.id === selectedId
  ) ?? detail;

  // Ordinamento client-side della pagina corrente (§6.6): la paginazione
  // resta server-side; il sort riordina le righe visibili.

  return (
    <div className="mx-auto max-w-7xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">
            {t("entitiesPage.title")}
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            {t("entitiesPage.subtitle")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="btn-primary"
            onClick={onExtract}
            disabled={busy}
          >
            {t("entitiesPage.extract")}
          </button>
          <label className="btn-secondary">
            {t("entitiesPage.importBtn")}
            <input
              type="file"
              className="hidden"
              accept=".csv,.tbx,.xml"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void onImport(f, "auto");
              }}
            />
          </label>
          <select
            className="input"
            aria-label={t("entitiesPage.exportAria")}
            onChange={(e) => void onExport(e.target.value as "csv" | "tbx")}
          >
            <option value="csv">{t("entitiesPage.exportCsv")}</option>
            <option value="tbx">{t("entitiesPage.exportTbx")}</option>
          </select>
        </div>
      </header>

      {notice && (
        <div className="mt-3 rounded-md border border-emerald-800 bg-emerald-950/40 px-4 py-2 text-sm text-emerald-200">
          {notice}
        </div>
      )}
      {error && <ErrorNote message={error} />}

      {/* Vista capitolo: intro + delta (§6.6) */}
      <section className="mt-6" aria-labelledby="capitolo-title">
        <div className="flex items-center justify-between gap-3">
          <h2 id="capitolo-title" className="text-sm font-semibold text-white">
            {t("entitiesPage.chapterTitle")}
          </h2>
          <select
            className="input"
            aria-label={t("entitiesPage.chapterAria")}
            value={activeChapter ?? ""}
            onChange={(e) => setActiveChapter(e.target.value || null)}
          >
            <option value="">-</option>
            {chapters.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
        </div>
        {chapterQuery.data && (
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div className="card space-y-1.5">
              <p className="text-xs text-slate-400">
                {t("entitiesPage.introduced")}
              </p>
              <p className="text-lg font-bold text-white">
                {chapterQuery.data.introduced_count}
              </p>
              <ul className="space-y-1">
                {chapterQuery.data.introduced.map((e) => (
                  <li key={e.id} className="text-xs text-slate-300">
                    {e.canonical_source}
                  </li>
                ))}
              </ul>
            </div>
            <div className="card space-y-1.5">
              <p className="text-xs text-slate-400">
                {t("entitiesPage.newDelta")}
              </p>
              <p className="text-lg font-bold text-white">
                {chapterQuery.data.new_count}
              </p>
              <ul className="space-y-1">
                {chapterQuery.data.delta.map((e) => (
                  <li key={e.id} className="text-xs text-emerald-300">
                    {e.canonical_source}
                  </li>
                ))}
              </ul>
            </div>
            <div className="card space-y-1.5">
              <p className="text-xs text-slate-400">
                {t("entitiesPage.returnedDelta")}
              </p>
              <p className="text-lg font-bold text-white">
                {chapterQuery.data.returned.length}
              </p>
              <ul className="space-y-1">
                {chapterQuery.data.returned.map((e) => (
                  <li key={e.id} className="text-xs text-slate-300">
                    {e.canonical_source}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </section>

      {/* Tabella filtrabile (§6.6 / AC2) */}
      <section className="mt-6" aria-labelledby="tabella-title">
        <h2 id="tabella-title" className="mb-2 text-sm font-semibold text-white">
          {t("entitiesPage.tableTitle", {
            n: entitiesQuery.data?.total ?? 0,
          })}
        </h2>

        {/* Filtri */}
        <div className="mb-3 flex flex-wrap gap-3">
          <input
            type="search"
            className="input"
            placeholder={t("entitiesPage.searchPlaceholder")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select
            className="input"
            aria-label={t("entitiesPage.statusAria")}
            value={statusFilter ?? ""}
            onChange={(e) => {
              setStatusFilter(e.target.value || null);
              setPage(1);
            }}
          >
            <option value="">{t("entitiesPage.statusAll")}</option>
            {Object.entries(STATUS_CLASS).map(([k]) => (
              <option key={k} value={k}>
                {entityStatusLabels[k] ?? k}
              </option>
            ))}
          </select>
          <select
            className="input"
            aria-label={t("entitiesPage.typeAria")}
            value={typeFilter ?? ""}
            onChange={(e) => {
              setTypeFilter(e.target.value || null);
              setPage(1);
            }}
          >
            <option value="">{t("entitiesPage.typeAll")}</option>
            {allTypes.map((ty) => (
              <option key={ty} value={ty}>
                {entityTypeLabels[ty] ?? ty}
              </option>
            ))}
          </select>
          <select
            className="input"
            aria-label={t("entitiesPage.genderAria")}
            value={genderFilter ?? ""}
            onChange={(e) => {
              setGenderFilter(e.target.value || null);
              setPage(1);
            }}
          >
            <option value="">{t("entitiesPage.genderAll")}</option>
            {allGenders.map((g) => (
              <option key={g} value={g}>
                {referentialGenderLabels[g] ?? g}
              </option>
            ))}
          </select>
          <select
            className="input"
            aria-label={t("entitiesPage.numberAria")}
            value={numberFilter ?? ""}
            onChange={(e) => {
              setNumberFilter(e.target.value || null);
              setPage(1);
            }}
          >
            <option value="">{t("entitiesPage.numberAll")}</option>
            {allNumbers.map((n) => (
              <option key={n} value={n}>
                {grammaticalNumberLabels[n] ?? n}
              </option>
            ))}
          </select>
          <select
            className="input"
            aria-label={t("entitiesPage.aliasAria")}
            value={aliasFilter}
            onChange={(e) => {
              setAliasFilter(e.target.value as "upper" | "lower" | "");
              setPage(1);
            }}
          >
            <option value="">{t("entitiesPage.aliasAll")}</option>
            <option value="upper">{t("entitiesPage.aliasUpper")}</option>
            <option value="lower">{t("entitiesPage.aliasLower")}</option>
          </select>
          <select
            className="input"
            aria-label={t("entitiesPage.translationAria")}
            value={translationFilter}
            onChange={(e) => {
              setTranslationFilter(
                e.target.value as
                  | "" | "identical" | "shares-word" | "same-as-en-and-alias"
              );
              setPage(1);
            }}
          >
            <option value="">{t("entitiesPage.translationAll")}</option>
            <option value="identical">
              {t("entitiesPage.translationIdentical")}
            </option>
            <option value="shares-word">
              {t("entitiesPage.translationSharesWord")}
            </option>
            <option value="same-as-en-and-alias">
              {t("entitiesPage.translationSameAsAlias")}
            </option>
          </select>
          <select
            className="input"
            aria-label={t("entitiesPage.chapterAria2")}
            value={chapterFilter ?? ""}
            onChange={(e) => {
              setChapterFilter(e.target.value || null);
              setPage(1);
            }}
          >
            <option value="">{t("entitiesPage.chapterAll")}</option>
            {chapters.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => {
              setStatusFilter(null);
              setTypeFilter(null);
              setGenderFilter(null);
              setNumberFilter(null);
              setChapterFilter(null);
              setAliasFilter("");
              setTranslationFilter("");
              setSearch("");
              setPage(1);
            }}
          >
            {t("entitiesPage.clearFilters")}
          </button>
        </div>

        {/* Azioni massive (§6.2.5) sulle righe selezionate */}
        {entitiesQuery.data && entitiesQuery.data.entities.length > 0 && (
          <div className="mb-1 flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={allPagesMode}
                onChange={() => void toggleAllPages()}
                disabled={bulkBusy}
              />
              {t("entitiesPage.allPages")}
            </label>
            <span className="text-xs text-slate-400">
              {bulkBusy
                ? t("entitiesPage.bulkBusy")
                : selectedRows.size > 0
                ? t("entitiesPage.selectedCount", {
                    n: selectedRows.size,
                    extra: allPagesMode
                      ? t("entitiesPage.allPagesExtra")
                      : "",
                  })
                : t("entitiesPage.selectHint")}
            </span>
            <button
              type="button"
              className="btn-primary"
              disabled={
                bulkBusy ||
                (allPagesMode ? false : selectedRows.size === 0)
              }
              onClick={() => {
                if (allPagesMode) {
                  void bulkApproveAllPages();
                } else {
                  void onBulkApprove([...selectedRows]);
                  setSelectedRows(new Set());
                }
              }}
            >
              {allPagesMode
                ? t("entitiesPage.approveAll")
                : t("entitiesPage.approveSelected")}
            </button>
            <button
              type="button"
              className="btn"
              disabled={
                bulkBusy ||
                (allPagesMode ? false : selectedRows.size === 0)
              }
              onClick={() => {
                if (allPagesMode) {
                  void bulkProposeAllPages();
                } else {
                  void onBulkPropose([...selectedRows]);
                  setSelectedRows(new Set());
                }
              }}
            >
              {allPagesMode
                ? t("entitiesPage.proposeAll")
                : t("entitiesPage.proposeSelected")}
            </button>
            <button
              type="button"
              className="btn"
              disabled={
                bulkBusy ||
                (allPagesMode ? false : selectedRows.size === 0)
              }
              onClick={() => {
                if (allPagesMode) {
                  void (async () => {
                    setBulkBusy(true);
                    try {
                      const all = await listAllEntities(projectId as string, currentFilters());
                      await onTranslateNames(all.map((e) => e.id));
                    } finally {
                      setBulkBusy(false);
                      setAllPagesMode(false);
                      setSelectedRows(new Set());
                    }
                  })();
                } else {
                  void onTranslateNames([...selectedRows]).then(() =>
                    setSelectedRows(new Set())
                  );
                }
              }}
            >
              {allPagesMode
                ? t("entitiesPage.translateNamesAll")
                : t("entitiesPage.translateNames")}
            </button>
            {selectedRows.size > 0 && !allPagesMode && (
              <button
                type="button"
                className="btn"
                onClick={() => setSelectedRows(new Set())}
              >
                {t("entitiesPage.deselectAll")}
              </button>
            )}
          </div>
        )}

        {/* Tabella */}
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-800 bg-slate-900/60 text-xs text-slate-400">
              <tr>
                <th className="px-3 py-1.5 font-medium">
                  <input
                    type="checkbox"
                    className="mr-10"
                    aria-label={t("entitiesPage.selectAllAria")}
                    checked={
                      allPagesMode ||
                      (sortedEntities.length > 0 &&
                        sortedEntities.every((en) => selectedRows.has(en.id)))
                    }
                    onChange={(e) => {
                      if (allPagesMode) {
                        void toggleAllPages(); // usciva da tutte-le-pagine
                      } else if (e.target.checked) {
                        void toggleAllPages();
                      } else {
                        setSelectedRows(new Set());
                      }
                    }}
                  />
                  Select
                </th>
                {([
                  { key: "alias", label: t("entitiesPage.colAlias") },
                  { key: "name", label: t("entitiesPage.colName") },
                  { key: "name_it", label: t("entitiesPage.colNameIt") },
                  { key: "type", label: t("entitiesPage.colType") },
                  { key: "status", label: t("entitiesPage.colStatus") },
                  { key: "mentions", label: t("entitiesPage.colMentions") },
                ] as { key: SortKey; label: string }[]).map(({ key, label }) => (
                  <th key={key} className="px-3 py-1.5 font-medium">
                    <button
                      type="button"
                      className="flex items-center gap-1 hover:text-white"
                      onClick={() => toggleSort(key)}
                      aria-label={t("entitiesPage.sortAria", {
                        label,
                        dir:
                          sortKey === key
                            ? sortDir === "asc"
                              ? t("entitiesPage.sortAsc")
                              : t("entitiesPage.sortDesc")
                            : "",
                      })}
                    >
                      {label}
                      <span className="text-slate-500" aria-hidden="true">
                        {sortKey === key
                          ? sortDir === "asc" ? "▲" : "▼"
                          : "↕"}
                      </span>
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {entitiesQuery.isLoading && (
                <tr>
                  <td colSpan={7} className="py-6 text-center text-slate-400">
                    {t("entitiesPage.loadingEntities")}
                  </td>
                </tr>
              )}
              {entitiesQuery.data &&
                sortedEntities
                  .filter(
                    (e) =>
                      !search ||
                      e.canonical_source.toLowerCase().includes(search.toLowerCase()) ||
                      e.aliases.some((a) =>
                        a.toLowerCase().includes(search.toLowerCase())
                      )
                  )
                  .map((e) => (
                    <tr
                      key={e.id}
                      className="cursor-pointer hover:bg-slate-800/40"
                      onClick={() => {
                        setSelectedId(e.id);
                        // resetta il dettaglio per forzare il caricamento
                        setDetail(null);
                        setEvidence(null);
                        setVersions(null);
                      }}
                    >
                      <td className="px-3 py-1.5">
                        <input
                          type="checkbox"
                          className="mr-10"
                          aria-label={t("entitiesPage.selectRowAria", {
                            name: e.canonical_source,
                          })}
                          checked={selectedRows.has(e.id)}
                          onChange={() => toggleRow(e.id)}
                          onClick={(ev) => ev.stopPropagation()}
                        />
                      </td>
                      <td className="px-3 py-1.5 text-slate-300">
                        {pickAlias(e) ?? "-"}
                      </td>
                      <td className="px-3 py-1.5 text-white">
                        {e.canonical_source}
                        {e.priority === "block_batch" && (
                          <span className="ml-1 badge bg-red-500/20 text-red-200">
                            {t("entitiesPage.blocksBatch")}
                          </span>
                        )}
                        {e.priority === "warn" && (
                          <span className="ml-1 badge bg-amber-500/20 text-amber-200">
                            warn
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-1.5 text-slate-300">
                        {e.canonical_target ?? "-"}
                      </td>
                      <td className="px-3 py-1.5 text-slate-300">
                        {entityTypeLabels[e.entity_type] ?? e.entity_type}
                        {e.referential_gender && (
                          <span className="ml-2 text-xs text-slate-500">
                            {referentialGenderLabels[e.referential_gender] ??
                              e.referential_gender}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-1.5">
                        <span
                          className={`badge ${STATUS_CLASS[e.status] ?? "bg-slate-700 text-slate-200"}`}
                        >
                          {entityStatusLabels[e.status] ?? e.status}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 text-slate-300">
                        {e.mention_count}
                      </td>
                    </tr>
                  ))}
            </tbody>
          </table>
        </div>

        {/* Paginazione (§6.6 / AC2) */}
        {entitiesQuery.data && entitiesQuery.data.total > PER_PAGE && (
          <div className="mt-4 flex items-center justify-between">
            <button
              type="button"
              className="btn-secondary"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              {t("entitiesPage.prevPage")}
            </button>
            <span className="text-sm text-slate-400">
              {t("entitiesPage.pageOf", {
                page,
                total: Math.ceil(entitiesQuery.data.total / PER_PAGE),
              })}
            </span>
            <button
              type="button"
              className="btn-secondary"
              disabled={
                page * PER_PAGE >= entitiesQuery.data.total
              }
              onClick={() => setPage((p) => p + 1)}
            >
              {t("entitiesPage.nextPage")}
            </button>
          </div>
        )}
      </section>

      {/* Side panel di dettaglio (§15.2 / §6.6) */}
      {selectedEntity && (
        <SidePanel
          entity={selectedEntity}
          evidence={evidence}
          versions={versions}
          chapters={chapters}
          onClose={() => {
            setSelectedId(null);
            // §15.2: azzerare ANCHE detail/evidence/versions — altrimenti
            // `selectedEntity` cade su `detail` (non-null dal fetch della
            // entità precedente) e il pannello resta aperto dopo il Chiudi.
            setDetail(null);
            setEvidence(null);
            setVersions(null);
          }}
          onField={saveField}
          onAddAlias={addAlias}
          onRemoveAlias={removeAlias}
          onMerge={doMerge}
          onSplit={doSplit}
          allEntities={entitiesQuery.data?.entities ?? []}
        />
      )}
    </div>
  );
}

/**
 * Side panel di dettaglio (§15.2): menzioni/evidenze con quote e pagina,
 * merge/split di alias, forma IT approvata, generi, policy, checkbox e
 * priorità.
 */
function SidePanel({
  entity,
  evidence,
  versions,
  chapters,
  onClose,
  onField,
  onAddAlias,
  onRemoveAlias,
  onMerge,
  onSplit,
  allEntities,
}: {
  entity: EntityDto;
  evidence: EntityEvidenceDto[] | null;
  versions: {
    version: number;
    action: string;
    snapshot: EntityDto;
    created_at: string | null;
  }[] | null;
  chapters: { id: string; label: string }[];
  onClose: () => void;
  onField: (field: keyof EntityDto, value: unknown) => Promise<void>;
  onAddAlias: () => Promise<void>;
  onRemoveAlias: (aliasId: string) => Promise<void>;
  onMerge: (targetId: string) => Promise<void>;
  onSplit: () => Promise<void>;
  allEntities: EntityDto[];
}) {
  const { locale, t, dict } = useI18n();
  const entityStatusLabels = dict.enums.entityStatus as Record<string, string>;
  const entityTypeLabels = dict.enums.entityType as Record<string, string>;
  const referentialGenderLabels = dict.enums.referentialGender as Record<
    string,
    string
  >;
  const italianGenderLabels = dict.enums.italianGender as Record<string, string>;
  const grammaticalNumberLabels = dict.enums.grammaticalNumber as Record<
    string,
    string
  >;
  const translationPolicyLabels = dict.enums.translationPolicy as Record<
    string,
    string
  >;
  const priorityLabels = dict.enums.entityPriority as Record<string, string>;
  const [forbidden, setForbidden] = useState(entity.forbidden_targets.join(";"));
  const [itTarget, setItTarget] = useState(entity.canonical_target ?? "");
  const [mergeTarget, setMergeTarget] = useState("");

  // Campi testuali: stato locale + PATCH con debounce (fix 2026-09-19: prima
  // ogni tasto scatenava un PATCH e la risposta sovrascriveva il campo
  // ancora in digitazione — lentezza e lettere mescolate).
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  function editDebounced(
    field: "canonical_target" | "forbidden_targets",
    value: string
  ) {
    if (field === "canonical_target") setItTarget(value);
    else setForbidden(value);
    if (timers.current[field]) clearTimeout(timers.current[field]);
    timers.current[field] = setTimeout(() => {
      delete timers.current[field];
      if (field === "canonical_target") {
        void onField("canonical_target", value || null);
      } else {
        void onField(
          "forbidden_targets",
          value.split(";").map((s) => s.trim()).filter(Boolean)
        );
      }
    }, 600);
  }

  function flushDebounced(field: "canonical_target" | "forbidden_targets") {
    const t = timers.current[field];
    if (t) {
      clearTimeout(t);
      delete timers.current[field];
    }
    if (field === "canonical_target") {
      if (itTarget !== (entity.canonical_target ?? "")) {
        void onField("canonical_target", itTarget || null);
      }
    } else {
      const arr = forbidden.split(";").map((s) => s.trim()).filter(Boolean);
      if (JSON.stringify(arr) !== JSON.stringify(entity.forbidden_targets)) {
        void onField("forbidden_targets", arr);
      }
    }
  }

  // risincronizza i campi testuali quando cambia l'entità selezionata e
  // scarica i salvataggi pendenti prima di smontare
  useEffect(() => {
    setItTarget(entity.canonical_target ?? "");
    setForbidden(entity.forbidden_targets.join(";"));
    const pending = { ...timers.current };
    timers.current = {};
    return () => {
      for (const [field, t] of Object.entries(pending)) {
        clearTimeout(t);
        if (field === "canonical_target") {
          void onField("canonical_target", itTarget || null);
        } else {
          void onField(
            "forbidden_targets",
            forbidden.split(";").map((s) => s.trim()).filter(Boolean)
          );
        }
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entity.id]);

  const mergedFrom = entity.aliases.filter(
    (a) => a === entity.canonical_source
  );
  const otherEntities = allEntities.filter((e) => e.id !== entity.id);

  return (
    <div
      className="fixed inset-0 z-40 bg-black/50"
      onClick={onClose}
    >
      <div
        className="absolute right-0 top-0 h-full w-full max-w-xl overflow-y-auto bg-slate-900 border-l border-slate-800 px-4 py-3"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 flex items-start justify-between gap-4 border-b border-slate-800 bg-slate-900 pb-2 mb-2">
          <div>
            <h2 className="text-lg font-bold text-white">{entity.canonical_source}</h2>
            <p className="text-xs text-slate-400">
              {entityStatusLabels[entity.status] ?? entity.status} ·{" "}
              {entityTypeLabels[entity.entity_type] ?? entity.entity_type}
            </p>
          </div>
          <button
            type="button"
            className="btn-secondary"
            onClick={onClose}
            aria-label={t("entitiesPage.closeAria")}
          >
            {t("entitiesPage.close")}
          </button>
        </div>

        {/* Forma IT approvata (§6.6) */}
        <div className="mt-4 space-y-1.5">
          <label className="label" htmlFor="it-target">
            {t("entitiesPage.itForm")}
          </label>
          <input
            id="it-target"
            className="input"
            value={itTarget}
            placeholder={t("entitiesPage.itPlaceholder")}
            onChange={(e) => editDebounced("canonical_target", e.target.value)}
            onBlur={() => flushDebounced("canonical_target")}
          />
        </div>

        {/* Generi, numero, tipo, policy (§6.4) */}
        <div className="mt-4 grid grid-cols-2 gap-4">
          <SelectField
            label={t("entitiesPage.typeField")}
            value={entity.entity_type}
            options={Object.keys(entityTypeLabels)}
            labels={entityTypeLabels}
            onChange={(v) => void onField("entity_type", v)}
          />
          <SelectField
            label={t("entitiesPage.referentialField")}
            value={entity.referential_gender}
            options={Object.keys(referentialGenderLabels)}
            labels={referentialGenderLabels}
            onChange={(v) => void onField("referential_gender", v)}
          />
          <SelectField
            label={t("entitiesPage.italianGenderField")}
            value={entity.italian_grammatical_gender}
            options={Object.keys(italianGenderLabels)}
            labels={italianGenderLabels}
            onChange={(v) => void onField("italian_grammatical_gender", v)}
          />
          <SelectField
            label={t("entitiesPage.numberField")}
            value={entity.grammatical_number}
            options={Object.keys(grammaticalNumberLabels)}
            labels={grammaticalNumberLabels}
            onChange={(v) => void onField("grammatical_number", v)}
          />
          <SelectField
            label={t("entitiesPage.policyField")}
            value={entity.translation_policy}
            options={Object.keys(translationPolicyLabels)}
            labels={translationPolicyLabels}
            onChange={(v) => void onField("translation_policy", v)}
          />
        </div>

        {/* Checkbox (§6.6) */}
        <div className="mt-4 flex flex-wrap gap-6">
          <label className="flex items-center gap-1 text-sm text-slate-200">
            <input
              type="checkbox"
              checked={entity.never_translate}
              onChange={(e) =>
                void onField("never_translate", e.target.checked)
              }
            />
            {t("entitiesPage.neverTranslate")}
          </label>
          <label className="flex items-center gap-1 text-sm text-slate-200">
            <input
              type="checkbox"
              checked={entity.allow_inflection}
              onChange={(e) =>
                void onField("allow_inflection", e.target.checked)
              }
            />
            {t("entitiesPage.allowInflection")}
          </label>
        </div>

        {/* Forme vietate (§6.6) */}
        <div className="mt-4 space-y-1.5">
          <label className="label" htmlFor="forbidden">
            {t("entitiesPage.forbiddenLabel")}
          </label>
          <input
            id="forbidden"
            className="input"
            value={forbidden}
            placeholder={t("entitiesPage.forbiddenPlaceholder")}
            onChange={(e) => editDebounced("forbidden_targets", e.target.value)}
            onBlur={() => flushDebounced("forbidden_targets")}
          />
        </div>

        {/* Priorità (§6.6) */}
        <div className="mt-4 space-y-1.5">
          <label className="label" htmlFor="priority">
            {t("entitiesPage.priorityLabel")}
          </label>
          <select
            id="priority"
            className="input"
            value={entity.priority ?? ""}
            onChange={(e) =>
              void onField("priority", e.target.value || null)
            }
          >
            <option value="">-</option>
            {Object.entries(PRIORITY_CLASS).map(([k]) => (
              <option key={k} value={k}>
                {priorityLabels[k] ?? k}
              </option>
            ))}
          </select>
        </div>

        {/* Alias (§6.5) */}
        <div className="mt-4 space-y-1.5">
          <label className="label">{t("entitiesPage.aliasLabel")}</label>
          <div className="flex flex-wrap gap-1">
            {entity.aliases.map((a) => (
              <span
                key={a}
                className="inline-flex items-center gap-1 rounded bg-slate-800 px-15 py-0.5 text-xs text-slate-200"
              >
                {a}
                {a !== entity.canonical_source && (
                  <button
                    type="button"
                    className="text-slate-400 hover:text-white"
                    title={t("entitiesPage.removeAlias")}
                    onClick={() => void onRemoveAlias(a)}
                  >
                    ×
                  </button>
                )}
              </span>
            ))}
          </div>
          <div className="mt-1 flex gap-1">
            <input
              id="new-alias"
              className="input flex-1"
              placeholder={t("entitiesPage.newAliasPlaceholder")}
            />
            <button type="button" className="btn-secondary" onClick={onAddAlias}>
              {t("entitiesPage.add")}
            </button>
          </div>
        </div>

        {/* Merge / split (§15.2 / §6.6) */}
        <div className="mt-4 space-y-3">
          <div className="space-y-1.5">
            <label className="label">{t("entitiesPage.mergeLabel")}</label>
            <div className="flex gap-1">
              <select
                className="input flex-1"
                value={mergeTarget}
                onChange={(e) => setMergeTarget(e.target.value)}
              >
                <option value="">{t("entitiesPage.mergePlaceholder")}</option>
                {otherEntities
                  .filter((e) => e.status !== "merged")
                  .map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.canonical_source}
                    </option>
                  ))}
              </select>
              <button
                type="button"
                className="btn-secondary"
                disabled={!mergeTarget}
                onClick={() => void onMerge(mergeTarget)}
              >
                {t("entitiesPage.mergeBtn")}
              </button>
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="label">{t("entitiesPage.splitLabel")}</label>
            <input
              id="split-alias"
              className="input"
              placeholder={t("entitiesPage.splitAliasPlaceholder")}
            />
            <input
              id="split-target"
              className="input"
              placeholder={t("entitiesPage.splitTargetPlaceholder")}
            />
            <button
              type="button"
              className="btn-secondary"
              onClick={onSplit}
            >
              {t("entitiesPage.splitBtn")}
            </button>
          </div>
        </div>

        {/* Evidenze (§15.2 / §6.6) */}
        <div className="mt-4 space-y-3">
          <h3 className="text-sm font-semibold text-white">
            {t("entitiesPage.evidenceTitle", { n: evidence?.length ?? 0 })}
          </h3>
          {evidence && evidence.length === 0 && (
            <p className="text-sm text-slate-400">
              {t("entitiesPage.noEvidence")}
            </p>
          )}
          {evidence &&
            evidence.map((ev) => (
              <div key={ev.id} className="rounded border border-slate-800 bg-slate-800/40 p-20">
                <p className="text-xs text-slate-400">
                  {t("entitiesPage.pageAbbr", { n: ev.page_number ?? "?" })} ·{" "}
                  {ev.evidence_type ?? "?"}
                  {ev.chapter_id && (
                    <span className="ml-1">
                      · {chapters.find((c) => c.id === ev.chapter_id)?.label ?? ev.chapter_id}
                    </span>
                  )}
                  {ev.ocr_suspect && (
                    <span
                      className="badge ml-1 bg-red-500/20 text-red-200"
                      title={t("entitiesPage.ocrTitle")}
                    >
                      {t("importPage.ocrSuspect")}
                    </span>
                  )}
                </p>
                <blockquote className="mt-10 border-l-15 border-slate-600 pl-20 italic text-slate-200">
                  “{ev.quote_text}”
                </blockquote>
                {ev.context && ev.context.length > 0 && (
                  <div className="mt-10 space-y-10">
                    {ev.context.map((c) => (
                      <p key={c.ordinal} className="text-xs text-slate-400">
                        <span className="text-slate-500">[{c.ordinal}]</span>{" "}
                        {c.source_text}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            ))}
        </div>

        {/* Storico (§15.4) */}
        {versions && versions.length > 0 && (
          <div className="mt-4 space-y-1.5">
            <h3 className="text-sm font-semibold text-white">
              {t("entitiesPage.history")}
            </h3>
            <ul className="space-y-10">
              {versions.map((v) => (
                <li key={v.version} className="text-xs text-slate-400">
                  v{v.version} · {v.action}
                  {v.created_at &&
                    ` · ${new Date(v.created_at).toLocaleString(
                      locale === "it" ? "it-IT" : "en-GB"
                    )}`}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

/** Select con mapping valore → etichetta IT. */
function SelectField({
  label,
  value,
  options,
  labels,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  labels: Record<string, string>;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-1.5">
      <label className="label">{label}</label>
      <select
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o} value={o}>
            {labels[o] ?? o}
          </option>
        ))}
      </select>
    </div>
  );
}
