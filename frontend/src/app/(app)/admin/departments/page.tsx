import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { DepartmentsScreen } from "@/features/admin/departments-screen";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("admin");
  return { title: t("departmentsTitle") };
}

export default async function DepartmentsAdminPage() {
  const user = await getCurrentUser();
  if (!can(user, "department:read")) return <NoPermission />;

  return <DepartmentsScreen canManage={can(user, "department:manage")} />;
}
