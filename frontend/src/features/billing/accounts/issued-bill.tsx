"use client";

import { useQuery } from "@tanstack/react-query";
import { Pencil, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useReducer, useState } from "react";
import { toast } from "sonner";

import { InvoiceStatusChip } from "@/components/status-chip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { CORRECTABLE, PrintActions } from "@/features/billing/accounts/bill-actions";
import {
  ChargeDialog,
  Lines,
  ReasonDialog,
  StagedPanel,
  staging,
  type DialogState,
} from "@/features/billing/accounts/ipd-bill-editor";
import { useCorrectBill } from "@/features/billing/accounts/use-ipd-bill";
import { formatMoney } from "@/features/billing/money";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { InvoiceSummary, PrintableBill } from "@/types/api";

/**
 * One issued bill, as a locked form: its lines, receipts and balance.
 *
 * Edit (the accounts office only) unlocks the lines. Changes are staged as in
 * the IPD editor, and "Issue corrected bill" asks for a reason, then issues a
 * new bill under a new number with the receipts moved onto it; this one is
 * cancelled. The bill itself never changes in place: it is in the GST series
 * and the patient holds a copy.
 */
export function IssuedBill({
  invoice,
  expanded,
  canCorrect,
}: {
  invoice: InvoiceSummary;
  expanded: boolean;
  canCorrect: boolean;
}) {
  const t = useTranslations("ipdBill");
  const history = useTranslations("history");
  const [open, setOpen] = useState(expanded);
  const [editing, setEditing] = useState(false);
  const [staged, dispatch] = useReducer(staging, []);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [asking, setAsking] = useState(false);
  const correct = useCorrectBill();

  const printable = useQuery({
    queryKey: ["billing", "printable", invoice.id],
    queryFn: ({ signal }) =>
      api.get<PrintableBill>(`/billing/invoices/${invoice.id}/printable`, { signal }),
    enabled: open,
  });

  const correctable = canCorrect && CORRECTABLE.has(invoice.status);
  const stopEditing = () => {
    dispatch({ type: "clear" });
    setEditing(false);
  };
  const issue = (reason: string) =>
    correct.mutate(
      { invoiceId: invoice.id, reason, ops: staged.map((item) => item.op) },
      {
        onSuccess: (fresh) => {
          setAsking(false);
          stopEditing();
          toast.success(t("corrected", { number: fresh.invoice_number, old: invoice.invoice_number }));
        },
        onError: (failure) => toast.error(isApiError(failure) ? failure.message : String(failure)),
      },
    );

  return (
    <div className="rounded-lg border" data-testid="issued-bill">
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
        <div className="flex flex-wrap gap-2">
          {correctable && !editing ? (
            <Button
              variant="outline"
              size="sm"
              className="h-tap"
              aria-label={t("editBillNumber", { number: invoice.invoice_number })}
              onClick={() => {
                setOpen(true);
                setEditing(true);
              }}
            >
              <Pencil aria-hidden />
              {t("editBill")}
            </Button>
          ) : null}
          <PrintActions invoiceId={invoice.id} number={invoice.invoice_number} />
        </div>
      </div>

      {open ? (
        <div className="space-y-3 border-t p-3 text-sm">
          {editing ? (
            <div className="bg-primary/5 flex flex-wrap items-center justify-between gap-2 rounded-md p-2">
              <p>{t("correctingNote", { number: invoice.invoice_number })}</p>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" className="h-tap" onClick={() => setDialog({ mode: "add" })}>
                  <Plus aria-hidden />
                  {t("addCharge")}
                </Button>
                <Button variant="ghost" size="sm" className="h-tap" onClick={stopEditing}>
                  {t("stopEditing")}
                </Button>
              </div>
            </div>
          ) : null}

          {printable.data ? (
            <>
              <Lines
                charges={printable.data.lines}
                editable={editing}
                stagedFor={(chargeId) =>
                  staged.filter((item) => "charge_id" in item.op && item.op.charge_id === chargeId)
                }
                onAction={(mode, charge) => setDialog({ mode, charge })}
              />
              {printable.data.payments.length ? (
                <ul className="text-muted-foreground space-y-0.5 text-xs">
                  {printable.data.payments.map((payment) => (
                    <li key={payment.id}>
                      {history("receipt", {
                        number: payment.receipt_number,
                        amount: formatMoney(payment.amount),
                        method: payment.method,
                      })}
                    </li>
                  ))}
                </ul>
              ) : null}
              <p className="text-right font-medium">
                {history("balanceDue", { amount: formatMoney(printable.data.totals.balance) })}
              </p>
            </>
          ) : (
            <Skeleton className="h-16 w-full" />
          )}

          {staged.length ? (
            <StagedPanel
              staged={staged}
              onUndo={(key) => dispatch({ type: "unstage", key })}
              onDiscard={() => dispatch({ type: "clear" })}
              onSubmit={() => setAsking(true)}
              submitLabel={t("issueCorrected")}
              pending={correct.isPending}
            />
          ) : null}
        </div>
      ) : null}

      {dialog ? (
        <ChargeDialog
          state={dialog}
          onClose={() => setDialog(null)}
          onStage={(op, label) => {
            dispatch({ type: "stage", op, label });
            setDialog(null);
          }}
        />
      ) : null}
      <ReasonDialog
        open={asking}
        onOpenChange={setAsking}
        title={t("issueCorrected")}
        hint={t("correctHint", { number: invoice.invoice_number })}
        label={t("correctionReason")}
        confirmLabel={t("issueCorrected")}
        keepLabel={t("keepEditing")}
        pending={correct.isPending}
        onConfirm={issue}
      />
    </div>
  );
}
