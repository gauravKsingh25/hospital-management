import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { LinkButton } from "@/components/ui/link-button";
import { PrintButton } from "@/features/billing/print/print-button";
import { PrintedSummary } from "@/features/chart/printed-summary";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Admission, DischargeSummary, Hospital } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("summary");
  return { title: t("title") };
}

/** A stay's discharge summary, printable. `?print=1` opens the print dialog on load. */
export default async function PrintedSummaryPage({
  params,
  searchParams,
}: {
  params: Promise<{ admissionId: string }>;
  searchParams: Promise<{ print?: string }>;
}) {
  const t = await getTranslations("summary");
  const bill = await getTranslations("bill");
  const user = await getCurrentUser();
  if (!can(user, "summary:read") || !user.hospital_id) return <NoPermission />;

  const [{ admissionId }, { print }] = await Promise.all([params, searchParams]);
  const [admission, summary, hospital] = await Promise.all([
    serverFetch<Admission>(`/ipd/admissions/${admissionId}`),
    serverFetch<DischargeSummary>(`/ipd/summaries/by-admission/${admissionId}`),
    serverFetch<Hospital>(`/hospitals/${user.hospital_id}`),
  ]);

  return (
    <>
      <div className="no-print mx-auto flex max-w-[210mm] items-center justify-between gap-3 px-6 pt-4">
        <LinkButton href={`/admissions/${admissionId}/summary`} variant="outline" className="h-tap">
          {t("printBack")}
        </LinkButton>
        <p className="text-muted-foreground text-sm">{bill("pdfHint")}</p>
        <PrintButton
          autoPrint={print === "1"}
          title={`${t("printTitle")} ${admission.admission_number}`}
        />
      </div>
      <PrintedSummary admission={admission} summary={summary} hospital={hospital} />
    </>
  );
}
