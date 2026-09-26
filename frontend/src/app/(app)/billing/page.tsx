import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { AccountsBoard } from "@/features/billing/accounts-board";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { can } from "@/lib/permissions";
import type { AccountBoardEntry, Page } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("billing");
  return { title: t("boardTitle") };
}

/** The cash counter's board — every visit that still owes something. */
export default async function BillingPage() {
  const t = await getTranslations("billing");
  const user = await getCurrentUser();

  if (!can(user, "charge:read")) return <NoPermission />;

  // Matches the client's page size — see the lab worklist for why.
  const board = await serverFetch<Page<AccountBoardEntry>>(
    `/billing/accounts?limit=${BOARD_PAGE_SIZE}`,
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("boardTitle")}</h1>
        <p className="text-muted-foreground text-sm">{t("boardSubtitle")}</p>
      </div>

      <AccountsBoard initial={board} />
    </div>
  );
}
