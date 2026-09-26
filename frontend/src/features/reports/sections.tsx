"use client";

import { useTranslations } from "next-intl";

import { formatMoney } from "@/features/billing/money";
import { BarList, type BarRow } from "@/features/reports/bar-list";
import {
  DASH,
  formatCount,
  formatDays,
  formatMinutes,
  formatPerHundred,
  formatRate,
} from "@/features/reports/format";
import { NothingYet, Section, Stat, StatGrid } from "@/features/reports/stat";
import type {
  FollowUpCompliance,
  FootfallReport,
  InpatientReport,
  OccupancyReport,
  QueueSnapshot,
  RevenueReport,
} from "@/types/api";

/**
 * The dashboard's sections, one per report.
 *
 * Each renders `null` when its permission was not held — the backend leaves
 * those fields `None` rather than omitting them, so the layout is decided by
 * permission and not by whether a key happened to be present.
 */

/** A label for a row whose name is genuinely absent upstream. */
function useUnnamed(): string {
  return useTranslations("common")("notRecorded");
}

// ---------------------------------------------------------------------------
// Today — the only section that changes minute to minute
// ---------------------------------------------------------------------------
export function TodaySection({ queue }: { queue: QueueSnapshot | null | undefined }) {
  const t = useTranslations("reports");
  const unnamed = useUnnamed();
  if (!queue) return null;

  const late = queue.running_late ?? [];

  return (
    <Section id="report-today" title={t("todayTitle")} hint={t("todaySubtitle")}>
      <StatGrid>
        <Stat label={t("waiting")} value={formatCount(queue.waiting)} testId="stat-waiting" />
        <Stat label={t("inConsultation")} value={formatCount(queue.in_consultation)} />
        <Stat label={t("completed")} value={formatCount(queue.completed)} tone="good" />
        <Stat
          label={t("leftWithoutBeingSeen")}
          value={formatCount(queue.left_without_being_seen)}
          // Not a neutral count. Every one of these is a patient who waited,
          // gave up, and went home without being examined.
          tone={queue.left_without_being_seen > 0 ? "bad" : "muted"}
        />
        <Stat label={t("averageWait")} value={formatMinutes(queue.average_wait_minutes)} />
        <Stat
          label={t("longestWait")}
          value={formatMinutes(queue.longest_wait_minutes)}
          tone={
            queue.longest_wait_minutes !== null &&
            queue.longest_wait_minutes !== undefined &&
            queue.longest_wait_minutes > queue.delay_threshold_minutes
              ? "warn"
              : "default"
          }
        />
      </StatGrid>

      {late.length > 0 ? (
        <div
          className="border-caution/50 bg-caution/10 space-y-1 rounded-lg border p-4"
          data-testid="running-late"
        >
          <p className="text-sm font-medium">
            {t("runningLate", { minutes: queue.delay_threshold_minutes })}
          </p>
          <ul className="text-sm">
            {late.map((doctor) => (
              <li key={doctor.doctor_id ?? "unassigned"} className="tabular">
                {doctor.doctor_name ?? unnamed} ·{" "}
                {t("waitingCount", { count: doctor.waiting })} ·{" "}
                {t("longestIs", { minutes: formatMinutes(doctor.longest_wait_minutes) })}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Occupancy
// ---------------------------------------------------------------------------
export function OccupancySection({ report }: { report: OccupancyReport | null | undefined }) {
  const t = useTranslations("reports");
  if (!report) return null;

  const rows: BarRow[] = (report.by_ward ?? []).map((ward) => ({
    key: ward.ward_id,
    label: ward.ward_name,
    value: ward.occupancy_rate,
    display: formatRate(ward.occupancy_rate),
    detail: t("wardDetail", {
      code: ward.ward_code,
      occupied: ward.occupied,
      usable: ward.usable_beds,
      cleaning: ward.cleaning,
      blocked: ward.out_of_service,
    }),
    // A ward at or above 90% has no room for the next admission from casualty.
    tone: ward.occupancy_rate >= 0.9 ? "warn" : "default",
  }));

  return (
    <Section id="report-occupancy" title={t("occupancyTitle")} hint={t("occupancySubtitle")}>
      <StatGrid>
        <Stat
          label={t("occupancyRate")}
          value={formatRate(report.occupancy_rate)}
          testId="stat-occupancy"
        />
        <Stat label={t("occupied")} value={formatCount(report.occupied)} />
        <Stat label={t("available")} value={formatCount(report.available)} tone="good" />
        <Stat
          label={t("usableBeds")}
          value={formatCount(report.usable_beds)}
          // The denominator, spelled out: a bed under repair is not capacity,
          // and an occupancy rate against total beds flatters the hospital.
          hint={t("usableBedsHint", {
            total: report.total_beds,
            blocked: report.out_of_service,
          })}
        />
      </StatGrid>

      {rows.length > 0 ? (
        <BarList rows={rows} testId="occupancy-by-ward" />
      ) : (
        <NothingYet message={t("noWards")} />
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Footfall
// ---------------------------------------------------------------------------
export function FootfallStats({ report }: { report: FootfallReport }) {
  const t = useTranslations("reports");

  return (
    <StatGrid>
      <Stat label={t("totalVisits")} value={formatCount(report.total_visits)} testId="stat-visits" />
      <Stat label={t("patientsSeen")} value={formatCount(report.patients_seen)} tone="good" />
      <Stat label={t("cancelledOrNoShow")} value={formatCount(report.cancelled_or_no_show)} />
      <Stat
        label={t("newPatients")}
        value={formatCount(report.new_patients)}
        hint={t("newPatientsHint")}
      />
    </StatGrid>
  );
}

export function FootfallBreakdown({ report }: { report: FootfallReport }) {
  const t = useTranslations("reports");
  const unnamed = useUnnamed();

  const departments: BarRow[] = (report.by_department ?? []).map((row) => ({
    key: row.department_id ?? "unassigned",
    label: row.department_name ?? unnamed,
    value: row.total,
    display: formatCount(row.total),
    detail: t("loadDetail", {
      seen: row.seen,
      wait: formatMinutes(row.average_wait_minutes),
    }),
  }));

  const doctors: BarRow[] = (report.by_doctor ?? []).map((row) => ({
    key: row.doctor_id ?? "unassigned",
    label: row.doctor_name ?? unnamed,
    value: row.total,
    display: formatCount(row.total),
    detail: t("doctorDetail", {
      seen: row.seen,
      consult: formatMinutes(row.average_consultation_minutes),
      wait: formatMinutes(row.average_wait_minutes),
    }),
  }));

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="space-y-2">
        <h3 className="text-muted-foreground text-sm font-medium">{t("byDepartment")}</h3>
        {departments.length > 0 ? (
          <BarList rows={departments} testId="footfall-by-department" />
        ) : (
          <NothingYet message={t("noVisits")} />
        )}
      </div>
      <div className="space-y-2">
        <h3 className="text-muted-foreground text-sm font-medium">{t("byDoctor")}</h3>
        {doctors.length > 0 ? (
          <BarList rows={doctors} testId="footfall-by-doctor" />
        ) : (
          <NothingYet message={t("noVisits")} />
        )}
      </div>
    </div>
  );
}

/**
 * Visits the nightly sweep closed rather than a person.
 *
 * Shown apart from the volume tiles because it is not a volume — it is a
 * process smell. CLAUDE.md §6's auto-close exists so a forgotten click cannot
 * leave an encounter open forever, and a rising number here means staff are
 * relying on it, which degrades every metric derived from closure times.
 */
export function AutoClosedNote({ report }: { report: FootfallReport }) {
  const t = useTranslations("reports");
  if (report.auto_closed === 0) return null;

  return (
    <p
      className="border-caution/50 bg-caution/10 rounded-lg border px-3 py-2 text-sm"
      data-testid="auto-closed"
    >
      {t("autoClosed", { count: report.auto_closed })}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Inpatient
// ---------------------------------------------------------------------------
const DISCHARGE_TYPES = ["RECOVERED", "REFERRED", "TRANSFERRED_OUT", "LAMA", "DECEASED"] as const;

export function InpatientSection({ report }: { report: InpatientReport | null | undefined }) {
  const t = useTranslations("reports");
  if (!report) return null;

  const byType = report.by_discharge_type ?? {};
  const rows: BarRow[] = DISCHARGE_TYPES.filter((type) => (byType[type] ?? 0) > 0).map((type) => ({
    key: type,
    label: t(`discharge${type}` as "dischargeRECOVERED"),
    value: byType[type] ?? 0,
    display: formatCount(byType[type] ?? 0),
    tone: type === "DECEASED" ? "bad" : type === "LAMA" ? "warn" : "default",
  }));

  return (
    <Section id="report-inpatient" title={t("inpatientTitle")} hint={t("inpatientSubtitle")}>
      <StatGrid>
        <Stat label={t("admissions")} value={formatCount(report.admissions)} />
        <Stat label={t("discharges")} value={formatCount(report.discharges)} />
        <Stat
          label={t("currentlyAdmitted")}
          value={formatCount(report.currently_admitted)}
          hint={t("nowHint")}
        />
        {/*
          `null` when nobody was discharged in the window. Rendered as an em
          dash rather than 0.0 — an ALOS with no denominator is unknown, and
          "0.0 days" is a number somebody would repeat in a meeting.
        */}
        <Stat
          label={t("averageLengthOfStay")}
          value={formatDays(report.average_length_of_stay_days)}
          hint={report.average_length_of_stay_days === null ? t("noDischarges") : t("days")}
          testId="stat-alos"
        />
      </StatGrid>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <h3 className="text-muted-foreground text-sm font-medium">{t("howStaysEnded")}</h3>
          {rows.length > 0 ? (
            <BarList rows={rows} testId="inpatient-by-outcome" />
          ) : (
            <NothingYet message={t("noDischarges")} />
          )}
        </div>

        <StatGrid columns={3}>
          <Stat
            label={t("deaths")}
            value={formatCount(report.deaths)}
            tone={report.deaths > 0 ? "bad" : "muted"}
          />
          <Stat
            label={t("mortalityRate")}
            value={
              report.mortality_rate === null || report.mortality_rate === undefined
                ? DASH
                : formatPerHundred(report.mortality_rate)
            }
            // The count alone rises with volume and says nothing; the rate is
            // the number that can be compared across months or hospitals.
            hint={t("perHundredDischarges")}
          />
          <Stat label={t("discharges")} value={formatCount(report.discharges)} tone="muted" />
        </StatGrid>
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Follow-up
// ---------------------------------------------------------------------------
export function FollowUpSection({ report }: { report: FollowUpCompliance | null | undefined }) {
  const t = useTranslations("reports");
  if (!report) return null;

  return (
    <Section id="report-follow-up" title={t("followUpTitle")} hint={t("followUpSubtitle")}>
      <StatGrid>
        <Stat
          label={t("complianceRate")}
          value={formatRate(report.compliance_rate)}
          hint={t("complianceHint")}
          tone={
            report.compliance_rate === null || report.compliance_rate === undefined
              ? "muted"
              : report.compliance_rate >= 0.6
                ? "good"
                : "warn"
          }
          testId="stat-compliance"
        />
        <Stat label={t("advised")} value={formatCount(report.advised)} />
        <Stat label={t("honoured")} value={formatCount(report.honoured)} tone="good" />
        <Stat
          label={t("missed")}
          value={formatCount(report.missed)}
          hint={t("pendingExcluded", { count: report.pending })}
        />
      </StatGrid>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Revenue
// ---------------------------------------------------------------------------
export function RevenueSection({ report }: { report: RevenueReport | null | undefined }) {
  const t = useTranslations("reports");
  const tBilling = useTranslations("billing");
  if (!report) return null;

  const categories: BarRow[] = (report.by_category ?? []).map((row) => ({
    key: row.category,
    label: t(`category${row.category}` as "categoryLAB"),
    value: Number(row.billable) || 0,
    display: formatMoney(row.billable),
    detail: t("categoryDetail", { charges: row.charges, waived: formatMoney(row.waived) }),
  }));

  const methods: BarRow[] = Object.entries(report.by_method ?? {}).map(([method, amount]) => ({
    key: method,
    label: tBilling(`method${method}` as "methodUPI"),
    value: Number(amount) || 0,
    display: formatMoney(amount),
  }));

  return (
    <Section id="report-revenue" title={t("revenueTitle")} hint={t("revenueSubtitle")}>
      {/*
        Three numbers that look interchangeable on a tile and are not. Earned is
        what was charged, collected is cash that arrived, outstanding is what is
        owed right now — and outstanding is not windowed at all, because a debt
        is a fact about today. Each carries its own sentence for the same reason
        the backend refuses to sum them.
      */}
      <StatGrid>
        <Stat
          label={t("earned")}
          value={formatMoney(report.charges_raised)}
          hint={t("earnedHint")}
          testId="stat-earned"
        />
        <Stat
          label={t("collected")}
          value={formatMoney(report.collected)}
          hint={t("collectedHint")}
          tone="good"
          testId="stat-collected"
        />
        <Stat
          label={t("outstanding")}
          value={formatMoney(report.outstanding_total)}
          hint={t("outstandingHint")}
          tone="warn"
          testId="stat-outstanding"
        />
        <Stat
          label={t("waivedAndDiscounted")}
          value={formatMoney(report.waived)}
          hint={t("discountsHint", { amount: formatMoney(report.discounts_given) })}
          tone="muted"
        />
      </StatGrid>

      {report.unpriced_charges > 0 ? (
        <p
          className="border-caution/50 bg-caution/10 rounded-lg border px-3 py-2 text-sm"
          data-testid="unpriced-charges"
        >
          {t("unpricedCharges", { count: report.unpriced_charges })}
        </p>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <h3 className="text-muted-foreground text-sm font-medium">{t("byCategory")}</h3>
          {categories.length > 0 ? (
            <BarList rows={categories} testId="revenue-by-category" />
          ) : (
            <NothingYet message={t("noCharges")} />
          )}
        </div>
        <div className="space-y-2">
          <h3 className="text-muted-foreground text-sm font-medium">{t("byMethod")}</h3>
          {methods.length > 0 ? (
            <BarList rows={methods} testId="revenue-by-method" />
          ) : (
            <NothingYet message={t("noPayments")} />
          )}
        </div>
      </div>
    </Section>
  );
}
