import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { ChartScreen } from "@/features/chart/chart-screen";
import { ApiError } from "@/lib/api/error";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Admission, Dose, MedicationOrder, Page } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("chart");
  return { title: t("title") };
}

/**
 * One patient's medication chart.
 *
 * Three fetches, in parallel, server-side: the admission (which carries the
 * patient and the bed), the prescribed drugs, and the dose slots. They are
 * separate endpoints because they are separate concerns on the backend, but
 * they arrive together — a chart that renders the drugs before it knows whose
 * chart it is would be the wrong screen to leave half-drawn.
 */
export default async function ChartPage({
  params,
}: PageProps<"/admissions/[admissionId]/chart">) {
  const { admissionId } = await params;
  const user = await getCurrentUser();

  if (!can(user, "medication:read")) return <NoPermission />;

  let admission: Admission;
  let orders: Page<MedicationOrder>;
  let doses: Page<Dose>;
  try {
    [admission, orders, doses] = await Promise.all([
      serverFetch<Admission>(`/ipd/admissions/${admissionId}`),
      serverFetch<Page<MedicationOrder>>(
        `/ipd/admissions/${admissionId}/medications?limit=100`,
      ),
      serverFetch<Page<Dose>>(`/ipd/doses?admission_id=${admissionId}&limit=100`),
    ]);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    if (error instanceof ApiError && error.isForbidden) return <NoPermission />;
    throw error;
  }

  return (
    <ChartScreen
      admission={admission}
      initialOrders={orders}
      initialDoses={doses}
      // The separation that is the point of a medication chart: a doctor
      // prescribes and does not sign at the bedside, a nurse gives the drug
      // and does not prescribe it.
      canPrescribe={can(user, "medication:prescribe")}
      canAdminister={can(user, "medication:administer")}
    />
  );
}
