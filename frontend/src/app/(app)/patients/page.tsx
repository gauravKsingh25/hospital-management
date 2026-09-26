import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { PatientIndex } from "@/features/patients/patient-index";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("patients");
  return { title: t("title") };
}

export default async function PatientsPage() {
  const user = await getCurrentUser();
  if (!can(user, "patient:read")) return <NoPermission />;

  return <PatientIndex />;
}
