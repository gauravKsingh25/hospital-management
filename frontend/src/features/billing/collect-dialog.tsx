"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Banknote, CreditCard, QrCode, TriangleAlert } from "lucide-react";

import { QrSvg } from "@/components/qr-svg";
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
import { LinkButton } from "@/components/ui/link-button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type { CollectPayment, QueueBoardEntry, VisitAccount } from "@/types/api";

import { formatMoney, isPositive } from "@/features/billing/money";

/** The three ways money arrives at an OPD counter; the rest are on the billing screen. */
const METHODS = [
  { method: "UPI", icon: QrCode },
  { method: "CARD", icon: CreditCard },
  { method: "CASH", icon: Banknote },
] as const;

type Method = (typeof METHODS)[number]["method"];

/**
 * A UPI deep link with the amount filled in — what every UPI app reads from a
 * QR. Encoded by hand: `URLSearchParams` writes spaces as `+`, which several
 * apps then show to the patient as a literal plus in the payee's name.
 */
function upiLink({
  vpa,
  payee,
  amount,
  note,
}: {
  vpa: string;
  payee: string;
  amount: string;
  note: string;
}): string {
  const fields: [string, string][] = [
    ["pa", vpa],
    ["pn", payee],
    ["am", amount],
    ["cu", "INR"],
    ["tn", note],
  ];
  return `upi://pay?${fields.map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join("&")}`;
}

/**
 * "Get payment": what the patient booked, and the money taken for it.
 *
 * Opens on UPI, with a QR for the exact balance on the hospital's own UPI ID.
 * There is no gateway behind it, so nothing tells the system the money
 * arrived: the receptionist looks at the patient's phone and types the UTR,
 * which is also what reconciles the receipt against the bank statement.
 *
 * The amount is never typed. The QR, the button and the request all use the
 * balance the server sent, and the request carries it back so a charge that
 * lands mid-payment is refused rather than silently collected.
 *
 * An unpriced line blocks collection here and points to the billing screen —
 * issuing a bill with a zero on it is the mistake this path must not make.
 */
