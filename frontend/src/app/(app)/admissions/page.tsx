import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { AdmissionDesk } from "@/features/admissions/admission-desk";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("admissions");
  return { title: t("title") };
}

/**
 * The admission desk — patients the OPD has sent for admission.
 *
 * Gated on `admission:desk`, not `admission:create`: reception and nurses can
 * already admit from a visit, but working this list is the admission desk's
 * job (and an administrator's). A hospital where one person does both gives
 * them both roles.
 */
export default async function AdmissionDeskPage() {
  const t = await getTranslations("admissions");
  const user = await getCurrentUser();

  if (!can(user, "admission:desk")) return <NoPermission />;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-muted-foreground text-sm">{t("subtitle")}</p>
      </div>
      <AdmissionDesk />
    </div>
  );
}
