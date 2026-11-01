"use client";

import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { Banknote, CircleCheck, QrCode } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { CollectPayment, QueueBoardEntry } from "@/types/api";

import { formatMoney, isPositive } from "@/features/billing/money";
import { useCollect } from "@/features/billing/use-collect";

// The dialog carries the QR encoder. Loaded on the first "Get payment" tap,
// so the reception board's own bundle does not pay for it.
const CollectDialog = dynamic(
  () => import("@/features/billing/collect-dialog").then((module) => module.CollectDialog),
  { ssr: false },
);

/** How long "Confirm cash" waits for its second tap before standing down. */
const ARM_MS = 4_000;

/**
 * The money line under a queue row: what is owed, and the two ways to take it.
 *
 * - **Get payment** opens the dialog: what was booked, a UPI QR for the exact
 *   amount, card or cash, and the transaction reference.
 * - **Paid** is cash in hand. Two taps — the first turns the button into
 *   "Confirm cash" for a few seconds — because a receipt recorded by a
 *   brushed thumb can only be undone by billing staff, and only with a reason.
 *
 * Both send the amount this row showed (`expected_amount`). If a charge has
 * landed since, the server refuses and the board refreshes, rather than
 * recording a different sum than the patient was asked for.
 *
 * Once settled the strip shrinks to a green "Paid" line. A charge that
 * arrives later (the doctor orders a test) brings the buttons back.
 *
 * Nothing for a deceased patient: that bill is settled with the family on
 * the billing screen, which records who paid (CLAUDE.md §6).
 */
export function CollectStrip({
  entry,
  className,
  onCollected,
}: {
  entry: QueueBoardEntry;
  className?: string;
  /** Called once the money is recorded — the board closes the line on it. */
  onCollected?: () => void;
}) {
  const t = useTranslations("billing");
  const common = useTranslations("common");
  const [paying, setPaying] = useState(false);
  const [armed, setArmed] = useState(false);
  const collect = useCollect(entry.encounter_id ?? "");

  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), ARM_MS);
    return () => window.clearTimeout(timer);
  }, [armed]);

  const due = entry.balance_due;
  if (!entry.encounter_id || due === null || due === undefined || entry.patient_is_deceased) {
    return null;
  }

  // Paid as far as this row knows: the board says so, or this strip just
  // collected exactly the amount the board is still showing until its next
  // refresh. A later, different balance is a new charge — buttons come back.
  const justCollected =
    collect.isSuccess && collect.variables?.expected_amount === due ? collect.data : null;

  if (!isPositive(due) || justCollected) {
    const paid = justCollected?.amount ?? entry.amount_paid;
    if (!isPositive(paid ?? null)) return null;
    return (
      <p
        className={cn("text-success flex items-center gap-1.5 text-sm font-medium", className)}
        data-testid="paid-chip"
      >
        <CircleCheck aria-hidden className="size-4" />
        {t("paidAmount", { amount: formatMoney(paid) })}
      </p>
    );
  }

  const name = entry.patient_name ?? common("notRecorded");
  const send = (body: CollectPayment, onSuccess?: () => void) =>
    collect.mutate(
      { ...body, expected_amount: body.expected_amount ?? due },
      {
        onSuccess: () => {
          onSuccess?.();
          onCollected?.();
        },
        onSettled: () => setArmed(false),
      },
    );

  return (
    <div
      className={cn("bg-caution/10 flex items-stretch gap-2 rounded-md p-1.5", className)}
      data-testid="collect-strip"
    >
      <div className="flex min-w-[4.5rem] flex-col justify-center px-1">
        <span className="tabular text-sm leading-tight font-semibold" data-testid="amount-due">
          {formatMoney(due)}
        </span>
        <span className="text-caution-foreground text-xs font-medium uppercase">
          {t("due")}
        </span>
      </div>

      <Button
        className="h-tap min-w-0 flex-1"
        disabled={collect.isPending}
        onClick={() => setPaying(true)}
        aria-label={t("getPaymentAria", { name })}
        data-testid="get-payment"
      >
        <QrCode aria-hidden className="size-4" />
        {t("getPayment")}
      </Button>

      <Button
        variant="outline"
        className={cn(
          "h-tap min-w-0 flex-1",
          armed && "bg-success text-success-foreground hover:bg-success/90 border-transparent",
        )}
        disabled={collect.isPending}
        onClick={() => (armed ? send({ method: "CASH" }) : setArmed(true))}
        aria-label={
          armed
            ? t("confirmCashAria", { amount: formatMoney(due), name })
            : t("markPaidAria", { name })
        }
        data-testid="mark-paid"
      >
        <Banknote aria-hidden className="size-4" />
        {armed ? t("confirmCash") : t("markPaid")}
      </Button>

      {paying ? (
        <CollectDialog
          entry={entry}
          encounterId={entry.encounter_id}
          open
          onOpenChange={setPaying}
          pending={collect.isPending}
          onConfirm={(body) => send(body, () => setPaying(false))}
        />
      ) : null}
    </div>
  );
}
