import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { DoctorsScreen } from "@/features/admin/doctors-screen";
import { getCurrentUser, serverFetchOptional } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Department, Page, User } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("admin");
  return { title: t("doctorsTitle") };
}

export default async function DoctorsAdminPage() {
  const user = await getCurrentUser();
  if (!can(user, "doctor:read")) return <NoPermission />;

  // Both are optional: an administrator who can manage doctors but not read
  // staff accounts gets the list without the "add" picker rather than an
  // error page.
  const [staff, departments] = await Promise.all([
    serverFetchOptional<Page<User>>("/users?limit=200"),
    serverFetchOptional<Page<Department>>("/departments?limit=100"),
  ]);

  return (
    <DoctorsScreen
      staff={staff?.items ?? []}
      departments={departments?.items ?? []}
      canManage={can(user, "doctor:manage")}
    />
  );
}
