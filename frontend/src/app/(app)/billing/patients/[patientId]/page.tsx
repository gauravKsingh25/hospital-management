import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { PatientHistory } from "@/features/billing/accounts/patient-history";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { BillHistory } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("history");
  return { title: t("title") };
}

/** A patient's bill history and cost journey — the accountant's view. */
export default async function PatientBillsPage({
  params,
}: {
  params: Promise<{ patientId: string }>;
}) {
  const user = await getCurrentUser();
  if (!can(user, "accounts:read")) return <NoPermission />;

  const { patientId } = await params;
  const history = await serverFetch<BillHistory>(`/billing/patients/${patientId}/history`);
  return (
    <PatientHistory
      initial={history}
      canEdit={can(user, "ipd_bill:edit")}
      canFinalise={can(user, "ipd_bill:finalise")}
    />
  );
}
