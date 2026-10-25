"use client";

import { ComingSoon } from "@/components/coming-soon";
import { useI18n } from "@/lib/i18n";

export default function AuditPage() {
  const { t } = useI18n();
  return (
    <ComingSoon
      title={t("audit.title")}
      description={t("audit.description")}
    />
  );
}
