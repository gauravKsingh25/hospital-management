import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { PatientQr } from "@/components/patient/qr-code";
import { PrintCardButton } from "@/features/patients/patient-card";
import { ApiError } from "@/lib/api/error";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { Patient } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("card");
  return { title: t("title") };
}

/**
 * The patient's card — the thing they bring back (CLAUDE.md §7b).
 *
 * Server-rendered end to end, so the QR encoder never reaches the browser on
 * this route. Nothing here is interactive except the print button.
 *
 * ## What is on it, and what is deliberately not
 *
 * Name, UHID, the QR, and the hospital. **No date of birth, no phone number,
 * no address, no diagnosis.** A card is carried in a wallet and left on
 * counters; every extra field on it is personal data that leaves the building
 * on a piece of paper, which the DPDP Act treats as processing like any other.
 * The UHID has to be there because it is the identifier the card exists to
 * carry; nothing else earns its place.
 *
 * Blood group is the one arguable omission — it is genuinely useful in an
 * emergency — and it is left off because a blood group printed months ago and
 * read in a crisis is exactly the kind of stale data that gets acted on. The
 * safety banner in the chart is where it belongs.
 */
export default async function PatientCardPage({
  params,
}: PageProps<"/patients/[patientId]/card">) {
  const { patientId } = await params;
  const t = await getTranslations("card");
  const tPatient = await getTranslations("patient");

  const user = await getCurrentUser();
  if (!can(user, "patient:read")) return <NoPermission />;

  let patient: Patient;
  try {
    patient = await serverFetch<Patient>(`/patients/${patientId}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  return (
    <div className="mx-auto max-w-md space-y-6">
      <div className="flex items-center justify-between gap-3 no-print">
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <PrintCardButton />
      </div>

      {/*
        Sized in millimetres rather than pixels: this is a physical object,
        roughly a credit card at 86 × 54 mm, and the only dimension that
        matters is the printed one.
      */}
      <div
        className="bg-card mx-auto flex items-center gap-4 rounded-xl border p-4 shadow-sm print:border print:shadow-none"
        style={{ width: "86mm", minHeight: "54mm" }}
        data-testid="patient-card"
      >
        <div className="min-w-0 flex-1 space-y-1">
          <p className="text-muted-foreground text-[10px] tracking-widest uppercase">
            {t("hospital")}
          </p>
          <p className="truncate text-lg leading-tight font-semibold">{patient.full_name}</p>
          <p className="text-muted-foreground tabular text-sm">
            {tPatient("uhid")} {patient.uhid}
          </p>
          <p className="text-muted-foreground pt-2 text-[10px]">{t("bringThis")}</p>
        </div>

        <PatientQr uhid={patient.uhid} size="26mm" className="shrink-0" />
      </div>

      <p className="text-muted-foreground text-center text-xs no-print">{t("printHint")}</p>
    </div>
  );
}
