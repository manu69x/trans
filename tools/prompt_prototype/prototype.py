#!/usr/bin/env python3
"""Prototipo del prompt di traduzione (§9.4, §9.5) su LLM Gateway.

Fatti:
  1. Costruisce il prompt §9.4 POPOLATO (RUOLO/PRIORITÀ/PROFILO/GLOSSARIO/TM/
     CONTESTO/TESTO con ID stabili) con dati fittizi realistici.
  2. Conta i TOKEN REALI del prompt popolato con più metodi:
       - stima conservativa (~4 char/token, inglese)
       - word-based x1.4
       - tiktoken se disponibile (cl100k_base)
     e li confronta col budget PRD §5.4/§8.1 (16.384 token totali;
     10-11k sorgente / 2-2.5k contesto / 2-3k output).
  3. Chiama Gateway con `response_format: {type:json_object}` +
     `reasoning_effort:none` (modello di traduzione da ADR-001).
  4. Valida l'output contro lo schema §9.5 E controlla la TERMINOLOGIA:
     ogni voce GLOSSARIO marcata "non tradurre" DEVA comparire identica.
  5. Ripete N prove; misura % blocchi validi (JSON+schema+ID+terminologia)
     al primo colpo.

Uso:
    python3 prototype.py --trials 10 --model <ID> --out results.json
    python3 prototype.py --trials 10 --dump-prompt        # solo mostra un prompt

Riferimenti PRD: §5.4, §8.1, §9.4, §9.5, §16 (Fase 0).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Costanti / configurazione
# --------------------------------------------------------------------------- #
BUDGET_TOTAL = 16384
BUDGET_SORGENTE_BASSO = 10000
BUDGET_SORGENTE_ALTO = 11000
BUDCONTestO = 2500
BUDGET_OUTPUT = 2500

GATEWAY_BASE = os.getenv("LLM_GATEWAY_BASE_URL", "http://127.0.0.1:8080/v1")
PHX_TOKEN = "sk-local-dev-change-me"
DEFAULT_MODEL = os.getenv("TRANS_TEXT_MODEL", "default")

SCHEMA_95 = {
    "translations": [
        {
            "segment_id": "string",
            "target_text": "string",
            "used_entity_ids": ["string"],
            "term_violations": [],
            "flags": [
                {
                    "type": "gender_ambiguous|term_ambiguous|ocr_suspect|"
                            "source_ambiguous|other",
                    "message": "string",
                }
            ],
        }
    ]
}

# --------------------------------------------------------------------------- #
# Dati fittizi realistici (corpus di test)
# --------------------------------------------------------------------------- #
# Paragrafo EN reale estratto da docs/benchmarks/corpus (native_01, LITERARY).
SOURCE_PARAGRAPH = (
    "The morning came grey and soft over the harbour of Saint Albans. Maren stood "
    "at the window of the small kitchen and watched the boats lean against their "
    "moorings, their hulls dripping with the tide that had left them long ago. Her "
    "father's coat still hung on the peg by the door, though he had not worn it for "
    "ten years. 'You are thinking about him again,' said her mother, without turning "
    "from the stove. The kettle began to murmur a low, patient tune."
)

# CONTESTO precedente (read-only), 2 segmenti prima + 1 dopo (overlap §5.4.r6).
PREVIOUS_CONTEXT = (
    "[DO_NOT_TRANSLATE_CONTEXT] The night had fallen cold over Saint Albans. Maren "
    "went to bed early, unable to sleep, listening to the sea against the harbour "
    "walls."
)

# GLOSSARIO: 5 voci. `keep` = forma "non tradurre" che DEVA comparire identica.
GLOSSARY = [
    {"src": "Saint Albans", "it": "Saint Albans", "keep": True,
     "entity_id": "ent-saint-albans-0001",
     "note": "Nome proprio — non tradurre."},
    {"src": "Maren", "it": "Maren", "keep": True, "entity_id": "ent-maren-0002",
     "note": "Nome proprio (PERSON, genere referenziale: femminile)."},
    {"src": "harbour", "it": "porto", "keep": False, "entity_id": "ent-harbour-0003",
     "note": "Concetto — genere grammaticale: maschile."},
    {"src": "moorings", "it": "pali", "keep": False, "entity_id": "ent-moorings-0004",
     "note": "Termine nautico."},
    {"src": "kettle", "it": "marmetta", "keep": False, "entity_id": "ent-kettle-0005",
     "note": "Oggetto casalingo."},
]

# MEMORIA DI TRADUZIONE: 2 esempi approvati (solo per coerenza).
TM_MATCHES = [
    {"src": "The night had fallen cold over Saint Albans.",
     "it": "La notte era caduta fredda over Saint Albans.",
     "match_score": 0.94},
    {"src": "You are thinking about him again.",
     "it": "Stai pensando di nuovo a lui.",
     "match_score": 0.88},
]

GENRE_STYLE_PROMPT = (
    "GENE: dramma lirico / realismo memoriale. TONO: prosa lenta, atmosferica, "
    "ricallatricia. VOCE:第三人称 indiretto libero. PREGIO EVITARE: calchi "
    "inglese, registri colloquiali non giustificati."
)

# --------------------------------------------------------------------------- #
# Costruzione del prompt (§9.4)
# --------------------------------------------------------------------------- #
def build_prompt():
    gloss_lines = []
    for g in GLOSSARY:
        flag = "NON TRADURRE" if g["keep"] else "traduci"
        gloss_lines.append(
            f"- [{flag}] {g['src']} -> {g['it']}  (ID:{g['entity_id']})  {g['note']}"
        )
    tm_lines = [
        f"- [{m['match_score']:.2f}] {m['src']} :: {m['it']}" for m in TM_MATCHES
    ]

    # Segmento con ID stabili (un segmento = il paragrafo intero, per il prototipo).
    seg_id = "seg-0001"
    segment_batch = f"[{seg_id}] {SOURCE_PARAGRAPH}"

    prompt = f"""RUOLO
