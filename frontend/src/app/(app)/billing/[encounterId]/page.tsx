import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { VisitAccountScreen } from "@/features/billing/visit-account";
import { ApiError } from "@/lib/api/error";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { VisitAccount } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("billing");
  return { title: t("accountTitle") };
}

/**
 * One visit's account.
 *
 * `/billing/accounts/{encounter}` returns the patient, the visit, the pending
 * charges and every invoice in a single response — assembled server-side for
 * the same reason as the doctor's chart. A counter screen that opens in four
 * requests is a counter screen with a queue behind it.
 */
export default async function VisitAccountPage({
  params,
}: PageProps<"/billing/[encounterId]">) {
  const { encounterId } = await params;
  const user = await getCurrentUser();

  if (!can(user, "charge:read")) return <NoPermission />;

  let account: VisitAccount;
  try {
    account = await serverFetch<VisitAccount>(`/billing/accounts/${encounterId}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    if (error instanceof ApiError && error.isForbidden) return <NoPermission />;
    throw error;
  }

  return (
    <VisitAccountScreen
      encounterId={encounterId}
      initial={account}
      canCreateInvoice={can(user, "invoice:create")}
      canIssueInvoice={can(user, "invoice:issue")}
      canRecordPayment={can(user, "payment:record")}
    />
  );
}
