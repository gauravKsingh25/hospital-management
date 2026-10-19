import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { PaymentsScreen } from "@/features/admin/payments-screen";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Hospital } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("admin");
  return { title: t("paymentsTitle") };
}

export default async function PaymentsAdminPage() {
  const user = await getCurrentUser();
  // A platform admin has no hospital of their own; this screen is per tenant.
  if (!can(user, "hospital:update") || !user.hospital_id) return <NoPermission />;

  const hospital = await serverFetch<Hospital>(`/hospitals/${user.hospital_id}`);
  return <PaymentsScreen hospitalId={hospital.id} initialUpiId={hospital.upi_vpa ?? ""} />;
}
