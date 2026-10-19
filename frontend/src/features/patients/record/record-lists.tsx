import { AlertOctagon, BedDouble, FileText, FlaskConical, IndianRupee } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { InvoiceStatusChip } from "@/components/status-chip";
import { LinkButton } from "@/components/ui/link-button";
import { formatMoney, isPositive } from "@/features/billing/money";
import { cn } from "@/lib/utils";
import type { AdmissionSummary, DischargeSummary, InvoiceSummary, ReportSummary } from "@/types/api";

/** A titled list section with an icon and a calm empty state. */
export function RecordSection({
  icon,
  title,
  hint,
  empty,
  isEmpty,
  children,
  testId,
}: {
  icon: ReactNode;
  title: string;
  hint?: string;
  empty: string;
  isEmpty: boolean;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section className="bg-card rounded-xl border" data-testid={testId}>
      <div className="flex items-start gap-3 border-b px-4 py-3">
        <span className="text-primary mt-0.5 shrink-0">{icon}</span>
        <div>
          <h2 className="text-base font-semibold">{title}</h2>
          {hint ? <p className="text-muted-foreground text-xs">{hint}</p> : null}
        </div>
      </div>
      {isEmpty ? (
        <p className="text-muted-foreground px-4 py-8 text-center text-sm">{empty}</p>
      ) : (
        children
      )}
    </section>
  );
}

const ADMISSION_TONES: Record<string, string> = {
  ADMITTED: "bg-info/15 text-info border-info/30",
  DISCHARGE_INITIATED: "bg-caution/15 text-caution-foreground border-caution/40",
  DISCHARGED: "bg-success/10 text-success border-success/25",
  CANCELLED: "bg-muted text-muted-foreground border-border",
};

const CHIP = "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap";

/** Whole nights between two instants — the same count the ward bills. */
function nights(from: string, to: string | null | undefined, now: number): number {
  const end = to ? new Date(to).getTime() : now;
  return Math.max(0, Math.round((end - new Date(from).getTime()) / 86_400_000));
}

/**
 * Every inpatient stay, with what the discharge summary said it was for and
 * what the patient went home on — the IPD half of "what were they treated
 * for", taken from the signed document rather than retyped.
 */
export function AdmissionsList({
  admissions,
  summaries,
  canReadSummary,
  now,
}: {
  admissions: AdmissionSummary[];
  summaries: Map<string, DischargeSummary>;
  canReadSummary: boolean;
  now: number;
}) {
  const t = useTranslations("record");
  const tWards = useTranslations("wards");
  const tSummary = useTranslations("summary");
  const format = useFormatter();
  const date = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium" });

  return (
    <RecordSection
      icon={<BedDouble aria-hidden className="size-5" />}
      title={t("tabAdmissions")}
      empty={t("noAdmissions")}
      isEmpty={admissions.length === 0}
      testId="admissions-list"
    >
      <ul className="divide-y">
        {admissions.map((admission) => {
          const summary = summaries.get(admission.id);
          return (
            <li key={admission.id} className="space-y-2 px-4 py-3" data-testid="admission-row">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="tabular text-sm font-semibold">{admission.admission_number}</span>
                <span className={cn(CHIP, ADMISSION_TONES[admission.status])}>
                  {tWards(`admission${admission.status}` as "admissionADMITTED")}
                </span>
                {admission.discharge_type ? (
                  <span className="text-muted-foreground text-xs">
                    {tWards(`discharge${admission.discharge_type}` as "dischargeRECOVERED")}
                  </span>
                ) : null}
                <LinkButton
                  variant="outline"
                  size="sm"
                  className="ml-auto"
                  href={`/admissions/${admission.id}`}
                >
                  {t("open")}
                </LinkButton>
              </div>
              <p className="text-muted-foreground text-sm">
                {t("admittedOn")} {date(admission.admitted_at)}
                {" → "}
                {admission.discharged_at ? date(admission.discharged_at) : t("stillAdmitted")}
                {" · "}
                {t("nights", { count: nights(admission.admitted_at, admission.discharged_at, now) })}
                {admission.ward_name ? ` · ${admission.ward_name}` : ""}
                {admission.bed_code ? ` / ${admission.bed_code}` : ""}
              </p>
              {summary ? (
                <dl className="bg-muted/40 grid gap-2 rounded-lg px-3 py-2 text-sm sm:grid-cols-2">
                  {summary.diagnoses ? (
                    <div>
                      <dt className="text-muted-foreground text-xs">{tSummary("diagnoses")}</dt>
                      <dd className="whitespace-pre-line">{summary.diagnoses}</dd>
                    </div>
                  ) : null}
                  {summary.discharge_medications ? (
                    <div>
                      <dt className="text-muted-foreground text-xs">{tSummary("dischargeMedications")}</dt>
                      <dd className="whitespace-pre-line">{summary.discharge_medications}</dd>
                    </div>
                  ) : null}
                  {canReadSummary ? (
                    <div className="sm:col-span-2">
                      <LinkButton
                        variant="link"
                        size="sm"
                        className="h-auto px-0"
                        href={`/admissions/${admission.id}/summary`}
                      >
                        <FileText aria-hidden className="size-4" />
                        {t("dischargeSummary")} · {tSummary(`status${summary.status}` as "statusDRAFT")}
                      </LinkButton>
                    </div>
                  ) : null}
                </dl>
              ) : null}
            </li>
          );
        })}
      </ul>
    </RecordSection>
  );
}

