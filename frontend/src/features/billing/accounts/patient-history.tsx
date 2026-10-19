"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { InvoiceStatusChip } from "@/components/status-chip";
import { LinkButton } from "@/components/ui/link-button";
import { Skeleton } from "@/components/ui/skeleton";
import { billHref } from "@/features/billing/accounts/accounts-tables";
import { CostJourney } from "@/features/billing/accounts/cost-journey";
import { IpdBillEditor } from "@/features/billing/accounts/ipd-bill-editor";
import { formatMoney } from "@/features/billing/money";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type { BillHistory, InvoiceSummary, PrintableBill, VisitBills } from "@/types/api";

/**
 * A patient's bills (spec §5.3): identity and lifetime totals on top, every
 * bill-bearing visit newest first on the left — the newest open — and the
 * selected visit's bill on the right. A stay that is not final yet opens in
 * the IPD bill editor; everything else is read only. The cost journey is one
 * tab away.
 */
type View = "bills" | "journey";

export function PatientHistory({
  initial,
  canEdit,
  canFinalise,
}: {
  initial: BillHistory;
  canEdit: boolean;
  canFinalise: boolean;
}) {
  const t = useTranslations("history");
  const [view, setView] = useState<View>("bills");
  const [selected, setSelected] = useState(0);

  const history = useQuery({
    queryKey: ["billing", "history", initial.patient.id],
    queryFn: ({ signal }) =>
      api.get<BillHistory>(`/billing/patients/${initial.patient.id}/history`, { signal }),
    initialData: initial,
  });
  const data = history.data;
  const patient = data.patient;
  const visit = data.visits[selected] ?? data.visits[0];

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{patient.name}</h1>
          <p className="text-muted-foreground tabular text-sm">
            {[patient.uhid, patient.age_years !== null ? `${patient.age_years} y` : null, patient.gender, patient.phone]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
        <dl className="grid grid-cols-3 gap-4 text-right" data-testid="lifetime">
          <div>
            <dt className="text-muted-foreground text-xs uppercase">{t("billed")}</dt>
            <dd className="tabular font-semibold">{formatMoney(data.lifetime_billed)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs uppercase">{t("paid")}</dt>
            <dd className="tabular font-semibold">{formatMoney(data.lifetime_paid)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs uppercase">{t("due")}</dt>
            <dd className="tabular text-caution-foreground font-semibold">
              {formatMoney(data.lifetime_due)}
            </dd>
          </div>
        </dl>
      </div>

      <div role="tablist" aria-label={t("views")} className="flex gap-1 border-b">
        {(["bills", "journey"] as View[]).map((option) => (
          <button
            key={option}
            type="button"
            role="tab"
            aria-selected={view === option}
            onClick={() => setView(option)}
            className={cn(
              "h-tap -mb-px border-b-2 px-4 text-sm font-medium",
              view === option
                ? "border-primary text-foreground"
                : "text-muted-foreground hover:text-foreground border-transparent",
            )}
          >
            {t(option === "bills" ? "tabBills" : "tabJourney")}
          </button>
        ))}
      </div>

      {view === "journey" ? (
        <CostJourney patientId={patient.id} />
      ) : data.visits.length === 0 ? (
        <p className="text-muted-foreground rounded-lg border border-dashed py-12 text-center text-sm">
          {t("noVisits")}
        </p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
          <ol className="space-y-2" aria-label={t("visits")}>
            {data.visits.map((item, index) => (
              <li key={`${item.encounter_id}-${item.kind}`}>
                <VisitItem
                  visit={item}
                  current={visit === item}
                  onSelect={() => setSelected(index)}
                />
              </li>
            ))}
          </ol>
          <section className="min-w-0 rounded-lg border p-4" data-testid="visit-pane" aria-live="polite">
            {visit ? (
              <VisitPane
                key={`${visit.encounter_id}-${visit.kind}`}
                visit={visit}
                canEdit={canEdit}
                canFinalise={canFinalise}
              />
            ) : null}
          </section>
        </div>
      )}
    </div>
  );
}

function useDay() {
  const format = useFormatter();
  return (value: string | null | undefined) =>
    value ? format.dateTime(new Date(value), { day: "2-digit", month: "short", year: "numeric" }) : "";
}

function VisitItem({
  visit,
  current,
  onSelect,
}: {
  visit: VisitBills;
  current: boolean;
  onSelect: () => void;
}) {
  const t = useTranslations("history");
  const day = useDay();
  const dates =
    visit.kind === "IPD"
      ? `${day(visit.started_at)} – ${visit.ended_at ? day(visit.ended_at) : t("now")}`
      : day(visit.started_at);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={current ? "true" : undefined}
      data-testid="visit-item"
      className={cn(
        "w-full rounded-lg border p-3 text-left text-sm",
        current ? "border-primary bg-primary/5 border-2" : "hover:bg-muted/50",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold">
          {visit.kind === "IPD" ? t("ipdAdmission") : t("opdVisit")}
        </span>
        <span className="text-muted-foreground text-xs">{t(`label_${visit.status_label.replaceAll(" ", "_")}` as "label_Paid")}</span>
      </div>
      <div className="text-muted-foreground text-xs">{dates}</div>
      <div className="text-muted-foreground text-xs">
        {[visit.admission_number, visit.ward_bed, visit.doctor_name].filter(Boolean).join(" · ")}
      </div>
      <div className="tabular mt-1 font-medium">{formatMoney(visit.total)}</div>
    </button>
  );
}

function VisitPane({
  visit,
  canEdit,
  canFinalise,
}: {
  visit: VisitBills;
  canEdit: boolean;
  canFinalise: boolean;
}) {
  const t = useTranslations("history");
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">
          {visit.kind === "IPD"
            ? `${t("ipdAdmission")} · ${visit.admission_number ?? ""}`
            : t("opdVisit")}
        </h2>
        <p className="text-muted-foreground text-sm">
          {t("visitTotals", {
            total: formatMoney(visit.total),
            paid: formatMoney(visit.paid),
            due: formatMoney(visit.due),
          })}
        </p>
      </div>
      {visit.kind === "IPD" ? (
        <IpdBillEditor encounterId={visit.encounter_id} canEdit={canEdit} canFinalise={canFinalise} />
      ) : visit.invoices.length === 0 ? (
        <p className="text-muted-foreground text-sm">{t("notBilledYet")}</p>
      ) : (
        visit.invoices.map((invoice, index) => (
          <InvoiceCard key={invoice.id} invoice={invoice} expanded={index === 0} />
        ))
      )}
    </div>
  );
}

/** One issued bill, read only: its lines by category, its receipts, its balance. */
function InvoiceCard({ invoice, expanded }: { invoice: InvoiceSummary; expanded: boolean }) {
  const t = useTranslations("history");
  const accounts = useTranslations("accounts");
  const categories = useTranslations("categories");
  const [open, setOpen] = useState(expanded);
  const printable = useQuery({
    queryKey: ["billing", "printable", invoice.id],
    queryFn: ({ signal }) =>
      api.get<PrintableBill>(`/billing/invoices/${invoice.id}/printable`, { signal }),
    enabled: open,
  });

  return (
    <div className="rounded-lg border">
      <div className="flex flex-wrap items-center justify-between gap-2 p-3">
        <button
          type="button"
          className="flex items-center gap-2 text-left"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <span className="tabular font-medium">{invoice.invoice_number}</span>
          <InvoiceStatusChip status={invoice.status} />
          <span className="tabular text-sm">{formatMoney(invoice.grand_total)}</span>
        </button>
        <div className="flex gap-1">
          <LinkButton href={billHref(invoice.id)} variant="ghost" size="sm">
            {accounts("open")}
          </LinkButton>
          <LinkButton
            href={billHref(invoice.id, true)}
            variant="ghost"
            size="sm"
            aria-label={accounts("printBill", { number: invoice.invoice_number })}
          >
            {accounts("print")}
          </LinkButton>
        </div>
      </div>
      {open ? (
        <div className="border-t p-3">
          {printable.data ? (
            <div className="space-y-3 text-sm">
              <table className="w-full">
                <caption className="sr-only">{invoice.invoice_number}</caption>
                <tbody className="divide-border divide-y">
                  {printable.data.lines.map((line) => (
                    <tr key={line.id}>
                      <td className="py-1.5">
                        <div className={cn(line.status === "WAIVED" && "line-through")}>
                          {line.description}
                        </div>
                        <div className="text-muted-foreground text-xs">{categories(line.category)}</div>
                      </td>
                      <td className="tabular py-1.5 text-right">{Number(line.quantity)}</td>
                      <td className="tabular py-1.5 text-right">{formatMoney(line.total_amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {printable.data.payments.length ? (
                <ul className="text-muted-foreground space-y-0.5 text-xs">
                  {printable.data.payments.map((payment) => (
                    <li key={payment.id}>
                      {t("receipt", {
                        number: payment.receipt_number,
                        amount: formatMoney(payment.amount),
                        method: payment.method,
                      })}
                    </li>
                  ))}
                </ul>
              ) : null}
              <p className="text-right font-medium">
                {t("balanceDue", { amount: formatMoney(printable.data.totals.balance) })}
              </p>
            </div>
          ) : (
            <Skeleton className="h-16 w-full" />
          )}
        </div>
      ) : null}
    </div>
  );
}
