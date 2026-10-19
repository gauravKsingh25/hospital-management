import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { NoPermission } from "@/components/no-permission";
import { SafetyBanner } from "@/components/patient/safety-banner";
import { PatientMessaging } from "@/features/messages/patient-messaging";
import { ProblemList } from "@/features/patients/record/problem-list";
import { ProfilePanel } from "@/features/patients/record/profile-panel";
import { RecordHeader } from "@/features/patients/record/record-header";
import {
  AdmissionsList,
  InvoicesList,
  ReportsList,
} from "@/features/patients/record/record-lists";
import { parseTab, RecordTabs, type RecordTab } from "@/features/patients/record/record-tabs";
import { StatStrip } from "@/features/patients/record/stat-strip";
import { OUTCOME_VISITS, VisitList, type VisitPermissions } from "@/features/patients/record/visit-list";
import { VitalsCard } from "@/features/patients/record/vitals-card";
import { ApiError } from "@/lib/api/error";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type {
  AdmissionSummary,
  DischargeSummary,
  Encounter,
  InvoiceSummary,
  Page,
  Patient,
  PatientClinicalSummary,
  PatientVisit,
  ReportSummary,
  SafetyBanner as SafetyBannerData,
} from "@/types/api";

const VISITS_PAGE_SIZE = 10;
const RECENT_VISITS = 3;
// A patient's admissions, reports and invoices number in the tens; one page
// of this size is every one of them for all but a handful of records, and
// those see a "showing N of M" note rather than a silent cut.
const LIST_LIMIT = 100;

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("patients") };
}

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/** Full encounters for the (rare) visits that ended in death, referral or LAMA. */
async function loadOutcomes(visits: PatientVisit[]): Promise<Map<string, Encounter>> {
  return new Map(
    await Promise.all(
      visits
        .filter((visit) => OUTCOME_VISITS.has(visit.status))
        .map(async (visit) => [visit.id, await serverFetch<Encounter>(`/encounters/${visit.id}`)] as const),
    ),
  );
}

/**
 * The patient record — one place for everything the hospital knows about a
 * person.
 *
 * Who they are, whether anything about them is dangerous, how often they have
 * been here, what they were treated for, and every visit, stay, report and
 * bill — each section shown only to the roles allowed to read it.
 *
 * Sections are URL tabs rendered on the server: only the section being read
 * is fetched, the URL says where you are, and the page ships no tab library.
 * The clinical sections come from `GET /encounters/patient/{id}/summary` and
 * `/visits`, which count a merged duplicate's history as this record's.
 */
