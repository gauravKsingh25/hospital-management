import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { TemplatesScreen } from "@/features/admin/templates-screen";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("admin");
  return { title: t("templatesTitle") };
}

export default async function TemplatesPage() {
  const user = await getCurrentUser();

  if (!can(user, "notification_template:read")) return <NoPermission />;

  return <TemplatesScreen canManage={can(user, "notification_template:manage")} />;
}
