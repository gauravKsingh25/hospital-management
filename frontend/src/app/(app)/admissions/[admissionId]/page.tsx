import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { AdmissionScreen } from "@/features/wards/admission-screen";
import { ApiError } from "@/lib/api/error";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Admission } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("wards");
  return { title: t("admissionTitle") };
}

/**
 * One inpatient stay.
 *
 * `/ipd/admissions/{id}` returns the admission, the bed, the ward and the
 * patient in a single response — assembled server-side for the same reason as
 * the doctor's chart. A screen where somebody transfers a bed or signs a
 * discharge must never render with the patient's name still loading.
 */
export default async function AdmissionPage({
  params,
}: PageProps<"/admissions/[admissionId]">) {
  const { admissionId } = await params;
  const user = await getCurrentUser();

  if (!can(user, "admission:read")) return <NoPermission />;

  let admission: Admission;
  try {
    admission = await serverFetch<Admission>(`/ipd/admissions/${admissionId}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    if (error instanceof ApiError && error.isForbidden) return <NoPermission />;
    throw error;
  }

  return (
    <AdmissionScreen
      admissionId={admissionId}
      initial={admission}
      // Moving a patient between beds is a bed assignment, not an admission
      // edit — the backend gates transfer on `bed:assign`.
      canTransfer={can(user, "bed:assign")}
      canDischarge={can(user, "admission:discharge")}
    />
  );
}
