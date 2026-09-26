import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { LabWorklist } from "@/features/lab/worklist";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { can } from "@/lib/permissions";
import type { Page, WorklistEntry } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("lab");
  return { title: t("title") };
}

/**
 * The diagnostics bench.
 *
 * Fetched server-side so the first paint already holds the list. The screen is
 * opened at the start of a shift and left open, so the initial render is the
 * one that has to be fast; the client takes over polling from there.
 */
export default async function LabPage() {
  const t = await getTranslations("lab");
  const user = await getCurrentUser();

  if (!can(user, "report:read")) return <NoPermission />;

  // The same page size the client uses. They must match, or the rows past the
  // client's limit disappear from the first paint the moment it refetches.
  const worklist = await serverFetch<Page<WorklistEntry>>(
    `/diagnostics/worklist?limit=${BOARD_PAGE_SIZE}`,
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-muted-foreground text-sm">{t("subtitle")}</p>
      </div>

      <LabWorklist
        initial={worklist}
        canAccession={can(user, "result:enter")}
        canCollect={can(user, "specimen:collect")}
        canReceive={can(user, "specimen:receive")}
      />
    </div>
  );
}
