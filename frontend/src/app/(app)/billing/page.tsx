import Link from "next/link";
import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { AccountsBoard } from "@/features/billing/accounts-board";
import { AccountsDashboard } from "@/features/billing/accounts/accounts-dashboard";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { can } from "@/lib/permissions";
import { cn } from "@/lib/utils";
import type { AccountBoardEntry, Hospital, Page } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("billing");
  return { title: t("boardTitle") };
}

/**
 * Billing: two screens for two jobs.
 *
 * **Accounts** is the accountant's (and the admin's) home — totals, every
 * bill and receipt, newest first. **Cash counter** is the cashier's board of
 * visits that owe money, oldest debt first, unchanged. Someone with only the
 * counter's powers sees only the counter, with no tab to wonder about.
 */
export default async function BillingPage({
  searchParams,
}: {
  searchParams: Promise<{ view?: string }>;
}) {
  const t = await getTranslations("billing");
  const accounts = await getTranslations("accounts");
  const user = await getCurrentUser();

  const canAccounts = can(user, "accounts:read");
  const canCounter = can(user, "charge:read");
  if (!canAccounts && !canCounter) return <NoPermission />;

  const { view } = await searchParams;
  const showCounter = !canAccounts || (view === "counter" && canCounter);

  if (showCounter) {
    // Matches the client's page size — see the lab worklist for why.
    const board = await serverFetch<Page<AccountBoardEntry>>(
      `/billing/accounts?limit=${BOARD_PAGE_SIZE}`,
    );
    return (
      <div className="space-y-6">
        {canAccounts ? <BillingTabs current="counter" /> : null}
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("boardTitle")}</h1>
          <p className="text-muted-foreground text-sm">{t("boardSubtitle")}</p>
        </div>
        <AccountsBoard initial={board} />
      </div>
    );
  }

  // A bill printed without a logo or address is not a proper document; say
  // so where the bills are, to whoever can fix it or ask for it.
  const hospital = user.hospital_id
    ? await serverFetch<Hospital>(`/hospitals/${user.hospital_id}`)
    : null;
  const incomplete = hospital !== null && (!hospital.logo_version || !hospital.address_line1);

  return (
    <div className="space-y-6">
      {canCounter ? <BillingTabs current="accounts" /> : null}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{accounts("title")}</h1>
        <p className="text-muted-foreground text-sm">{accounts("subtitle")}</p>
      </div>
      {incomplete ? (
        <div
          className="bg-caution/10 border-caution/40 flex flex-wrap items-center justify-between gap-3 rounded-lg border px-4 py-3 text-sm"
          data-testid="profile-banner"
          role="status"
        >
          <span>{accounts("profileIncomplete")}</span>
          {can(user, "hospital:update") ? (
            <Link href="/admin/hospital" className="font-medium underline underline-offset-4">
              {accounts("completeProfile")}
            </Link>
          ) : null}
        </div>
      ) : null}
      <AccountsDashboard />
    </div>
  );
}

async function BillingTabs({ current }: { current: "accounts" | "counter" }) {
  const t = await getTranslations("accounts");
  const tabs = [
    { key: "accounts", href: "/billing", label: t("tabAccounts") },
    { key: "counter", href: "/billing?view=counter", label: t("tabCounter") },
  ] as const;
  return (
    <nav aria-label={t("billingViews")}>
      <div role="tablist" className="bg-muted inline-flex rounded-lg p-1">
        {tabs.map((tab) => (
          <Link
            key={tab.key}
            href={tab.href}
            role="tab"
            aria-selected={current === tab.key}
            className={cn(
              "h-tap inline-flex items-center rounded-md px-4 text-sm font-medium",
              current === tab.key
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}
