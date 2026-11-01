import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { EmergencyForm } from "@/features/emergency/emergency-form";
import { getCurrentUser } from "@/lib/api/server";
import { can, canAll } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("emergency");
  return { title: t("title") };
}

/** The casualty door (UX-01). Reached by the red button on Reception, or F9. */
export default async function EmergencyPage() {
  const t = await getTranslations("emergency");
  const user = await getCurrentUser();

  if (!canAll(user, "patient:create", "appointment:create")) return <NoPermission />;

  return (
    <div className="space-y-6">
      <header className="mx-auto max-w-2xl no-print">
        <h1 className="text-destructive text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-muted-foreground mt-1 text-sm">{t("subtitle")}</p>
      </header>

      <EmergencyForm canRequestAdmission={can(user, "admission:request")} />
    </div>
  );
}
