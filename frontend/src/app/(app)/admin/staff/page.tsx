import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { StaffScreen } from "@/features/admin/staff-screen";
import { getCurrentUser, serverFetchOptional } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Page, Role } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("admin");
  return { title: t("staffTitle") };
}

/**
 * Staff accounts.
 *
 * The role list is fetched server-side and passed down: it is small, it
 * changes almost never, and every dialog on this screen needs it. Fetching it
 * per dialog would be three requests to render one checkbox list.
 */
export default async function StaffPage() {
  const user = await getCurrentUser();
  if (!can(user, "user:read")) return <NoPermission />;

  // `serverFetchOptional`: an administrator who can read users but not roles
  // still gets a working list, just without the role editor.
  const roles = await serverFetchOptional<Page<Role>>("/roles?limit=100");

  return (
    <StaffScreen
      roles={roles?.items ?? []}
      canCreate={can(user, "user:create")}
      canAssignRoles={can(user, "role:assign")}
      canDeactivate={can(user, "user:deactivate")}
    />
  );
}
