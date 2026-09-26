import { getTranslations } from "next-intl/server";
import { ShieldBan } from "lucide-react";

import { NoPermission } from "@/components/no-permission";
import { LinkButton } from "@/components/ui/link-button";
import { Outbox } from "@/features/messages/outbox";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { can } from "@/lib/permissions";
import type { MessageSummary, Page } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("messages");
  return { title: t("title") };
}

/**
 * The outbox.
 *
 * The `notifications` module has had a complete API since build-order step 8
 * and no screen at all, which meant the sentence its own RBAC comment names —
 * "the front desk is where 'I never got the message' is said out loud" — had
 * nowhere to be answered. Every message the system sends existed only in the
 * database.
 *
 * Server-rendered so the first page is already there: this is opened with a
 * patient waiting at the counter.
 */
export default async function MessagesPage() {
  const t = await getTranslations("messages");
  const user = await getCurrentUser();

  if (!can(user, "notification:read")) return <NoPermission />;

  const outbox = await serverFetch<Page<MessageSummary>>(
    `/notifications?limit=${BOARD_PAGE_SIZE}`,
  );

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-2xl">
          <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
          <p className="text-muted-foreground text-sm">{t("subtitle")}</p>
        </div>

        {can(user, "suppression:read") ? (
          <LinkButton variant="outline" className="h-tap" href="/messages/blocked">
            <ShieldBan aria-hidden className="size-4" />
            {t("blockedTitle")}
          </LinkButton>
        ) : null}
      </header>

      <Outbox
        initial={outbox}
        canSend={can(user, "notification:send")}
        canCancel={can(user, "notification:cancel")}
      />
    </div>
  );
}