Sei un traduttore editoriale professionista dall'inglese all'italiano.
Traduci con fedeltà semantica, naturalezza letteraria italiana e coerenza assoluta
con il glossario approvato. Non riassumere, non censurare, non aggiungere
spiegazioni, non omettere contenuto e non inventare dettagli.

PRIORITÀ
1. Conserva esattamente gli ID dei segmenti, i tag e i placeholder.
2. Applica le voci GLOSSARIO/ENTITÀ approvate. Le forme "non tradurre" devono
   rimanere identiche salvo flessione esplicitamente consentita.
3. Per PERSON usa il genere referenziale solo quando è fornito come evidenza.
   Per oggetti/luoghi/concetti usa il genere grammaticale italiano fornito.
   Se il dato è unknown, non inventare un genere: usa una resa naturale evitando
   assunzioni quando possibile e segnala il dubbio nel campo flags.
4. Le marcature [[POSS:...]] 's indicano possessivo inglese. Rendilo in italiano
   in modo naturale; non emettere mai tali marcature.
5. Il token isolato "I" è pronome personale inglese di prima persona, mai un nome proprio.
6. Mantieni voce, tempo verbale, focalizzazione, ritmo dei dialoghi e punteggiatura significativa.
7. Il contesto è solo per comprensione: non tradurlo né restituirlo.

PROFILO GENERE
{GENRE_STYLE_PROMPT}

GLOSSARIO ED ENTITÀ
{chr(10).join(gloss_lines)}

MEMORIA DI TRADUZIONE (solo esempi approvati; adatta al contesto)
{chr(10).join(tm_lines)}

CONTESTO PRECEDENTE (non tradurre)
{PREVIOUS_CONTEXT}

TESTO DA TRADURRE
{segment_batch}

OUTPUT
Restituisci esclusivamente JSON conforme ALLO SCHEMA §9.5. La ROOT deve essere un oggetto con una sola chiave "translations" (lista). Un item per ogni ID ricevuto, in quest'ordine:

