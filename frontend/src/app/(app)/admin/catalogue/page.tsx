import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { CatalogueScreen } from "@/features/admin/catalogue-screen";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("admin");
  return { title: t("catalogueTitle") };
}

export default async function CatalogueAdminPage() {
  const user = await getCurrentUser();
  if (!can(user, "catalogue:read")) return <NoPermission />;

  return <CatalogueScreen canManage={can(user, "catalogue:manage")} />;
}
