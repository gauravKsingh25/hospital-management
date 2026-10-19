import { CalendarClock, Stethoscope, User } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { EncounterStatusChip } from "@/components/status-chip";
import { OutcomeSummary } from "@/features/outcomes/terminal-outcome";
import { VisitOutcomeAction } from "@/features/outcomes/visit-outcome-action";
import { AddDiagnosis } from "@/features/patients/record/add-diagnosis";
import { cn } from "@/lib/utils";
import type { Encounter, OrderType, PatientVisit } from "@/types/api";

/** Visits whose ending can no longer change. */
export const CLOSED_VISITS = new Set([
  "COMPLETED",
  "CANCELLED",
  "NO_SHOW",
  "REFERRED_OUT",
  "LAMA",
  "DECEASED",
]);

/** Visits whose ending carries a record worth showing. */
export const OUTCOME_VISITS = new Set(["DECEASED", "REFERRED_OUT", "LAMA"]);

export type VisitPermissions = {
  canRecordDeath: boolean;
  canRecordReferral: boolean;
  canRecordLama: boolean;
  canRecordDiagnosis: boolean;
};

/**
 * The visit history: one card per visit, newest first.
 *
 * Each card answers what a clinician scanning old notes asks — when, with
 * whom, why they came, what was found, what was ordered, and when they were
 * told to come back. Cancelled and no-show bookings stay in the list, greyed
 * and unnumbered: "did they come last Tuesday?" is a real question, and the
 * answer "no" is part of the history.
 */
export function VisitList({
  visits,
  outcomes,
  patientName,
  permissions,
  compact = false,
}: {
  visits: PatientVisit[];
  outcomes: Map<string, Encounter>;
  patientName: string;
  permissions: VisitPermissions;
  compact?: boolean;
}) {
  return (
    <ol className="space-y-3" data-testid="visit-list">
      {visits.map((visit) => (
        <VisitCard
          key={visit.id}
          visit={visit}
          outcome={outcomes.get(visit.id)}
          patientName={patientName}
          permissions={permissions}
          compact={compact}
        />
      ))}
    </ol>
  );
}

function VisitCard({
  visit,
  outcome,
  patientName,
  permissions,
  compact,
}: {
  visit: PatientVisit;
  outcome: Encounter | undefined;
  patientName: string;
  permissions: VisitPermissions;
  compact: boolean;
}) {
  const t = useTranslations("record");
  const tConsult = useTranslations("consultation");
  const format = useFormatter();
  const open = !CLOSED_VISITS.has(visit.status);
  const attended = visit.visit_number !== null && visit.visit_number !== undefined;

  // Orders grouped by kind, so "3 lab tests, 2 medicines" reads as one line.
  const ordersByType = new Map<OrderType, string[]>();
  for (const order of visit.orders ?? []) {
    if (order.status === "CANCELLED") continue;
    ordersByType.set(order.order_type, [...(ordersByType.get(order.order_type) ?? []), order.item_name]);
  }

  return (
    <li
      className={cn(
        "bg-card rounded-xl border",
        open && "border-primary/40 ring-primary/10 ring-2",
        !attended && "opacity-70",
      )}
      data-testid="visit-card"
      data-status={visit.status}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2.5">
        <span
          className={cn(
            "tabular rounded-md px-2 py-0.5 text-xs font-semibold",
            attended ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
          )}
        >
          {attended ? t("visitNumber", { number: visit.visit_number! }) : t("notAttended")}
        </span>
        <span className="text-sm font-medium">
          {format.dateTime(new Date(visit.started_at), { dateStyle: "medium", timeStyle: "short" })}
        </span>
        <span className="text-muted-foreground text-xs">
          {t(`type${visit.encounter_type}` as "typeOPD")} · <span className="tabular">{visit.encounter_number}</span>
        </span>
        <span className="ml-auto">
          <EncounterStatusChip status={visit.status} />
        </span>
      </div>

      <div className="space-y-2.5 px-4 py-3 text-sm">
        {visit.doctor_name || visit.department_name ? (
          <p className="text-muted-foreground flex items-center gap-1.5">
            <User aria-hidden className="size-4 shrink-0" />
            <span className="text-foreground font-medium">{visit.doctor_name}</span>
            {visit.doctor_name && visit.department_name ? " · " : null}
            {visit.department_name}
          </p>
        ) : null}

        {visit.chief_complaint ? (
          <p>
            <span className="text-muted-foreground">{t("complaint")}: </span>
            {visit.chief_complaint}
          </p>
        ) : null}

        {visit.diagnoses !== null && visit.diagnoses !== undefined ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <Stethoscope aria-hidden className="text-muted-foreground size-4 shrink-0" />
            {visit.diagnoses.length === 0 ? (
              <span className="text-muted-foreground">{t("noDiagnosis")}</span>
            ) : (
              visit.diagnoses.map((diagnosis) => (
                <span
                  key={`${diagnosis.code ?? ""}-${diagnosis.description}`}
                  className={cn(
                    "rounded-md border px-2 py-0.5 text-xs",
                    diagnosis.is_primary ? "border-primary/30 bg-primary/5 font-semibold" : "bg-muted/50",
                  )}
                  data-testid="visit-diagnosis"
                >
                  {diagnosis.description}
                  {diagnosis.code ? <span className="text-muted-foreground ml-1 tabular">{diagnosis.code}</span> : null}
                </span>
              ))
            )}
            {open && permissions.canRecordDiagnosis ? (
              <AddDiagnosis encounterId={visit.id} patientName={patientName} />
            ) : null}
          </div>
        ) : null}

        {!compact && ordersByType.size > 0 ? (
          <dl className="grid gap-1 sm:grid-cols-[auto_1fr] sm:gap-x-3">
            {[...ordersByType.entries()].map(([type, items]) => (
              <div key={type} className="contents">
                <dt className="text-muted-foreground text-xs sm:pt-0.5">
                  {tConsult(`orderType${type}` as "orderTypeLAB")}
                </dt>
                <dd className="text-sm">{items.join(", ")}</dd>
              </div>
            ))}
          </dl>
        ) : null}

        {visit.follow_up_date ? (
          <p className="text-muted-foreground flex items-center gap-1.5 text-xs">
            <CalendarClock aria-hidden className="size-4 shrink-0" />
            {t("followUpOn", {
              date: format.dateTime(new Date(visit.follow_up_date), { dateStyle: "medium" }),
            })}
            {visit.follow_up_instructions ? ` — ${visit.follow_up_instructions}` : ""}
          </p>
        ) : null}

        {outcome ? <OutcomeSummary encounter={outcome} /> : null}

        {/* Death, referral or LAMA against a visit that is still open —
            on the overview too, because the open visit is what staff come for. */}
        {open ? (
          <div className="flex justify-end">
            <VisitOutcomeAction
              encounterId={visit.id}
              patientName={patientName}
              canRecordDeath={permissions.canRecordDeath}
              canRecordReferral={permissions.canRecordReferral}
              canRecordLama={permissions.canRecordLama}
            />
          </div>
        ) : null}
      </div>
    </li>
  );
}