export function ReportsList({ reports }: { reports: ReportSummary[] }) {
  const t = useTranslations("record");
  const format = useFormatter();

  return (
    <RecordSection
      icon={<FlaskConical aria-hidden className="size-5" />}
      title={t("tabReports")}
      empty={t("noReports")}
      isEmpty={reports.length === 0}
      testId="reports-list"
    >
      <ul className="divide-y">
        {reports.map((report) => (
          <li key={report.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">
                {report.test_name}
                {report.has_critical_result ? (
                  <span className="text-critical ml-2 inline-flex items-center gap-1 text-xs font-semibold">
                    <AlertOctagon aria-hidden className="size-3.5" />
                    {t("criticalValue")}
                  </span>
                ) : null}
              </p>
              <p className="text-muted-foreground text-xs">
                {t(`discipline${report.discipline}` as "disciplineLAB")}
                {" · "}
                <span className="tabular">{report.report_number}</span>
                {" · "}
                {format.dateTime(new Date(report.created_at), { dateStyle: "medium" })}
              </p>
            </div>
            <span
              className={cn(
                CHIP,
                report.status === "FINAL" || report.status === "AMENDED"
                  ? "bg-success/10 text-success border-success/25"
                  : report.status === "CANCELLED"
                    ? "bg-muted text-muted-foreground border-border"
                    : "bg-caution/15 text-caution-foreground border-caution/40",
              )}
            >
              {t(`report${report.status}` as "reportFINAL")}
            </span>
            <LinkButton variant="outline" size="sm" href={`/lab/reports/${report.id}`}>
              {t("open")}
            </LinkButton>
          </li>
        ))}
      </ul>
    </RecordSection>
  );
}

export function InvoicesList({ invoices }: { invoices: InvoiceSummary[] }) {
  const t = useTranslations("record");
  const format = useFormatter();

  return (
    <RecordSection
      icon={<IndianRupee aria-hidden className="size-5" />}
      title={t("tabBilling")}
      hint={t("billingHint")}
      empty={t("noInvoices")}
      isEmpty={invoices.length === 0}
      testId="invoices-list"
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted-foreground text-xs">
            <tr className="border-b">
              <th className="px-4 py-2 text-left font-medium">{t("invoice")}</th>
              <th className="px-4 py-2 text-left font-medium">{t("status")}</th>
              <th className="px-4 py-2 text-right font-medium">{t("total")}</th>
              <th className="px-4 py-2 text-right font-medium">{t("paid")}</th>
              <th className="px-4 py-2 text-right font-medium">{t("balance")}</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y">
            {invoices.map((invoice) => (
              <tr key={invoice.id}>
                <td className="px-4 py-2.5">
                  <p className="tabular font-medium">{invoice.invoice_number}</p>
                  <p className="text-muted-foreground text-xs">
                    {format.dateTime(new Date(invoice.issued_at ?? invoice.created_at), {
                      dateStyle: "medium",
                    })}
                  </p>
                </td>
                <td className="px-4 py-2.5">
                  <InvoiceStatusChip status={invoice.status} />
                </td>
                <td className="tabular px-4 py-2.5 text-right">{formatMoney(invoice.grand_total)}</td>
                <td className="tabular px-4 py-2.5 text-right">{formatMoney(invoice.amount_paid)}</td>
                <td
                  className={cn(
                    "tabular px-4 py-2.5 text-right font-semibold",
                    isPositive(invoice.balance_due) && "text-critical",
                  )}
                >
                  {formatMoney(invoice.balance_due)}
                </td>
                <td className="px-4 py-2.5 text-right">
                  <LinkButton variant="outline" size="sm" href={`/billing/${invoice.encounter_id}`}>
                    {t("openAccount")}
                  </LinkButton>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </RecordSection>
  );
}
