import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { SafetyBanner } from "@/components/patient/safety-banner";
import { ReportScreen } from "@/features/lab/report-screen";
import { ApiError } from "@/lib/api/error";
import { getCurrentUser, serverFetch, serverFetchOptional } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import { safetyBannerFor } from "@/lib/safety-banner";
import type { Analyte, DiagnosticReport, Patient } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("lab");
  return { title: t("reportTitle") };
}

/**
 * One report: enter the values, then sign it.
 *
 * The report and its analytes are fetched together, server-side. The analytes
 * are what turn an empty form into a named list of measurements with their
 * units — without them the technician would be typing into unlabelled boxes,
 * so they are not something to load afterwards.
 */
export default async function ReportPage({ params }: PageProps<"/lab/reports/[reportId]">) {
  const { reportId } = await params;
  const user = await getCurrentUser();

  if (!can(user, "report:read")) return <NoPermission />;

  let report: DiagnosticReport;
  try {
    report = await serverFetch<DiagnosticReport>(`/diagnostics/reports/${reportId}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    if (error instanceof ApiError && error.isForbidden) return <NoPermission />;
    throw error;
  }

  // A report accessioned against a catalogue entry has analytes; a radiology
  // study has none and is written as prose instead. `serverFetchOptional` so a
  // technician without `catalogue:read` still gets a working screen.
  let analytes: Analyte[] = [];
  if (report.catalogue_item_id) {
    analytes =
      (await serverFetchOptional<Analyte[]>(
        `/diagnostics/catalogue/${report.catalogue_item_id}/analytes`,
      )) ?? [];
  }

  // UX-04: allergies and infection precautions matter to whoever handles the
  // sample, not only to whoever treats the patient. Optional, so a bench role
  // without `patient:read` still gets a working screen.
  const patient = await serverFetchOptional<Patient>(`/patients/${report.patient_id}`);

  return (
    <div className="space-y-4">
      {patient ? <SafetyBanner banner={safetyBannerFor(patient)} /> : null}
      <ReportScreen
        initialReport={report}
        analytes={analytes}
        canEnter={can(user, "result:enter")}
        canVerify={can(user, "result:verify")}
      />
    </div>
  );
}
