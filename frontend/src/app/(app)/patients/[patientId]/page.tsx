import { notFound } from "next/navigation";
import { QrCode } from "lucide-react";
import { getFormatter, getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { EncounterStatusChip } from "@/components/status-chip";
import { LinkButton } from "@/components/ui/link-button";
import { PatientMessaging } from "@/features/messages/patient-messaging";
import { ApiError } from "@/lib/api/error";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { EncounterSummary, Page, Patient } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("patients") };
}

/**
 * The patient record.
 *
 * Deliberately thin at this stage. It exists because the universal search and
 * the duplicate warning both need somewhere to send you — a "use this record"
 * button that leads nowhere is worse than no button — and because the visit
 * history is what tells reception whether the person at the counter is
 * already in the middle of something.
 *
 * The full record (identifiers, consents, alerts management, ABHA linkage) is
 * the `patients` module's own frontend slice and is not built yet. What is
 * here is the part the reception and doctor slices actually depend on.
 */
export default async function PatientPage({ params }: PageProps<"/patients/[patientId]">) {
  const { patientId } = await params;
  const t = await getTranslations("patient");
  const tConsult = await getTranslations("consultation");
  const tCard = await getTranslations("card");
  const common = await getTranslations("common");
  const format = await getFormatter();

  const user = await getCurrentUser();
  if (!can(user, "patient:read")) return <NoPermission />;

  let patient: Patient;
  try {
    patient = await serverFetch<Patient>(`/patients/${patientId}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  const visits = await serverFetch<Page<EncounterSummary>>("/encounters", {
    query: { patient_id: patientId, limit: 20 },
  });

  // `alerts` carries a server-side default, so the schema marks it optional
  // even though a response always has it. Resolved once here rather than
  // guarded at each use.
  const activeAlerts = (patient.alerts ?? []).filter((alert) => alert.is_active);

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <header className="space-y-2">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">{patient.full_name}</h1>
          <span className="text-muted-foreground tabular text-sm">
            {t("uhid")} {patient.uhid}
          </span>
          {/* Reprinting a lost card is a counter task, so it lives on the
              record rather than behind an administration screen. */}
          <LinkButton variant="ghost" size="sm" className="ml-auto" href={`/patients/${patient.id}/card`}>
            <QrCode aria-hidden className="size-4" />
            {tCard("title")}
          </LinkButton>
        </div>
        <p className="text-muted-foreground text-sm">
          {patient.phone}
          {" · "}
          {patient.age_years === null || patient.age_years === undefined
            ? common("notRecorded")
            : t("ageYears", { age: patient.age_years })}
          {" · "}
          {t(`gender${patient.gender}` as "genderMALE")}
          {patient.blood_group !== "UNKNOWN" ? ` · ${patient.blood_group}` : ""}
        </p>
      </header>

      {/* The safety alerts, if any. Same rule as the consultation screen:
          never collapsed, never behind a tab. */}
      {activeAlerts.length > 0 ? (
        <ul className="border-caution/50 bg-caution/10 space-y-1 rounded-lg border px-4 py-3">
          {activeAlerts.map((alert) => (
            <li key={alert.id} className="text-sm font-medium">
              {alert.label}
              {alert.detail ? (
                <span className="text-muted-foreground font-normal"> — {alert.detail}</span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {/* Whether the hospital is messaging this person, and the one-click way
          to stop — DPDP Act 2023 §6 makes consent withdrawable, and a
          withdrawal that needs a support ticket is not really withdrawable. */}
      {can(user, "suppression:read") ? (
        <PatientMessaging
          patientId={patient.id}
          patientName={patient.full_name}
          canManage={can(user, "suppression:manage")}
        />
      ) : null}

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">{tConsult("history")}</h2>

        {visits.items.length === 0 ? (
          <p className="text-muted-foreground rounded-lg border border-dashed px-4 py-6 text-center text-sm">
            {common("none")}
          </p>
        ) : (
          <ul className="divide-border divide-y rounded-lg border">
            {visits.items.map((visit) => (
              <li key={visit.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <p className="tabular text-sm font-medium">{visit.encounter_number}</p>
                  <p className="text-muted-foreground text-xs">
                    {format.dateTime(new Date(visit.started_at), "short")}
                    {visit.chief_complaint ? ` · ${visit.chief_complaint}` : ""}
                  </p>
                </div>
                <EncounterStatusChip status={visit.status} />
                <LinkButton variant="ghost" size="sm" href={`/consultation/${visit.id}`}>
                  {common("next")}
                </LinkButton>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
