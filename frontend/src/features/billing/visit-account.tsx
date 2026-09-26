"use client";

import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";

import { EncounterStatusChip, InvoiceStatusChip } from "@/components/status-chip";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { EncounterStatus, Invoice, InvoiceSummary, VisitAccount } from "@/types/api";

import { formatMoney, isPositive } from "@/features/billing/money";
import { PaymentDialog } from "@/features/billing/payment-dialog";

/**
 * One visit's account: what is owed, what has been invoiced, what was paid.
 *
 * The cashier never builds a bill here. Charges arrive on their own from
 * consultation, lab, radiology and pharmacy through the event bus — CLAUDE.md
 * §7b is explicit that reception *reviews* the invoice and never rebuilds it —
 * so the only actions on this screen are assemble, issue and take payment.
 *
 * Taking the last rupee is what closes the visit. That happens on the server
 * (`reevaluate_closure`), not here: this screen has no idea what else might
 * still be outstanding, and a second opinion about whether a visit is finished
 * is how one gets closed with a lab result still pending.
 */
export function VisitAccountScreen({
  encounterId,
  initial,
  canCreateInvoice,
  canIssueInvoice,
  canRecordPayment,
}: {
  encounterId: string;
  initial: VisitAccount;
  canCreateInvoice: boolean;
  canIssueInvoice: boolean;
  canRecordPayment: boolean;
}) {
  const t = useTranslations("billing");
  const router = useRouter();
  const [paying, setPaying] = useState<InvoiceSummary | null>(null);

  const account = useQuery({
    queryKey: ["billing", "account", encounterId],
    queryFn: ({ signal }) =>
      api.get<VisitAccount>(`/billing/accounts/${encounterId}`, { signal }),
    initialData: initial,
    staleTime: 0,
  });

  const data = account.data;
  const status = data.encounter_status as EncounterStatus;
  const deceased = status === "DECEASED";

  const assemble = useMutation({
    mutationFn: () =>
      api.post<Invoice>("/billing/invoices", { encounter_id: encounterId }),
    onSuccess: () => {
      toast.success(t("draftAssembled"));
      void account.refetch();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const issue = useMutation({
    mutationFn: (invoiceId: string) =>
      api.post<Invoice>(`/billing/invoices/${invoiceId}/issue`, {}),
    onSuccess: () => {
      toast.success(t("invoiceIssued"));
      void account.refetch();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const drafts = data.invoices.filter((invoice) => invoice.status === "DRAFT");
  const payable = data.invoices.filter(
    (invoice) => invoice.status === "ISSUED" || invoice.status === "PARTIALLY_PAID",
  );

  return (
    <div className="space-y-6">
      <header
        className={cn(
          "rounded-lg border p-4",
          deceased ? "border-foreground/40 bg-foreground/5" : "bg-muted/30",
        )}
      >
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">{data.patient_name}</h1>
            <p className="text-muted-foreground tabular text-sm">
              {data.uhid} · {data.encounter_number}
            </p>
          </div>
          <div className="text-right">
            <div className="tabular text-2xl font-semibold">
              {formatMoney(data.balance_due)}
            </div>
            <div className="text-muted-foreground text-xs">{t("balanceDue")}</div>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <EncounterStatusChip status={status} />
          {data.has_unpriced_items ? (
            <span className="bg-caution/20 text-caution-foreground rounded px-2 py-0.5 text-xs font-medium">
              {t("unpricedWarning")}
            </span>
          ) : null}
        </div>

        {/*
          A deceased patient's bill is still owed, and CLAUDE.md §6 requires the
          settlement flow to run. What must not happen is a screen instructing
          staff to collect from the patient. The amount stays; the framing
          changes.
        */}
        {deceased ? (
          <p className="text-foreground mt-3 text-sm font-medium">{t("deceasedSettlement")}</p>
        ) : null}
      </header>

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="charges-heading">
        <h2 id="charges-heading" className="text-sm font-semibold">
          {t("pendingCharges")}
        </h2>

        {data.pending_charges.length === 0 ? (
          <p className="text-muted-foreground text-sm">{t("noPendingCharges")}</p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">{t("pendingCharges")}</caption>
                <thead className="text-muted-foreground">
                  <tr>
                    <th scope="col" className="py-2 text-left font-medium">
                      {t("item")}
                    </th>
                    <th scope="col" className="py-2 text-right font-medium">
                      {t("amount")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-border divide-y">
                  {data.pending_charges.map((charge) => (
                    <tr key={charge.id}>
                      <td className="py-2">
                        {charge.description}
                        {charge.needs_pricing ? (
                          <span className="text-caution-foreground ml-2 text-xs font-medium">
                            {t("noPrice")}
                          </span>
                        ) : null}
                      </td>
                      <td className="tabular py-2 text-right">
                        {formatMoney(charge.total_amount)}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t font-medium">
                    <td className="py-2">{t("pendingTotal")}</td>
                    <td className="tabular py-2 text-right">
                      {formatMoney(data.pending_total)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>

            {canCreateInvoice ? (
              <Button
                className="h-tap"
                disabled={assemble.isPending}
                onClick={() => assemble.mutate()}
              >
                {t("assembleInvoice")}
              </Button>
            ) : null}
          </>
        )}
      </section>

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="invoices-heading">
        <h2 id="invoices-heading" className="text-sm font-semibold">
          {t("invoices")}
        </h2>

        {data.invoices.length === 0 ? (
          <p className="text-muted-foreground text-sm">{t("noInvoices")}</p>
        ) : (
          <ul className="divide-border divide-y">
            {data.invoices.map((invoice) => (
              <li
                key={invoice.id}
                className="flex flex-wrap items-center justify-between gap-3 py-3"
              >
                <div>
                  <div className="tabular text-sm font-medium">{invoice.invoice_number}</div>
                  <div className="mt-1">
                    <InvoiceStatusChip status={invoice.status} />
                  </div>
                </div>

                <div className="text-right">
                  <div className="tabular font-semibold">{formatMoney(invoice.grand_total)}</div>
                  {isPositive(invoice.balance_due) ? (
                    <div className="text-muted-foreground tabular text-xs">
                      {t("outstanding", { amount: formatMoney(invoice.balance_due) })}
                    </div>
                  ) : null}
                </div>

                <div className="flex gap-2">
                  {invoice.status === "DRAFT" && canIssueInvoice ? (
                    <Button
                      size="sm"
                      className="h-tap"
                      disabled={issue.isPending}
                      onClick={() => issue.mutate(invoice.id)}
                    >
                      {t("issueInvoice")}
                    </Button>
                  ) : null}

                  {canRecordPayment &&
                  isPositive(invoice.balance_due) &&
                  invoice.status !== "DRAFT" ? (
                    <Button size="sm" className="h-tap" onClick={() => setPaying(invoice)}>
                      {t("takePayment")}
                    </Button>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        )}

        {drafts.length > 0 && payable.length === 0 && !canIssueInvoice ? (
          <p className="text-muted-foreground text-sm">{t("needsIssuing")}</p>
        ) : null}
      </section>

      {paying ? (
        <PaymentDialog
          invoice={paying}
          open
          onOpenChange={(open) => {
            if (!open) setPaying(null);
          }}
          patientIsDeceased={deceased}
          onPaid={() => {
            setPaying(null);
            void account.refetch();
            // The visit may have just closed itself, and the counter's board
            // is the screen that has to notice.
            router.refresh();
          }}
        />
      ) : null}
    </div>
  );
}
