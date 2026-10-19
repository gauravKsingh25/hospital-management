import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { MedicinesScreen } from "@/features/medicines/medicines-screen";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("medicines");
  return { title: t("title") };
}

/**
 * The medicine list — presets with a dose and a price, used across the
 * inpatient flow.
 *
 * Readable by anyone with `medicine:read` (doctors pick from it, nurses and
 * cashiers look prices up); editable with `medicine:manage`, which the
 * admission desk, pharmacy and administrators hold.
 */
export default async function MedicinesPage() {
  const t = await getTranslations("medicines");
  const user = await getCurrentUser();

  if (!can(user, "medicine:read")) return <NoPermission />;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-muted-foreground text-sm">{t("subtitle")}</p>
      </div>
      <MedicinesScreen canManage={can(user, "medicine:manage")} />
    </div>
  );
}
