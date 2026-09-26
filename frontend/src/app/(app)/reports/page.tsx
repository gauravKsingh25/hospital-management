import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { ReportsDashboard } from "@/features/reports/dashboard";
// From `windows.ts`, not from the client component that also uses it — a
// constant re-exported through a `"use client"` module arrives here as a
// client-reference stub, not a number.
import { DEFAULT_TRAILING_DAYS } from "@/features/reports/windows";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Dashboard } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("reports");
  return { title: t("title") };
}

/**
 * The KPI dashboard — CLAUDE.md §13 step 10, and the last module to get a screen.
 *
 * Gated on `report:operational` alone, which is the same permission the
 * endpoint requires. The finer permissions (`report:clinical`,
 * `report:revenue`) are not checked here at all: the server decides which
 * sections come back, and the client renders whatever arrived. Checking twice
 * would be two places to get it wrong, and only one of them is authoritative.
 *
 * Server-rendered so the first paint holds real numbers. This is a screen
 * somebody opens, glances at, and closes — a spinner would be most of the
 * time they spend on it.
 */
export default async function ReportsPage() {
  const t = await getTranslations("reports");
  const user = await getCurrentUser();

  if (!can(user, "report:operational")) return <NoPermission />;

  const dashboard = await serverFetch<Dashboard>(
    `/reports/dashboard?trailing_days=${DEFAULT_TRAILING_DAYS}`,
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-muted-foreground text-sm">{t("subtitle")}</p>
      </div>

      <ReportsDashboard initial={dashboard} />
    </div>
  );
}
