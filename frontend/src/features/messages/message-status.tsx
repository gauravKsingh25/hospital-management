"use client";

import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import type { MessageStatus } from "@/types/api";

/**
 * A message's status, coloured and labelled.
 *
 * The distinction this chip exists to hold is **`SUPPRESSED` is not
 * `FAILED`**. The backend keeps them apart deliberately — "we chose not to
 * send" and "we tried and could not" answer different questions, and an
 * auditor asks the first — so the screen must not collapse them into one grey
 * "not sent". They get different colours and different words, and suppressed
 * carries its reason.
 *
 * Colour is never the only signal: every chip has its label too. A charge nurse
 * reading this across a counter, or roughly one in twelve male staff, would
 * otherwise be looking at identical grey pills.
 */
const TONES: Record<MessageStatus, string> = {
  PENDING: "bg-muted text-muted-foreground border-border",
  SENT: "border-success/25 bg-success/10 text-success",
  DELIVERED: "border-success/40 bg-success/15 text-success",
  FAILED: "border-destructive/30 bg-destructive/10 text-destructive",
  // Deliberate, not broken. Kept visually distinct from FAILED.
  SUPPRESSED: "border-foreground/30 bg-foreground/5 text-foreground",
  CANCELLED: "border-caution/40 bg-caution/15 text-caution-foreground",
};

export function MessageStatusChip({
  status,
  className,
}: {
  status: MessageStatus;
  className?: string;
}) {
  const t = useTranslations("messages");

  return (
    <span
      className={cn(
        "rounded-full border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap",
        TONES[status],
        className,
      )}
      data-status={status}
    >
      {t(`status${status}` as "statusSENT")}
    </span>
  );
}
