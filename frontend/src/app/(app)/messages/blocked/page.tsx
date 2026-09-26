import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { SuppressionList } from "@/features/messages/suppression-list";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { can } from "@/lib/permissions";
import type { Page, Suppression } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("messages");
  return { title: t("blockedTitle") };
}

/**
 * Who is blocked from being messaged.
 *
 * The screen that makes CLAUDE.md §14's hardest invariant inspectable. The
 * rule — never message a deceased patient — is enforced in three places in the
 * backend and was, until this page, invisible to everybody in the hospital.
 * An invariant nobody can see is one people stop trusting, and then work
 * around.
 */
export default async function BlockedPage() {
  const t = await getTranslations("messages");
  const user = await getCurrentUser();

  if (!can(user, "suppression:read")) return <NoPermission />;

  const suppressions = await serverFetch<Page<Suppression>>(
    `/notifications/suppressions?limit=${BOARD_PAGE_SIZE}`,
  );

  return (
    <div className="space-y-6">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{t("blockedTitle")}</h1>
        <p className="text-muted-foreground text-sm">{t("blockedSubtitle")}</p>
      </div>

      <SuppressionList initial={suppressions} canManage={can(user, "suppression:manage")} />
    </div>
  );
}