export default async function PatientPage({
  params,
  searchParams,
}: PageProps<"/patients/[patientId]">) {
  const { patientId } = await params;
  const query = await searchParams;
  const t = await getTranslations("record");

  const user = await getCurrentUser();
  if (!can(user, "patient:read")) return <NoPermission />;

  let patient: Patient;
  try {
    patient = await serverFetch<Patient>(`/patients/${patientId}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  // An old card or link to a record since merged: the API follows the merge,
  // and the address bar should too, so the next click is on the right record.
  if (patient.id !== patientId) {
    const tab = first(query.tab);
    redirect(`/patients/${patient.id}${tab ? `?tab=${encodeURIComponent(tab)}` : ""}`);
  }

  const canSeeVisits = can(user, "encounter:read");
  const allowed: Record<RecordTab, boolean> = {
    overview: true,
    visits: canSeeVisits,
    admissions: can(user, "admission:read"),
    reports: can(user, "report:read"),
    billing: can(user, "invoice:read"),
    profile: true,
  };
  const requested = parseTab(query.tab);
  const tab: RecordTab = allowed[requested] ? requested : "overview";

  const summary = canSeeVisits
    ? await serverFetch<PatientClinicalSummary>(`/encounters/patient/${patient.id}/summary`)
    : null;

  const visitPermissions: VisitPermissions = {
    canRecordDeath: can(user, "encounter:record_death"),
    canRecordReferral: can(user, "encounter:record_referral"),
    canRecordLama: can(user, "encounter:record_lama"),
    canRecordDiagnosis: can(user, "diagnosis:record"),
  };

  const activeAlerts = (patient.alerts ?? []).filter((alert) => alert.is_active);
  const banner: SafetyBannerData = {
    patient_id: patient.id,
    uhid: patient.uhid,
    full_name: patient.full_name,
    age_years: patient.age_years ?? null,
    gender: patient.gender,
    blood_group: patient.blood_group,
    is_deceased: patient.is_deceased,
    alerts: activeAlerts.map((alert) => (alert.detail ? `${alert.label} — ${alert.detail}` : alert.label)),
    has_critical_alert: activeAlerts.some((alert) => alert.severity === "CRITICAL"),
  };

  const tabs = (
    [
      { key: "overview", label: t("tabOverview") },
      { key: "visits", label: t("tabVisits"), count: summary?.total_visits },
      { key: "admissions", label: t("tabAdmissions") },
      { key: "reports", label: t("tabReports") },
      { key: "billing", label: t("tabBilling") },
      { key: "profile", label: t("tabProfile") },
    ] satisfies { key: RecordTab; label: string; count?: number }[]
  ).filter((entry) => allowed[entry.key]);

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <RecordHeader patient={patient} />

      {/* Never collapsed, never behind a tab (CLAUDE.md §7b). */}
      <SafetyBanner banner={banner} />

      {summary ? <StatStrip summary={summary} /> : null}

      <RecordTabs patientId={patient.id} active={tab} tabs={tabs} />

      <div data-testid={`record-panel-${tab}`}>
        {tab === "overview" ? (
          <OverviewTab
            patient={patient}
            summary={summary}
            canReadVitals={can(user, "vitals:read")}
            permissions={visitPermissions}
            viewAllLabel={t("seeAllVisits", { count: summary?.total_visits ?? 0 })}
            recentLabel={t("recentVisits")}
            noVisits={t("noVisits")}
            messaging={
              // Whether the hospital is messaging this person, and the
              // one-click way to stop — DPDP Act 2023 §6 makes consent
              // withdrawable, and a withdrawal that needs a support ticket is
              // not really withdrawable. On the overview, where it always was.
              can(user, "suppression:read") ? (
                <PatientMessaging
                  patientId={patient.id}
                  patientName={patient.full_name}
                  canManage={can(user, "suppression:manage")}
                />
              ) : null
            }
          />
        ) : tab === "visits" ? (
          <VisitsTab
            patient={patient}
            page={Math.max(1, Number.parseInt(first(query.page) ?? "1", 10) || 1)}
            permissions={visitPermissions}
            labels={{
              empty: t("noVisits"),
              emptyHint: t("noVisitsHint"),
              newer: t("previous"),
              older: t("next"),
            }}
            pageOf={(page, pages) => t("pageOf", { page, pages })}
          />
        ) : tab === "admissions" ? (
          <AdmissionsTab patientId={patient.id} canReadSummary={can(user, "summary:read")} />
        ) : tab === "reports" ? (
          <Capped
            page={await serverFetch<Page<ReportSummary>>("/diagnostics/reports", {
              query: { patient_id: patient.id, limit: LIST_LIMIT },
            })}
            note={(shown, total) => t("partialList", { shown, total })}
          >
            {(items) => <ReportsList reports={items} />}
          </Capped>
        ) : tab === "billing" ? (
          <Capped
            page={await serverFetch<Page<InvoiceSummary>>("/billing/invoices", {
              query: { patient_id: patient.id, limit: LIST_LIMIT },
            })}
            note={(shown, total) => t("partialList", { shown, total })}
          >
            {(items) => <InvoicesList invoices={items} />}
          </Capped>
        ) : (
          <ProfilePanel patient={patient} />
        )}
      </div>
    </div>
  );
}

/**
 * A list read from a worklist endpoint, re-ordered for a record.
 *
 * The lab bench and the cash counter order their worklists oldest-first (the
 * oldest request is the one to work next); a history reads newest-first. Any
 * cap is stated rather than silent.
 */
function Capped<T extends { created_at: string }>({
  page,
  note,
  children,
}: {
  page: Page<T>;
  note: (shown: number, total: number) => string;
  children: (items: T[]) => ReactNode;
}) {
  const items = [...page.items].sort((a, b) => b.created_at.localeCompare(a.created_at));
  return (
    <div className="space-y-2">
      {children(items)}
      {page.total > items.length ? (
        <p className="text-muted-foreground text-xs">{note(items.length, page.total)}</p>
      ) : null}
    </div>
  );
}

async function OverviewTab({
  patient,
  summary,
  canReadVitals,
  permissions,
  viewAllLabel,
  recentLabel,
  noVisits,
  messaging,
}: {
  patient: Patient;
  summary: PatientClinicalSummary | null;
  canReadVitals: boolean;
  permissions: VisitPermissions;
  viewAllLabel: string;
  recentLabel: string;
  noVisits: string;
  messaging: ReactNode;
}) {
  const recent = summary
    ? await serverFetch<Page<PatientVisit>>(`/encounters/patient/${patient.id}/visits`, {
        query: { limit: RECENT_VISITS },
      })
    : null;
  const outcomes = recent ? await loadOutcomes(recent.items) : new Map<string, Encounter>();

  return (
    <div className="space-y-5">
      <div className="grid gap-4 lg:grid-cols-[3fr_2fr]">
        <ProblemList problems={summary?.problems} />
        {/* `undefined` reads "not shown to your role"; `null` reads "none taken". */}
        <VitalsCard vitals={canReadVitals ? (summary?.latest_vitals ?? null) : undefined} />
      </div>

      {recent ? (
        <section className="space-y-3">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="text-base font-semibold">{recentLabel}</h2>
            {recent.total > recent.items.length ? (
              <Link
                href={`/patients/${patient.id}?tab=visits`}
                className="text-primary text-sm font-medium underline-offset-4 hover:underline"
              >
                {viewAllLabel}
              </Link>
            ) : null}
          </div>
          {recent.items.length === 0 ? (
            <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-8 text-center text-sm">
              {noVisits}
            </p>
          ) : (
            <VisitList
              visits={recent.items}
              outcomes={outcomes}
              patientName={patient.full_name}
              permissions={permissions}
              compact
            />
          )}
        </section>
      ) : null}

      {messaging}
    </div>
  );
}

async function VisitsTab({
  patient,
  page,
  permissions,
  labels,
  pageOf,
}: {
  patient: Patient;
  page: number;
  permissions: VisitPermissions;
  labels: { empty: string; emptyHint: string; newer: string; older: string };
  pageOf: (page: number, pages: number) => string;
}) {
  const visits = await serverFetch<Page<PatientVisit>>(`/encounters/patient/${patient.id}/visits`, {
    query: { limit: VISITS_PAGE_SIZE, offset: (page - 1) * VISITS_PAGE_SIZE },
  });
  const outcomes = await loadOutcomes(visits.items);
  const pages = Math.max(1, Math.ceil(visits.total / VISITS_PAGE_SIZE));
  const href = (target: number) => `/patients/${patient.id}?tab=visits&page=${target}`;

  if (visits.total === 0) {
    return (
      <div className="text-muted-foreground rounded-xl border border-dashed px-4 py-12 text-center">
        <p className="text-foreground text-sm font-medium">{labels.empty}</p>
        <p className="mt-1 text-sm">{labels.emptyHint}</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <VisitList
        visits={visits.items}
        outcomes={outcomes}
        patientName={patient.full_name}
        permissions={permissions}
      />
      {pages > 1 ? (
        <nav className="flex items-center justify-between gap-3" aria-label={pageOf(page, pages)}>
          <p className="text-muted-foreground text-sm">{pageOf(page, pages)}</p>
          <div className="flex gap-2">
            {page > 1 ? (
              <Link href={href(page - 1)} className="h-tap inline-flex items-center rounded-md border px-3 text-sm">
                {labels.newer}
              </Link>
            ) : null}
            {visits.has_more ? (
              <Link href={href(page + 1)} className="h-tap inline-flex items-center rounded-md border px-3 text-sm">
                {labels.older}
              </Link>
            ) : null}
          </div>
        </nav>
      ) : null}
    </div>
  );
}

async function AdmissionsTab({ patientId, canReadSummary }: { patientId: string; canReadSummary: boolean }) {
  const [admissions, summaries] = await Promise.all([
    serverFetch<Page<AdmissionSummary>>("/ipd/admissions", {
      query: { patient_id: patientId, limit: LIST_LIMIT },
    }),
    canReadSummary
      ? serverFetch<Page<DischargeSummary>>("/ipd/summaries", {
          query: { patient_id: patientId, limit: LIST_LIMIT },
        })
      : null,
  ]);

  // An amended summary is a new row; the newest one per stay is the one that
  // stands.
  const byAdmission = new Map<string, DischargeSummary>();
  for (const summary of summaries?.items ?? []) {
    const current = byAdmission.get(summary.admission_id);
    if (!current || summary.created_at > current.created_at) {
      byAdmission.set(summary.admission_id, summary);
    }
  }

  return (
    <AdmissionsList
      admissions={admissions.items}
      summaries={byAdmission}
      canReadSummary={canReadSummary}
      // eslint-disable-next-line react-hooks/purity -- a server render is one instant; "still admitted" counts nights to it.
      now={Date.now()}
    />
  );
}
