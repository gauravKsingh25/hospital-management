import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { WardsScreen } from "@/features/admin/wards-screen";
import { getCurrentUser, serverFetchOptional } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Department, Page } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("admin");
  return { title: t("wardsTitle") };
}

export default async function WardsAdminPage() {
  const user = await getCurrentUser();
  if (!can(user, "ward:read")) return <NoPermission />;

  const departments = await serverFetchOptional<Page<Department>>("/departments?limit=100");

  // Beds are created under `ward:manage` too — the backend gates
  // `POST /beds` on it, so the screen must not offer what it would refuse.
  return (
    <WardsScreen
      departments={departments?.items ?? []}
      canManage={can(user, "ward:manage")}
    />
  );
}
