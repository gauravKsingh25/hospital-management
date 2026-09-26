import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { SummaryScreen } from "@/features/chart/summary-screen";
import { ApiError } from "@/lib/api/error";
import { getCurrentUser, serverFetch, serverFetchOptional } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Admission, DischargeSummary } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("summary");
  return { title: t("title") };
}

/**
 * The discharge summary for one stay.
 *
 * The summary is fetched with `serverFetchOptional` because "there isn't one
 * yet" is the ordinary state, not an error — it is compiled when a doctor
 * marks the patient fit to go, and before that the screen's job is to offer
 * the compile button rather than a 404.
 */
export default async function SummaryPage({
  params,
}: PageProps<"/admissions/[admissionId]/summary">) {
  const { admissionId } = await params;
  const user = await getCurrentUser();

  if (!can(user, "summary:read")) return <NoPermission />;

  let admission: Admission;
  try {
    admission = await serverFetch<Admission>(`/ipd/admissions/${admissionId}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    if (error instanceof ApiError && error.isForbidden) return <NoPermission />;
    throw error;
  }

  const summary = await serverFetchOptional<DischargeSummary>(
    `/ipd/summaries/by-admission/${admissionId}`,
  );

  return (
    <SummaryScreen
      admission={admission}
      initial={summary}
      canWrite={can(user, "summary:write")}
      // Signing is checked twice on the backend — the permission *and* the
      // role, because the signature carries a registration number and an
      // administrator holding the permission is not a clinician.
      canSign={can(user, "summary:sign")}
    />
  );
}