{{
  "translations": [
    {{
      "segment_id": "seg-0001",
      "target_text": "<la tua traduzione di questo segmento>",
      "used_entity_ids": ["ent-saint-albans-0001","ent-maren-0002"],
      "term_violations": [],
      "flags": []
    }}
  ]
}}

Regole:
- La ROOT è l'oggetto con la sola chiave "translations"; NON usare {{"seg-0001": "..."}} come forma di risposta.
- "segment_id" = l'ID ricevuto ("seg-0001"). In "used_entity_ids" metti gli ID delle voci GLOSSARIO/ENTITÀ realmente usate nella resa.
- "term_violations": lascia [] se tutte le voci "non tradurre" sono identiche; riporta qui eventuali violazioni.
- "flags": [] di default; aggiungi oggetti {{type, message}} SOLO per dubbi reali (gender_ambiguous|term_ambiguous|ocr_suspect|source_ambiguous|other).
- Non aggiungere chiavi esterne, non commentare, non spiegare."""
    return prompt, seg_id


def build_messages(prompt):
    return [
        {"role": "system", "content": "Traducitore editoriale EN->IT. Rispondi in JSON."},
        {"role": "user", "content": prompt},
    ]


# --------------------------------------------------------------------------- #
# Conteggio token REALI
# --------------------------------------------------------------------------- #
def chars_per_token(text, cpt):
    return int(round(len(text) / cpt))


def word_based_tokens(text, factor=1.4):
    return int(round(len(re.findall(r"\S+", text)) * factor))


def tiktoken_count(text):
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return None


def count_prompt_tokens(prompt, seg_id):
    """Ritorna un dict con tutti i metodi + breakdown per sezione PRD §5.4.r5."""
    # Il prompt reale include system+user. Simuliamo il payload completo.
    sys_txt = "Traducitore editoriale EN->IT. Rispondi in JSON."
    full_payload = sys_txt + "\n" + prompt

    methods = {
        "char_4_0": chars_per_token(full_payload, 4.0),   # conservativa EN
        "char_3_2": chars_per_token(full_payload, 3.2),   # EN più densa
        "word_x1_4": word_based_tokens(full_payload, 1.4),
    }
    tt = tiktoken_count(full_payload)
    methods["tiktoken_cl100k"] = tt if tt is not None else "NA (tiktoken non installato)"

    # Breakdown per sezione (metodo char_3.2, più vicino a un tokenizer reale EN).
    seg_prompt, _ = build_prompt()
    sections = {
        "RUOLO+PRIORITÀ": seg_prompt.split("PROFILO GENERE")[0],
        "PROFILO GENERE": seg_prompt.split("PROFILO GENERE")[1].split("GLOSSARIO")[0],
        "GLOSSARIO+TM": seg_prompt.split("GLOSSARIO")[1].split("CONTESTO PRECEDENTE")[0],
        "CONTESTO PRECEDENTE": seg_prompt.split("CONTESTO PRECEDENTE")[1].split("TESTO DA TRADURRE")[0],
        "TESTO DA TRADURRE": seg_prompt.split("TESTO DA TRADURRE\n[", 1)[1] if "TESTO DA TRADURRE\n[" in seg_prompt else "",
    }
    breakdown = {k: chars_per_token(v, 3.2) for k, v in sections.items()}

    return {
        "methods": methods,
        "tiktoken_available": tt is not None,
        "breakdown_sections": breakdown,
        "budget_total": BUDGET_TOTAL,
        "budget_source_low": BUDGET_SORGENTE_BASSO,
        "budget_source_high": BUDGET_SORGENTE_ALTO,
        "budget_context": BUDCONTestO,
        "budget_output": BUDGET_OUTPUT,
    }


# --------------------------------------------------------------------------- #
# Validazione schema §9.5 + terminologia
# --------------------------------------------------------------------------- #
VALID_FLAG_TYPES = {
    "gender_ambiguous", "term_ambiguous", "ocr_suspect", "source_ambiguous", "other"
}


def validate_output(payload, seg_id):
    """Ritorna (ok_bool, [{campo, messaggio})."""
    errors = []
    if not isinstance(payload, dict):
        return False, [{"campo": "root", "messaggio": "JSON non è un oggetto."}]
    if "translations" not in payload or not isinstance(payload["translations"], list):
        errors.append({"campo": "translations", "messaggio": "manca o non è una lista."})
        return False, errors

    seen_ids = set()
    for i, item in enumerate(payload["translations"]):
        for f in ("segment_id", "target_text"):
            if f not in item or not isinstance(item[f], str) or not item[f].strip():
                errors.append({"campo": f"translations[{i}].{f}",
                               "messaggio": f"manca/non valida."})
        sid = item.get("segment_id")
        if sid:
            seen_ids.add(sid)
        # used_entity_ids
        uei = item.get("used_entity_ids", [])
        if not isinstance(uei, list) or not all(isinstance(x, str) for x in uei):
            errors.append({"campo": f"translations[{i}].used_entity_ids",
                           "messaggio": "non è lista di stringhe."})
        # term_violations
        tv = item.get("term_violations", [])
        if not isinstance(tv, list):
            errors.append({"campo": f"translations[{i}].term_violations",
                           "messaggio": "non è una lista."})
        # flags
        fl = item.get("flags", [])
        if not isinstance(fl, list):
            errors.append({"campo": f"translations[{i}].flags",
                           "messaggio": "non è una lista."})
        for j, frag in enumerate(fl):
            if not isinstance(frag, dict) or frag.get("type") not in VALID_FLAG_TYPES:
                errors.append({"campo": f"translations[{i}].flags[{j}].type",
                               "messaggio": f"value non valido: {frag.get('type')!r}"})

    # ID corrispondenti
    if seg_id not in seen_ids:
        errors.append({"campo": "ID_match",
                       "messaggio": f"segment_id '{seg_id <seg_id}' non presente."})

    return (len(errors) == 0), errors


def check_terminology(target_text):
    """Verifica che le voci 'non tradurre' (keep=True) compiano identiche."""
    violations = []
    for g in GLOSSARY:
        if not g["keep"]:
            continue
        # Ricerca case-insensitive della forma esatta
        if re.search(rf"\b{re.escape(g['it'])}\b", target_text, flags=re.IGNORECASE):
            continue
        violations.append({"term": g["it"], "in_target": False})
    return violations


# --------------------------------------------------------------------------- #
# Chiamada Gateway
# --------------------------------------------------------------------------- #
def gateway_chat(model, messages, temperature=0.2, top_p=1.0, seed=7, max_tokens=4096):
    body = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "reasoning_effort": "none",
        "seed": seed,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    data = json.dumps(body).encode()
    headers = {
        "Authorization": f"Bearer {PHX_TOKEN}",
        "Content-Type": "application/json",
    }
    t0 = time.time()
    req = urllib.request.Request(GATEWAY_BASE + "/chat/completions", data=data,
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.load(resp)
    elapsed = time.time() - t0
    return payload, elapsed


# --------------------------------------------------------------------------- #
# Loop prove
# --------------------------------------------------------------------------- #
def run_trials(model, n_trials):
    prompt, seg_id = build_prompt()
    results = []
    for t in range(1, n_trials + 1):
        messages = build_messages(prompt)
        payload, elapsed = gateway_chat(model, messages, seed=7 + t)
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        # Parsing JSON
        try:
            parsed = json.loads(content)
            json_ok = True
        except Exception as e:
            parsed = None
            json_ok = False
        schema_ok, schema_err = validate_output(parsed, seg_id) if json_ok else (False,
            [{"campo": "json", "messaggio": f"parse failed: {e}"}])
        # Terminologia (solo se schema ok)
        term_ok = True
        term_violations = []
        if schema_ok and isinstance(parsed, dict):
            for item in parsed.get("translations", []):
                term_violations += check_terminology(item.get("target_text", ""))
            term_ok = len(term_violations) == 0
        first_pass = json_ok and schema_ok and term_ok
        trans_list = parsed["translations"] if (isinstance(parsed, dict) and json_ok and isinstance(parsed.get("translations"), list)) else []
        results.append({
            "trial": t,
            "elapsed_ms": round(elapsed * 1000, 1),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "json_ok": json_ok,
            "schema_valid": schema_ok,
            "terminology_ok": term_ok,
            "first_pass": first_pass,
            "errors": schema_err if not schema_ok else (term_violations if not term_ok else []),
            "target_texts": [it.get("target_text", "") for it in trans_list],
        })
    return results, seg_id


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Prototipo prompt traduzione §9.4/§9.5")
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dump-prompt", action="store_true",
                    help="Mostra un prompt popolato + conteggio token e basta.")
    args = ap.parse_args()

    prompt, seg_id = build_prompt()
    toks = count_prompt_tokens(prompt, seg_id)

    if args.dump_prompt:
        print("=== PROMPT POPOLATO (§9.4 con dati fittizi) ===")
        print(prompt)
        print("\n\n=== CONTEGGIO TOKEN REALI (prompt popolato) ===")
        print(f"  budget totale PRD ......... {toks['budget_total']:,}")
        print(f"  budget sorgente ........... {toks['budget_source_low']:,}-{toks['budget_source_high']:,}")
        print(f"  budget contesto ........... {toks['budget_context']:,}")
        print(f"  budget output ............. {toks['budget_output']:,}")
        print("  metodi conteggio (totale prompt+system):")
        for k, v in toks["methods"].items():
            print(f"    {k:<18} {v}")
        print("  breakdown sezioni:")
        for k, v in toks["breakdown_sections"].items():
            print(f"    {k:<22} {v:,}")
        tot = toks["methods"].get("char_3_2")
        within = tot <= toks["budget_total"]
        print(f"\n  bolla: {tot:,}/{toks['budget_total']:,} token totali "
              f"-> {'IN BUDGET' if within else 'OUT OF BUDGET'}")
        return

    print(f"Modello: {args.model}")
    print(f"Segmento ID: {seg_id}")
    toks = count_prompt_tokens(prompt, seg_id)
    print("\n=== CONTEGGIO TOKEN REALI ===")
    for k, v in toks["methods"].items():
        print(f"  {k:<18} {v}")
    tot = toks["methods"]["char_3_2"]
    print(f"  TOTALE (char_3.2) ......... {tot:,}/{toks['budget_total']:,} "
          f"-> {'IN BUDGET' if tot <= toks['budget_total'] else 'OUT OF BUDGET'}")
    print(f"\nRipetizione {args.trials} prove...\n")

    results, seg_id = run_trials(args.model, args.trials)

    n_pass = sum(1 for r in results if r["first_pass"])
    n_schema = sum(1 for r in results if r["schema_valid"])
    n_json = sum(1 for r in results if r["json_ok"])
    n_term = sum(1 for r in results if r["terminology_ok"])
    avg_ms = sum(r["elapsed_ms"] for r in results) / len(results)
    avg_prompt = sum(r["prompt_tokens"] or 0 for r in results) / len(results)

    report = {
        "model": args.model,
        "seg_id": seg_id,
        "trials": args.trials,
        "results": results,
        "summary": {
            "first_pass_ok": n_pass,
            "first_pass_pct": round(100.0 * n_pass / args.trials, 1),
            "schema_valid": n_schema,
            "schema_pct": round(100.0 * n_schema / args.trials, 1),
            "json_ok": n_json,
            "terminology_ok": n_term,
            "avg_elapsed_ms": round(avg_ms, 1),
            "avg_prompt_tokens": round(avg_prompt, 1),
            "budget_total": toks["budget_total"],
            "token_estimate_char32": tot,
        },
        "prompt_tokenization": toks,
    }

    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"Report scritto in {args.out}")
    else:
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))

    print("\n=== Riepilogo ===")
    for r in results:
        status = "PASS" if r["first_pass"] else "FAIL"
        print(f"  trial {r['trial']:>2}: {status}  "
              f"{r['elapsed_ms']:>7.1f}ms  "
              f"ptok={str(r['prompt_tokens']):>6}  "
              f"ctok={str(r['completion_tokens']):>5}")


if __name__ == "__main__":
    main()
