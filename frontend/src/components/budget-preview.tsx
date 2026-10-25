/**
 * Preview del token budget (§8.2 / §5.4 / §15.3).
 *
 * Mostra, per un capitolo, il piano di blocchi del planner con lo stato di
 * budget per blocco e il budget aggregato del capitolo, calcolati rispetto al
 * modello di traduzione selezionato e al `max_output` configurato.
 *
 * Un blocco è ABBILITATO solo se `input + output ≤ context_window`;
 * l'intero capitolo è avviabile solo se ogni blocco lo è e la somma
 * rientra nella finestra. Il risultato alimenta il gate che impedisce
 * l'avvio del batch (§15.3).
 */

import { useMemo } from "react";

import {
  checkChapterBudget,
  formatTokens,
  type BudgetCheck,
} from "@/lib/model-budget";
import type { PlanBlock, GatewayModel, PlanResponse } from "@/lib/api-types";
import { useI18n } from "@/lib/i18n";

function StatusRow({
  label,
  status,
  ctx,
}: {
  label: string;
  status: BudgetCheck["status"];
  ctx: BudgetCheck;
}) {
  const { t } = useI18n();
  const ok = status === "ok";
  const unknown = status === "unknown";
  const color = ok
    ? "text-emerald-300"
    : unknown
      ? "text-amber-300"
      : "text-red-300";
  const text = ok
    ? "OK"
    : unknown
      ? t("budget.unknown")
      : t("budget.insufficient");
  return (
    <div className="flex items-center justify-between gap-2 py-1.5 text-sm">
      <span className="text-slate-300">{label}</span>
      <span className={`font-medium ${color}`}>{text}</span>
      <span className="text-xs text-slate-400">
        {formatTokens(ctx.needed)} / {formatTokens(ctx.contextWindow) ?? "—" }
        {"  · "}
        {t("budget.headroom")}{" "}
        {formatTokens(Math.max(0, ctx.headroom))}
      </span>
    </div>
  );
}

function BlockRow({ check, index }: { check: BudgetCheck; index: number }) {
  const { t } = useI18n();
  const ok = check.status === "ok";
  const unknown = check.status === "unknown";
  return (
    <div className="flex items-center justify-between gap-2 border-t border-slate-800/60 py-1.5 text-sm">
      <span className="text-slate-300">{t("budget.blockN", { n: index + 1 })}</span>
      <span
        className={`text-xs font-medium ${
          ok
            ? "text-emerald-300"
            : unknown
              ? "text-amber-300"
              : "text-red-300"
        }`}
      >
        {ok
          ? t("budget.blockReady")
          : unknown
            ? t("budget.blockPending")
            : t("budget.blocked")}
      </span>
      <span className="text-xs text-slate-400">
        {formatTokens(check.inputTokens)} / {formatTokens(check.contextWindow) ?? "—" }
      </span>
    </div>
  );
}

export function BudgetPreview({
  plan,
  model,
  maxOutput,
}: {
  plan: PlanResponse | null;
  model: GatewayModel | null;
  maxOutput: number | null | undefined;
}) {
  const { t } = useI18n();
  const chapter = useMemo(() => {
    if (!plan || !model) return null;
    return checkChapterBudget(plan.blocks, model, maxOutput);
  }, [plan, model, maxOutput]);

  if (!plan) {
    return <p className="text-sm text-slate-400">{t("budget.noPlan")}</p>;
  }

  if (!model) {
    return (
      <div className="rounded-md border border-amber-800 bg-amber-950/40 px-4 py-3 text-sm text-amber-200">
        {t("budget.pickModel")}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="card">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">
            {t("budget.blocks")}
          </p>
          <p className="mt-1 text-lg font-semibold text-white">{plan.verification.blocks}</p>
        </div>
        <div className="card">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">
            {t("budget.limitPerBlock")}
          </p>
          <p className="mt-1 text-lg font-semibold text-white">
            {formatTokens(plan.verification.limit)}
          </p>
        </div>
        <div className="card">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">
            {t("budget.window")}
          </p>
          <p className="mt-1 text-lg font-semibold text-white">
            {formatTokens(model.context_window) ?? "—"}
          </p>
        </div>
        <div className="card">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">
            {t("budget.outputReserve")}
          </p>
          <p className="mt-1 text-lg font-semibold text-white">
            {formatTokens(
              typeof maxOutput === "number" ? maxOutput : 2048
            )}
          </p>
        </div>
      </div>

      <section aria-labelledby="budget-capitolo">
        <h3 id="budget-capitolo" className="mb-1 text-sm font-semibold text-white">
          {t("budget.chapterBudget")}
        </h3>
        <div className="space-y-1">
          <StatusRow
            label={t("budget.wholeChapter")}
            status={chapter?.status ?? "unknown"}
            ctx={{
              status: chapter?.status ?? "unknown",
              contextWindow: chapter?.contextWindow ?? null,
              inputTokens: chapter?.inputTokens ?? 0,
              outputReserve: chapter?.outputReserve ?? 2048,
              needed: chapter?.needed ?? 0,
              headroom: chapter?.headroom ?? 0,
            }}
          />
        </div>
      </section>

      <section aria-labelledby="blocchi">
        <h3 id="blocchi" className="mb-1 text-sm font-semibold text-white">
          {t("budget.perBlock")}
        </h3>
        <div className="card">
          {chapter?.perBlock.map((c, i) => (
            <BlockRow key={i} check={c} index={i} />
          ))}
        </div>
      </section>

      {plan.verification.oversized_blocks.length > 0 && (
        <div className="rounded-md border border-red-800 bg-red-950/40 px-4 py-2 text-sm text-red-200">
          {t("budget.oversized", {
            limit: formatTokens(plan.verification.limit) ?? "",
            list: plan.verification.oversized_blocks.join(", "),
          })}
        </div>
      )}
    </div>
  );
}

export type { PlanBlock };
