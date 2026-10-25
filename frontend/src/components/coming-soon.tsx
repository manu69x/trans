"use client";

/** Segnaposto per le pagine delle fasi successive (F2+). */

import { useI18n } from "@/lib/i18n";

export function ComingSoon({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  const { t } = useI18n();
  return (
    <div className="mx-auto max-w-2xl">
      <h1 className="text-2xl font-bold text-white">{title}</h1>
      <div className="card mt-4 text-sm text-slate-300">
        <p>{description}</p>
        <p className="mt-2 text-xs text-slate-500">{t("comingSoon.note")}</p>
      </div>
    </div>
  );
}
