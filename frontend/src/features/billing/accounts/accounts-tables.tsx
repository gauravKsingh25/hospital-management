"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { InvoiceStatusChip } from "@/components/status-chip";
import { LinkButton } from "@/components/ui/link-button";
import { formatMoney } from "@/features/billing/money";
import { cn } from "@/lib/utils";
import type { BillRow, PatientRow, PaymentRow } from "@/types/api";

/**
 * The dashboard's tables. Read only: every row opens, prints or leads to the
 * patient's bill history, and nothing here changes a figure.
 */
const TH = "px-3 py-2.5 text-left font-medium";
const TH_RIGHT = "px-3 py-2.5 text-right font-medium";
const TD = "px-3 py-2.5 align-top";

export const billHref = (invoiceId: string, print = false) =>
  `/bills/${invoiceId}${print ? "?print=1" : ""}`;
export const patientHref = (patientId: string) => `/billing/patients/${patientId}`;

function Frame({ caption, children }: { caption: string; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-3xl text-sm">
        <caption className="sr-only">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

function useWhen() {
  const format = useFormatter();
  return (value: string | null | undefined) =>
    value
      ? format.dateTime(new Date(value), {
          day: "2-digit",
          month: "short",
          hour: "numeric",
          minute: "2-digit",
        })
      : "—";
}

export function BillsTable({ rows, caption }: { rows: BillRow[]; caption: string }) {
  const t = useTranslations("accounts");
  const when = useWhen();
  return (
    <Frame caption={caption}>
      <thead className="bg-muted/50 text-muted-foreground">
        <tr>
          <th scope="col" className={TH}>{t("date")}</th>
          <th scope="col" className={TH}>{t("billNo")}</th>
          <th scope="col" className={TH}>{t("patient")}</th>
          <th scope="col" className={TH}>{t("visit")}</th>
          <th scope="col" className={TH_RIGHT}>{t("amount")}</th>
          <th scope="col" className={TH_RIGHT}>{t("paid")}</th>
          <th scope="col" className={TH}>{t("status")}</th>
          <th scope="col" className={TH_RIGHT}>{t("actions")}</th>
        </tr>
      </thead>
      <tbody className="divide-border divide-y">
        {rows.map((row) => (
          <tr key={row.invoice_id} data-testid="bill-row">
            <td className={cn(TD, "tabular whitespace-nowrap")}>{when(row.issued_at ?? row.created_at)}</td>
            <td className={cn(TD, "tabular text-xs")}>{row.invoice_number}</td>
            <td className={TD}>
              <Link href={patientHref(row.patient_id)} className="font-medium hover:underline">
                {row.patient_name ?? "—"}
              </Link>
              <div className="text-muted-foreground tabular text-xs">{row.uhid}</div>
            </td>
            <td className={TD}>
              {row.scope}
              {row.stay_days ? (
                <span className="text-muted-foreground"> · {t("days", { count: row.stay_days })}</span>
              ) : null}
            </td>
            <td className={cn(TD, "tabular text-right")}>{formatMoney(row.grand_total)}</td>
            <td className={cn(TD, "tabular text-right")}>{formatMoney(row.amount_paid)}</td>
            <td className={TD}>
              <InvoiceStatusChip status={row.status} />
            </td>
            <td className={cn(TD, "text-right")}>
              <div className="flex justify-end gap-1">
                <LinkButton
                  href={billHref(row.invoice_id)}
                  variant="ghost"
                  size="sm"
                  aria-label={t("openBill", { number: row.invoice_number })}
                >
                  {t("open")}
                </LinkButton>
                <LinkButton
                  href={billHref(row.invoice_id, true)}
                  variant="ghost"
                  size="sm"
                  aria-label={t("downloadBill", { number: row.invoice_number })}
                >
                  {t("pdf")}
                </LinkButton>
                <LinkButton
                  href={billHref(row.invoice_id, true)}
                  variant="ghost"
                  size="sm"
                  aria-label={t("printBill", { number: row.invoice_number })}
                >
                  {t("print")}
                </LinkButton>
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </Frame>
  );
}

export function PaymentsTable({ rows, caption }: { rows: PaymentRow[]; caption: string }) {
  const t = useTranslations("accounts");
  const billing = useTranslations("billing");
  const when = useWhen();
  return (
    <Frame caption={caption}>
      <thead className="bg-muted/50 text-muted-foreground">
        <tr>
          <th scope="col" className={TH}>{t("date")}</th>
          <th scope="col" className={TH}>{t("receiptNo")}</th>
          <th scope="col" className={TH}>{t("patient")}</th>
          <th scope="col" className={TH}>{t("billNo")}</th>
          <th scope="col" className={TH}>{t("method")}</th>
          <th scope="col" className={TH_RIGHT}>{t("amount")}</th>
          <th scope="col" className={TH}>{t("receivedBy")}</th>
        </tr>
      </thead>
      <tbody className="divide-border divide-y">
        {rows.map((row) => {
          // A reversed receipt stays on the list, struck through: the drawer
          // has to reconcile against what happened, not what was meant.
          const struck = row.status === "REVERSED";
          return (
            <tr key={row.id} data-testid="payment-row" className={cn(struck && "text-muted-foreground")}>
              <td className={cn(TD, "tabular whitespace-nowrap")}>{when(row.received_at)}</td>
              <td className={cn(TD, "tabular text-xs", struck && "line-through")}>
                {row.receipt_number}
                {row.reverses_payment_id ? (
                  <div className="text-critical text-xs no-underline">{t("reversal")}</div>
                ) : null}
              </td>
              <td className={TD}>
                <Link href={patientHref(row.patient_id)} className="font-medium hover:underline">
                  {row.patient_name ?? "—"}
                </Link>
                <div className="text-muted-foreground tabular text-xs">{row.uhid}</div>
              </td>
              <td className={cn(TD, "tabular text-xs")}>
                {row.invoice_number ? (
                  <Link href={billHref(row.invoice_id)} className="hover:underline">
                    {row.invoice_number}
                  </Link>
                ) : (
                  "—"
                )}
              </td>
              <td className={TD}>
                {billing(`method${row.method}` as "methodUPI")}
                {row.reference ? (
                  <div className="text-muted-foreground tabular text-xs">{row.reference}</div>
                ) : null}
              </td>
              <td className={cn(TD, "tabular text-right", struck && "line-through")}>
                {formatMoney(row.amount)}
              </td>
              <td className={TD}>{row.received_by_name ?? "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </Frame>
  );
}

export function PatientsTable({ rows, caption }: { rows: PatientRow[]; caption: string }) {
  const t = useTranslations("accounts");
  const when = useWhen();
  return (
    <Frame caption={caption}>
      <thead className="bg-muted/50 text-muted-foreground">
        <tr>
          <th scope="col" className={TH}>{t("patient")}</th>
          <th scope="col" className={TH}>{t("lastActivity")}</th>
          <th scope="col" className={TH_RIGHT}>{t("visits")}</th>
          <th scope="col" className={TH_RIGHT}>{t("totalCost")}</th>
          <th scope="col" className={TH_RIGHT}>{t("paid")}</th>
          <th scope="col" className={TH_RIGHT}>{t("due")}</th>
        </tr>
      </thead>
      <tbody className="divide-border divide-y">
        {rows.map((row) => (
          <tr key={row.patient_id} data-testid="patient-row">
            <td className={TD}>
              <Link href={patientHref(row.patient_id)} className="font-medium hover:underline">
                {row.name ?? "—"}
              </Link>
              <div className="text-muted-foreground tabular text-xs">{row.uhid}</div>
            </td>
            <td className={cn(TD, "tabular whitespace-nowrap")}>{when(row.last_activity_at)}</td>
            <td className={cn(TD, "tabular text-right")}>{row.visits}</td>
            <td className={cn(TD, "tabular text-right")}>{formatMoney(row.billed)}</td>
            <td className={cn(TD, "tabular text-right")}>{formatMoney(row.paid)}</td>
            <td className={cn(TD, "tabular text-right font-medium")}>{formatMoney(row.due)}</td>
          </tr>
        ))}
      </tbody>
    </Frame>
  );
}
