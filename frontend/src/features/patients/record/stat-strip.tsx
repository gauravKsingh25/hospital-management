import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { EncounterStatusChip } from "@/components/status-chip";
import type { PatientClinicalSummary } from "@/types/api";

function Stat({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div className="bg-card rounded-xl border px-4 py-3" data-testid={testId}>
      <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">{label}</p>
      <div className="mt-1">{children}</div>
    </div>
  );
}

/**
 * The four questions asked about a patient before anything else: how often
 * have they been here, when last, are they due back, and where are they now.
 */
export function StatStrip({ summary }: { summary: PatientClinicalSummary }) {
  const t = useTranslations("record");
  const format = useFormatter();
  const date = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium" });

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Stat label={t("statVisits")} testId="stat-visits">
        <p className="tabular text-2xl font-semibold">{summary.total_visits}</p>
        <p className="text-muted-foreground text-xs">
          {t("statVisitsBreakdown", {
            opd: summary.opd_visits,
            inpatient: summary.inpatient_visits,
            emergency: summary.emergency_visits,
          })}
        </p>
      </Stat>

      <Stat label={t("statLastVisit")}>
        {summary.last_visit_at ? (
          <>
            <p className="text-base font-semibold">{date(summary.last_visit_at)}</p>
            <p className="text-muted-foreground text-xs">
              {format.relativeTime(new Date(summary.last_visit_at))}
              {summary.first_visit_at && summary.total_visits > 1
                ? ` · ${t("statFirstVisit", { date: date(summary.first_visit_at) })}`
                : ""}
            </p>
          </>
        ) : (
          <p className="text-muted-foreground text-base">{t("statNoVisits")}</p>
        )}
      </Stat>

      <Stat label={t("statFollowUp")}>
        {summary.next_follow_up_date ? (
          <>
            <p className="text-base font-semibold">{date(summary.next_follow_up_date)}</p>
            <p className="text-muted-foreground text-xs">
              {format.relativeTime(new Date(summary.next_follow_up_date))}
            </p>
          </>
        ) : (
          <p className="text-muted-foreground text-base">{t("statNoFollowUp")}</p>
        )}
      </Stat>

      <Stat label={t("statNow")}>
        {summary.open_visit ? (
          <div className="space-y-1">
            <EncounterStatusChip status={summary.open_visit.status} />
            <p className="text-muted-foreground tabular text-xs">
              {summary.open_visit.encounter_number}
            </p>
          </div>
        ) : (
          <p className="text-muted-foreground text-base">{t("statNotInHospital")}</p>
        )}
      </Stat>
    </div>
  );
}
