"use client";

import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { InvoiceSummary, Payment, PaymentMethod } from "@/types/api";

import { formatMoney } from "@/features/billing/money";

/**
 * Taking money.
 *
 * UPI is first and is the default, because CLAUDE.md §9 says so and because it
 * is how most counters in an Indian hospital are actually paid. Cash is second
 * for the people who still bring notes.
 *
 * The reference field appears for every method except cash, and the backend
 * *requires* it there: a UPI or card payment with no transaction id cannot be
 * matched against the settlement file the next morning, and an unreconcilable
 * receipt is how a hospital discovers in a quiet month that it is short.
 *
 * The amount is prefilled with the full balance and stays editable — part
 * payments are ordinary, and typing the common case is wasted keystrokes at a
 * counter with a queue behind it.
 */
const METHODS: PaymentMethod[] = ["UPI", "CASH", "CARD", "NETBANKING", "WALLET", "CHEQUE"];

/** Cash is the only method the backend accepts without a reference. */
const NEEDS_REFERENCE = new Set<PaymentMethod>([
  "UPI",
  "CARD",
  "NETBANKING",
  "WALLET",
  "CHEQUE",
]);

export function PaymentDialog({
  invoice,
  open,
  onOpenChange,
  patientIsDeceased,
  onPaid,
}: {
  invoice: InvoiceSummary;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  patientIsDeceased: boolean;
  onPaid: () => void;
}) {
  const t = useTranslations("billing");
  const common = useTranslations("common");

  const [amount, setAmount] = useState(invoice.balance_due);
  const [method, setMethod] = useState<PaymentMethod>("UPI");
  const [reference, setReference] = useState("");
  const [payerName, setPayerName] = useState("");

  const referenceRequired = NEEDS_REFERENCE.has(method);

  const record = useMutation({
    mutationFn: () =>
      api.post<Payment>(`/billing/invoices/${invoice.id}/payments`, {
        amount,
        method,
        reference: reference.trim() || null,
        payer_name: payerName.trim() || null,
      }),
    onSuccess: (payment) => {
      toast.success(t("paymentRecorded", { receipt: payment.receipt_number }));
      onPaid();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const valid =
    Number(amount) > 0 &&
    Number(amount) <= Number(invoice.balance_due) &&
    (!referenceRequired || reference.trim().length > 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("takePayment")}</DialogTitle>
          <DialogDescription>
            {invoice.invoice_number} · {t("outstanding", {
              amount: formatMoney(invoice.balance_due),
            })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="space-y-1.5">
            <Label htmlFor="payment-amount">{t("amount")}</Label>
            <Input
              id="payment-amount"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              inputMode="decimal"
              className="h-tap tabular text-lg"
              autoFocus
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="payment-method">{t("method")}</Label>
            <Select
              value={method}
              onValueChange={(value) => setMethod((value ?? "UPI") as PaymentMethod)}
            >
              <SelectTrigger id="payment-method" className="h-tap w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {METHODS.map((option) => (
                  <SelectItem key={option} value={option}>
                    {t(`method${option}` as "methodUPI")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {referenceRequired ? (
            <div className="space-y-1.5">
              <Label htmlFor="payment-reference">{t("reference")}</Label>
              <Input
                id="payment-reference"
                value={reference}
                onChange={(event) => setReference(event.target.value)}
                className="h-tap"
                placeholder={t("referencePlaceholder")}
              />
              <p className="text-muted-foreground text-xs">{t("referenceHint")}</p>
            </div>
          ) : null}

          {/*
            Who handed the money over. Optional in general and genuinely useful
            when the patient did not: a relative settling a bill after a death
            or a discharge against advice.
          */}
          {patientIsDeceased ? (
            <div className="space-y-1.5">
              <Label htmlFor="payer-name">{t("payerName")}</Label>
              <Input
                id="payer-name"
                value={payerName}
                onChange={(event) => setPayerName(event.target.value)}
                className="h-tap"
                placeholder={t("payerNamePlaceholder")}
              />
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button className="h-tap" disabled={!valid || record.isPending} onClick={() => record.mutate()}>
            {t("recordPayment")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
