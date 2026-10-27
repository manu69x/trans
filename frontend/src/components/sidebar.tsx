"use client";

/**
 * Sidebar di navigazione (client component): voci e testi dal dizionario
 * i18n, include lo switcher di lingua IT/EN. Estratta da layout.tsx (che
 * resta un server component) perché le label dipendono dal locale scelto
 * dall'utente a runtime.
 */

import Link from "next/link";

import TranslationProgress from "./translation-progress";
import QueueStopButton from "./queue-stop-button";
import UserMenu from "./user-menu";
import { useI18n, type Locale } from "@/lib/i18n";
import type { ITDict } from "@/lib/dictionaries/it";

type NavKey = Exclude<keyof ITDict["nav"], "main">;

/** Voci di navigazione principale (PRD §11.1) — label via dizionario. */
const NAV: { href: string; key: NavKey }[] = [
  { href: "/", key: "dashboard" },
  { href: "/progetti", key: "progetti" },
  { href: "/import", key: "import" },
  { href: "/entita", key: "entita" },
  { href: "/segmenti", key: "segmenti" },
  { href: "/traduzione", key: "traduzione" },
  { href: "/qa", key: "qa" },
  { href: "/prompt-modelli", key: "promptModelli" },
  { href: "/anteprima", key: "anteprima" },
  { href: "/export", key: "export" },
  { href: "/verifica-libro", key: "verificaLibro" },
  { href: "/info", key: "info" },
];

function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n();
  return (
    <div className="flex items-center justify-between gap-2 px-4 py-2">
      <span className="text-xs text-slate-500">{t("sidebar.language")}</span>
      <div
        role="group"
        aria-label={t("sidebar.changeLanguage")}
        className="flex overflow-hidden rounded-md border border-slate-700"
      >
        {(["it", "en"] as Locale[]).map((code) => (
          <button
            key={code}
            type="button"
            onClick={() => setLocale(code)}
            aria-pressed={locale === code}
            className={`px-2 py-0.5 text-xs font-semibold uppercase ${
              locale === code
                ? "bg-slate-700 text-white"
                : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
            }`}
          >
            {code}
          </button>
        ))}
      </div>
    </div>
  );
}

export default function Sidebar() {
  const { t } = useI18n();
  return (
    <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col overflow-y-auto border-r border-slate-800 bg-slate-900/40">
      <div className="border-b border-slate-800 px-4 py-4">
        <Link href="/" className="text-lg font-bold leading-tight tracking-tight">
          Free Book Translation
        </Link>
        <p className="mt-0.5 text-xs text-slate-400">{t("sidebar.tagline")}</p>
      </div>
      <nav className="flex-1 space-y-0.5 px-2 py-2" aria-label={t("nav.main")}>
        {NAV.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className="block rounded-md px-3 py-1 text-sm leading-5 text-slate-300 hover:bg-slate-800 hover:text-white"
          >
            {t(`nav.${item.key}` as const)}
          </Link>
        ))}
      </nav>
      <TranslationProgress />
      <QueueStopButton />
      <UserMenu />
      <LanguageSwitcher />
      <div className="border-t border-slate-800 px-4 py-3 text-xs text-slate-500">
        {t("sidebar.privacy")}
      </div>
    </aside>
  );
}
