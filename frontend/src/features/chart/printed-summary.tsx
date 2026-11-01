import { getFormatter, getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import type { Admission, DischargeSummary, Hospital } from "@/types/api";

/**
 * The discharge summary as the patient takes it home — an A4 page in the
 * familiar hospital layout: the hospital's mark on top, who and where in two
 * columns, when they left, then diagnosis, complaints and course, and the
 * hospital's emergency number and full identity at the foot.
 *
 * A server component: nothing on paper needs JavaScript. The hospital's
 * details are the profile as it is now (Settings → Hospital profile), so the
 * page always carries the address of the hospital running the system. A
 * summary nobody has signed yet says so across the top.
 */
type Section = { key: keyof DischargeSummary; labelKey: string };

// Always printed, in this order — the three the format is built around.
const CORE: Section[] = [
  { key: "diagnoses", labelKey: "printDiagnosis" },
  { key: "presenting_complaint", labelKey: "printPresentingComplaints" },
  { key: "course_in_hospital", labelKey: "printCourse" },
];

// Printed only when filled: an empty heading on paper reads as an omission.
const MORE: Section[] = [
  { key: "investigations", labelKey: "investigations" },
  { key: "procedures", labelKey: "procedures" },
  { key: "treatment_given", labelKey: "treatmentGiven" },
  { key: "condition_at_discharge", labelKey: "conditionAtDischarge" },
  { key: "discharge_medications", labelKey: "dischargeMedications" },
  { key: "follow_up_instructions", labelKey: "followUpInstructions" },
  { key: "diet_and_activity", labelKey: "dietAndActivity" },
  { key: "warning_signs", labelKey: "warningSigns" },
];

export async function PrintedSummary({
  admission,
  summary,
  hospital,
}: {
  admission: Admission;
  summary: DischargeSummary;
  hospital: Hospital;
}) {
  const t = await getTranslations("summary");
  const bill = await getTranslations("bill");
  const format = await getFormatter();
  const day = (value: string) =>
    format.dateTime(new Date(value), { day: "2-digit", month: "short", year: "numeric" });
  const moment = (value: string) =>
    format.dateTime(new Date(value), {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });

  const text = (key: keyof DischargeSummary) => ((summary[key] as string | null) ?? "").trim();
  const more = MORE.filter((section) => text(section.key));
  const signed = summary.status !== "DRAFT" && summary.signed_by_name;
  const place = [hospital.address_line1, hospital.address_line2, hospital.city, hospital.state]
    .filter(Boolean)
    .join(", ");
  const ageSex = [
    admission.patient_age_years != null ? t("printAge", { age: admission.patient_age_years }) : null,
    admission.patient_gender?.[0],
  ]
    .filter(Boolean)
    .join(" / ");

  return (
    <article
      className="mx-auto max-w-[210mm] space-y-5 p-6 text-[13px] leading-snug print:p-0"
      data-testid="printed-summary"
    >
      <header className="flex items-center gap-4" data-testid="letterhead">
        {hospital.logo_version ? (
          // eslint-disable-next-line @next/next/no-img-element -- a tenant-private stored file
          <img
            src={`/bff/hospitals/${hospital.id}/logo?version=${hospital.logo_version}`}
            alt={bill("logoAlt")}
            className="max-h-20 max-w-56 object-contain"
          />
        ) : null}
        <p className="text-2xl font-bold">{hospital.name}</p>
      </header>

      <h1 className="text-center text-xl font-bold underline underline-offset-4">
        {t("printTitle")}
      </h1>
      {signed ? null : (
        <p className="border border-black py-1 text-center font-bold" data-testid="summary-draft">
          {t("printDraft")}
        </p>
      )}

      <section className="grid grid-cols-2 gap-x-8 gap-y-1">
        <dl className="space-y-1">
          <Row label={t("printPatient")} value={admission.patient_name ?? "—"} />
          <Row
            label={t("printLocation")}
            value={admission.last_ward_bed ?? "—"}
          />
          <Row label={t("printConsultant")} value={admission.attending_doctor_name ?? "—"} />
          <Row label={t("printAdmissionNo")} value={admission.admission_number} />
        </dl>
        <dl className="space-y-1">
          <Row label={t("printAgeSex")} value={ageSex || "—"} />
          <Row label="UHID" value={admission.uhid ?? "—"} />
          <Row label={t("printAdmitted")} value={moment(admission.admitted_at)} />
        </dl>
      </section>

      <Block label={t("printDischarged")}>
        {admission.discharged_at ? moment(admission.discharged_at) : t("printNotDischarged")}
      </Block>

      {CORE.map((section) => (
        <Block key={section.key} label={t(section.labelKey as "printDiagnosis")}>
          {text(section.key) || "—"}
        </Block>
      ))}
      {more.map((section) => (
        <Block key={section.key} label={t(section.labelKey as "investigations")}>
          {text(section.key)}
          {section.key === "follow_up_instructions" && summary.follow_up_date ? (
            <span className="block font-medium">
              {t("printReviewOn", { date: day(summary.follow_up_date) })}
            </span>
          ) : null}
        </Block>
      ))}

      {signed ? (
        <p data-testid="summary-signature">
          {t("printSignedBy", {
            name: summary.signed_by_name ?? "",
            date: summary.signed_at ? moment(summary.signed_at) : "",
          })}
          {summary.signatory_registration_number
            ? ` · ${t("printRegistration", { number: summary.signatory_registration_number })}`
            : null}
        </p>
      ) : null}

      {hospital.phone ? (
        <p
          className="border-y-2 border-dashed border-black py-2 text-center text-xl font-semibold uppercase"
          data-testid="summary-emergency"
        >
          {t("printEmergency", { phone: hospital.phone })}
        </p>
      ) : null}

      <p className="font-medium">
        {signed ? t("printComputerGeneratedSigned") : t("printComputerGenerated")}
      </p>

      <footer className="space-y-0.5 text-[12px]" data-testid="summary-hospital">
        <p className="font-bold">{hospital.legal_name || hospital.name}</p>
        {hospital.legal_name && hospital.legal_name !== hospital.name ? (
          <p className="font-bold">({hospital.name})</p>
        ) : null}
        {place ? <p>{[place, hospital.pincode].filter(Boolean).join(" - ")}</p> : null}
        {hospital.phone || hospital.email ? (
          <p>
            {[hospital.phone && `${bill("phone")} ${hospital.phone}`, hospital.email]
              .filter(Boolean)
              .join(" · ")}
          </p>
        ) : null}
        {hospital.gstin || hospital.hfr_facility_id ? (
          <p>
            {[hospital.gstin && `GSTIN ${hospital.gstin}`, hospital.hfr_facility_id && `HFR ${hospital.hfr_facility_id}`]
              .filter(Boolean)
              .join(" · ")}
          </p>
        ) : null}
      </footer>
    </article>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2">
      <dt className="w-36 shrink-0 font-bold uppercase">{label}</dt>
      <dd>: {value}</dd>
    </div>
  );
}

function Block({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section className="break-inside-avoid space-y-1">
      <h2 className="font-bold uppercase">{label}:</h2>
      <div className="whitespace-pre-line">{children}</div>
    </section>
  );
}
