import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { MedicationRound } from "@/features/chart/medication-round";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { can } from "@/lib/permissions";
import type { Dose, Page } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("chart");
  return { title: t("roundTitle") };
}

/**
 * The ward's medication round.
 *
 * Server-rendered so the first paint already holds the outstanding doses. A
 * nurse opens this standing in a corridor with a trolley; a blank frame
 * followed by a list is a second spent waiting, per patient, all shift.
 */
export default async function RoundsPage() {
  const t = await getTranslations("chart");
  const user = await getCurrentUser();

  if (!can(user, "medication:read")) return <NoPermission />;

  const doses = await serverFetch<Page<Dose>>(
    `/ipd/doses?status=DUE&limit=${BOARD_PAGE_SIZE}`,
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("roundTitle")}</h1>
        <p className="text-muted-foreground text-sm">{t("roundSubtitle")}</p>
      </div>

      <MedicationRound initial={doses} canAdminister={can(user, "medication:administer")} />
    </div>
  );
}
