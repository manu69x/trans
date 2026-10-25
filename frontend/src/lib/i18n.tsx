"use client";

/**
 * i18n minimale client-side: LocaleProvider + useI18n (contestuale, senza
 * dipendenze esterne). L'app è interamente client-rendered, quindi la lingua
 * vive in un context React e il cambio è istantaneo, senza ricaricare.
 *
 * - Prima visita: auto-rilevata da navigator.language (it* → IT, resto → EN).
 * - Scelta manuale persistita in localStorage ("trans.locale").
 * - <html lang> tenuto sincronizzato dall'effetto del provider.
 *
 * Il dizionario IT (src/lib/dictionaries/it.ts) è la source of truth:
 * `Dict = typeof it` tipizza anche EN, quindi la build segnala ogni chiave
 * mancante o in eccesso. `t()` accetta solo percorsi esistenti (DeepKeys).
 * I record enumerativi (etichette di stato, MQM, …) si leggono da `dict`
 * direttamente: `dict.enums.jobStatus[code] ?? code`.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { it } from "./dictionaries/it";
import { en } from "./dictionaries/en";

export type Locale = "it" | "en";
export type Dict = typeof it;

/** Percorsi puntati delle stringhe ("nav.dashboard", "dash.emptyNew", …). */
type DeepKeys<T> = T extends string
  ? never
  : {
      [K in keyof T & string]: T[K] extends string
        ? K
        : `${K}.${DeepKeys<T[K]>}`;
    }[keyof T & string];

export type DictKey = DeepKeys<Dict>;

const DICTS: Record<Locale, Dict> = { it, en };
const STORAGE_KEY = "trans.locale";

/** Auto-rilevamento: italiano per i browser it*, inglese per tutti gli altri. */
function detectLocale(): Locale {
  if (typeof navigator === "undefined") return "it";
  const langs = Array.isArray(navigator.languages)
    ? navigator.languages
    : [navigator.language];
  for (const lang of langs) {
    if (!lang) continue;
    const low = lang.toLowerCase();
    if (low.startsWith("it")) return "it";
    if (low.startsWith("en")) return "en";
  }
  return "en";
}

function lookup(dict: Dict, key: string): string | undefined {
  let node: unknown = dict;
  for (const part of key.split(".")) {
    if (node && typeof node === "object" && part in node) {
      node = (node as Record<string, unknown>)[part];
    } else {
      return undefined;
    }
  }
  return typeof node === "string" ? node : undefined;
}

/** Sostituisce i segnaposto {param} nel template. */
function interpolate(
  template: string,
  params?: Record<string, string | number>
): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in params ? String(params[name]) : match
  );
}

type I18nContextValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  /** Dizionario corrente (per i record enumerativi). */
  dict: Dict;
  /** Stringa per chiave puntata, con interpolazione {param}. */
  t: (key: DictKey, params?: Record<string, string | number>) => string;
};

const I18nContext = createContext<I18nContextValue | null>(null);

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>("it");

  // Primo mount: preferenza salvata, altrimenti auto-rilevata.
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored === "it" || stored === "en") {
        setLocaleState(stored);
        return;
      }
    } catch {
      // localStorage non disponibile: resta l'auto-rilevamento.
    }
    setLocaleState(detectLocale());
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Solo in memoria se lo storage è bloccato.
    }
  }, []);

  const t = useCallback<I18nContextValue["t"]>(
    (key, params) => {
      const template =
        lookup(DICTS[locale], key) ?? lookup(DICTS.it, key) ?? key;
      return interpolate(template, params);
    },
    [locale]
  );

  const value = useMemo(
    () => ({ locale, setLocale, dict: DICTS[locale], t }),
    [locale, setLocale, t]
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    throw new Error("useI18n richiede <LocaleProvider> nell'albero React.");
  }
  return ctx;
}
