"use client";

import Link from "next/link";

import { useI18n } from "@/lib/i18n";

export default function NotFound() {
  const { t } = useI18n();
  return (
    <div className="mx-auto max-w-xl text-center">
      <h1 className="text-2xl font-bold text-white">{t("notFound.title")}</h1>
      <p className="mt-2 text-sm text-slate-400">{t("notFound.body")}</p>
      <Link href="/" className="btn-primary mt-4 inline-flex">
        {t("notFound.back")}
      </Link>
    </div>
  );
}
