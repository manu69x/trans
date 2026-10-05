/**
 * Logica di calcolo del budget di token (§8.2 / §5.4 / §15.3).
 *
 * Un blocco LLM può essere avviato SOLO se il modello ha abbastanza finestra
 * di contesto per contenere l'input (sorgente + contesto/glossario/TM) E la
 * riserva di output. La finestra di contesto è `model.context_window`;
 * l'input stimato è `token_budget.total` (già comprensivo delle riserve
 * default del planner); l'output è la riserva configurata dall'utente
 * (`max_output`, default 2048).
 *
 * Il risultato è una valutazione per ogni blocco:
 *   - `ok`            -> input+output rientra nella finestra, blocco avviabile.
 *   - `insufficient`  -> input+output supera la finestra, BLOCCO NON ABBILITATO.
 *   - `unknown`       -> la finestra di contesto è sconosciuta: non si può
 *                        verificare, non si blocca ma si segnala.
 */

import type { GatewayModel, PlanBlock } from "./api-types";

export type BudgetStatus = "ok" | "insufficient" | "unknown";

export interface BudgetCheck {
  status: BudgetStatus;
  contextWindow: number | null;
  inputTokens: number;
  outputReserve: number;
  needed: number;
  headroom: number;
}

/** Riserva di output di default (§5.4.5: 2.000-3.000 token riservati). */
const DEFAULT_OUTPUT_RESERVE = 2048;

/**
 * Calcola lo stato di budget per un singolo blocco.
 *
 * @param block  blocco dal /plan (con `token_budget` popolato).
 * @param model  modello di traduzione selezionato.
 * @param maxOutput riserva output configurata dall'utente (in token).
 */
export function checkBlockBudget(
  block: PlanBlock,
  model: GatewayModel,
  maxOutput: number | null | undefined
): BudgetCheck {
  const contextWindow = model.context_window ?? null;
  const inputTokens = block.token_budget?.total ?? 0;
  const outputReserve =
    typeof maxOutput === "number" && maxOutput > 0
      ? Math.round(maxOutput)
      : DEFAULT_OUTPUT_RESERVE;
  const needed = inputTokens + outputReserve;

  if (contextWindow == null) {
    return {
      status: "unknown",
      contextWindow: null,
      inputTokens,
      outputReserve,
      needed,
      headroom: 0,
    };
  }

  const headroom = contextWindow - needed;
  const status = headroom >= 0 ? "ok" : "insufficient";
  return { status, contextWindow, inputTokens, outputReserve, needed, headroom };
}

/**
 * Valutazione aggregata di un capitolo: somma l'input di tutti i blocchi e
 * verifica che l'intero capitolo (input + output) entri nella finestra.
 * Questo è il gate che impedisce l'avvio del batch (§15.3).
 */
export function checkChapterBudget(
  blocks: PlanBlock[],
  model: GatewayModel,
  maxOutput: number | null | undefined
): {
  status: BudgetStatus;
  contextWindow: number | null;
  inputTokens: number;
  outputReserve: number;
  needed: number;
  headroom: number;
  perBlock: BudgetCheck[];
} {
  const contextWindow = model.context_window ?? null;
  const outputReserve =
    typeof maxOutput === "number" && maxOutput > 0
      ? Math.round(maxOutput)
      : DEFAULT_OUTPUT_RESERVE;

  const perBlock = blocks.map((b) =>
    checkBlockBudget(b, model, maxOutput)
  );

  const inputTokens = perBlock.reduce((s, c) => s + c.inputTokens, 0);
  const needed = inputTokens + outputReserve;
  const headroom = contextWindow == null ? 0 : contextWindow - needed;

  let status: BudgetStatus;
  if (contextWindow == null) {
    status = "unknown";
  } else if (perBlock.every((c) => c.status !== "insufficient") && headroom >= 0) {
    status = "ok";
  } else {
    status = "insufficient";
  }

  return { status, contextWindow, inputTokens, outputReserve, needed, headroom, perBlock };
}

/** Formatta un conteggio di token in modo leggibile. */
export function formatTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString("it-IT");
}