export function CollectDialog({
  entry,
  encounterId,
  open,
  onOpenChange,
  pending,
  onConfirm,
}: {
  entry: QueueBoardEntry;
  encounterId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pending: boolean;
  onConfirm: (body: CollectPayment) => void;
}) {
  const t = useTranslations("billing");
  const tQueue = useTranslations("queue");
  const common = useTranslations("common");
  const [method, setMethod] = useState<Method>("UPI");
  const [reference, setReference] = useState("");

  // The visit account screen's key: whichever of the two is open, the other
  // is fresh when it opens.
  const account = useQuery({
    queryKey: ["billing", "account", encounterId],
    queryFn: ({ signal }) => api.get<VisitAccount>(`/billing/accounts/${encounterId}`, { signal }),
    staleTime: 0,
  });

  const data = account.data;
  const due = data?.balance_due ?? null;
  const needsReference = method !== "CASH";
  const canConfirm =
    data !== undefined &&
    isPositive(due) &&
    !data.has_unpriced_items &&
    (!needsReference || reference.trim().length > 0);

  const invoiced = (data?.invoices ?? []).filter(
    (invoice) =>
      isPositive(invoice.balance_due) &&
      invoice.status !== "CANCELLED" &&
      invoice.status !== "WRITTEN_OFF",
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92dvh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("getPayment")}</DialogTitle>
          <DialogDescription>
            {entry.patient_name ?? common("notRecorded")} · {tQueue("token")} {entry.token_number}
          </DialogDescription>
        </DialogHeader>

        {data === undefined ? (
          <div className="space-y-2" aria-busy="true">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-48 w-full" />
          </div>
        ) : (
          <div className="space-y-4 text-left">
            {/* What was booked — the question the patient asks first. */}
            <section aria-labelledby="collect-items">
              <h3 id="collect-items" className="sr-only">
                {t("booked")}
              </h3>
              <ul className="divide-border divide-y rounded-md border text-sm">
                {data.pending_charges.map((charge) => (
                  <li key={charge.id} className="flex items-baseline justify-between gap-3 px-3 py-2">
                    <span className="min-w-0">
                      {charge.description}
                      {charge.needs_pricing ? (
                        <span className="text-caution-foreground ml-2 text-xs font-medium">
                          {t("noPrice")}
                        </span>
                      ) : null}
                    </span>
                    <span className="tabular shrink-0">{formatMoney(charge.total_amount)}</span>
                  </li>
                ))}
                {invoiced.map((invoice) => (
                  <li key={invoice.id} className="flex items-baseline justify-between gap-3 px-3 py-2">
                    <span className="min-w-0">
                      {t("invoiceLine", { number: invoice.invoice_number })}
                    </span>
                    <span className="tabular shrink-0">{formatMoney(invoice.balance_due)}</span>
                  </li>
                ))}
                <li className="bg-muted/40 flex items-baseline justify-between gap-3 px-3 py-2.5">
                  <span className="font-medium">{t("toPay")}</span>
                  <span className="tabular text-xl font-semibold" data-testid="collect-total">
                    {formatMoney(due)}
                  </span>
                </li>
              </ul>
            </section>

            {data.has_unpriced_items ? (
              <div className="bg-caution/15 text-caution-foreground flex items-start gap-2 rounded-md p-3 text-sm">
                <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
                <div className="space-y-2">
                  <p>{t("unpricedBlock")}</p>
                  <LinkButton variant="outline" className="h-tap" href={`/billing/${encounterId}`}>
                    {t("openAccount")}
                  </LinkButton>
                </div>
              </div>
            ) : (
              <>
                <div role="radiogroup" aria-label={t("method")} className="grid grid-cols-3 gap-2">
                  {METHODS.map(({ method: option, icon: Icon }) => (
                    <Button
                      key={option}
                      role="radio"
                      aria-checked={method === option}
                      variant={method === option ? "default" : "outline"}
                      className="h-tap"
                      onClick={() => setMethod(option)}
                      data-testid={`method-${option}`}
                    >
                      <Icon aria-hidden className="size-4" />
                      {t(`method${option}`)}
                    </Button>
                  ))}
                </div>

                {method === "UPI" ? (
                  data.upi_vpa && due ? (
                    <div className="flex flex-col items-center gap-2 rounded-md border p-3">
                      <QrSvg
                        value={upiLink({
                          vpa: data.upi_vpa,
                          payee: data.payee_name ?? data.upi_vpa,
                          amount: due,
                          note: data.encounter_number,
                        })}
                        size="13rem"
                        label={t("qrLabel", { amount: formatMoney(due), vpa: data.upi_vpa })}
                      />
                      <p className="text-muted-foreground text-center text-xs">
                        {t("scanToPay")}
                        <br />
                        <span className="text-foreground font-medium">{data.upi_vpa}</span>
                      </p>
                    </div>
                  ) : (
                    <p className="bg-muted text-muted-foreground rounded-md p-3 text-sm">
                      {t("noUpiId")}
                    </p>
                  )
                ) : null}

                {needsReference ? (
                  <div className="space-y-1.5">
                    <Label htmlFor="collect-reference">
                      {method === "UPI" ? t("utr") : t("cardApproval")}
                    </Label>
                    <Input
                      id="collect-reference"
                      value={reference}
                      onChange={(event) => setReference(event.target.value)}
                      inputMode={method === "UPI" ? "numeric" : "text"}
                      autoComplete="off"
                      className="h-tap tabular text-lg"
                    />
                    <p className="text-muted-foreground text-xs">{t("referenceHint")}</p>
                  </div>
                ) : null}
              </>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className={cn("h-tap", canConfirm && "bg-success text-success-foreground hover:bg-success/90")}
            disabled={!canConfirm || pending}
            onClick={() =>
              onConfirm({
                method,
                reference: needsReference ? reference.trim() : null,
                expected_amount: due,
              })
            }
            data-testid="confirm-received"
          >
            {t("confirmReceived", { amount: formatMoney(due) })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
