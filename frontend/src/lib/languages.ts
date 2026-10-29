/**
 * Catalogo lingue abilitate (multilingua Tier 1, 2026-10-29).
 *
 * Mirror di `backend/backend/languages.py`: la source è sempre inglese
 * (l'estrazione entità BookNLP upstream è inglese-only), i target Tier 1
 * sono le lingue core TranslateGemma delle grandi editorie EN→X.
 * Il backend valida comunque la coppia (422 su coppia non abilitata).
 */

export interface TargetLanguage {
  code: string;
  /** Etichetta inglese ("Spanish"). */
  name_en: string;
  /** Etichetta italiana ("Spagnolo"). */
  name_it: string;
}

export const SUPPORTED_SOURCE = "en";

export const SUPPORTED_TARGETS: TargetLanguage[] = [
  { code: "it", name_en: "Italian", name_it: "Italiano" },
  { code: "es", name_en: "Spanish", name_it: "Spagnolo" },
  { code: "fr", name_en: "French", name_it: "Francese" },
  { code: "de", name_en: "German", name_it: "Tedesco" },
  { code: "pt", name_en: "Portuguese", name_it: "Portoghese" },
];
