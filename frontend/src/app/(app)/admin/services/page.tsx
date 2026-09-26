import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { ServicesScreen } from "@/features/admin/services-screen";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("admin");
  return { title: t("servicesTitle") };
}

export default async function ServicesAdminPage() {
  const user = await getCurrentUser();
  if (!can(user, "service:read")) return <NoPermission />;

  // One flag for both halves: setting a price is `service:manage`, and so is
  // creating the rate card it belongs to.
  return <ServicesScreen canManage={can(user, "service:manage")} />;
}
