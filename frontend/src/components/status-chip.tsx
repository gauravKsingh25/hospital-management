import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import type { EncounterStatus, InvoiceStatus, QueueStatus, WorklistStage } from "@/types/api";

/**
 * The colour-coded status chip (CLAUDE.md §7b).
 *
 * This is the encounter state machine rendered — no new data, no new
 * concepts. Because the mapping lives here and only here, "what colour is
 * PENDING_CLEARANCE?" has one answer everywhere in the app, and a new status
 * added to the backend is a line in this file rather than a hunt through JSX.
 *
 * Colour is never the only signal: each chip carries its label, so the
 * roughly one in twelve male staff with a colour vision deficiency reads the
 * same information as everyone else. That also makes the chips work in the
 * black-and-white printouts wards still put on clipboards.
 */

/** Every status the backend can produce, mapped once. */
const ENCOUNTER_TONES: Record<EncounterStatus, string> = {
  // Waiting states — cool, calm, no action implied yet.
  REGISTERED: "bg-info/10 text-info border-info/25",
  IN_CONSULTATION: "bg-primary/10 text-primary border-primary/25",

  // Someone is being kept waiting. Amber, because these are the states a
  // charge nurse should be able to spot from across the room.
  AWAITING_RESULTS: "bg-caution/15 text-caution-foreground border-caution/40",
  PENDING_CLEARANCE: "bg-caution/15 text-caution-foreground border-caution/40",

  ADMITTED: "bg-info/15 text-info border-info/30",

  // Closed cleanly.
  COMPLETED: "bg-success/10 text-success border-success/25",

  // Nothing went wrong clinically; the visit simply did not happen.
  CANCELLED: "bg-muted text-muted-foreground border-border",
  NO_SHOW: "bg-muted text-muted-foreground border-border",

  // The three edge-case terminals from CLAUDE.md §6. Deliberately distinct
  // from both "closed" and "cancelled": these are the ones that matter in an
  // audit, and they must never be mistaken for a routine ending at a glance.
  REFERRED_OUT: "bg-info/15 text-info border-info/40",
  LAMA: "bg-critical/10 text-critical border-critical/30",
  DECEASED: "bg-foreground/85 text-background border-foreground",
};

const QUEUE_TONES: Record<QueueStatus, string> = {
  WAITING: "bg-info/10 text-info border-info/25",
  CALLED: "bg-caution/15 text-caution-foreground border-caution/40",
  IN_CONSULTATION: "bg-primary/10 text-primary border-primary/25",
  COMPLETED: "bg-success/10 text-success border-success/25",
  SKIPPED: "bg-muted text-muted-foreground border-border",
  LEFT_WITHOUT_BEING_SEEN: "bg-critical/10 text-critical border-critical/30",
};

/**
 * The bench worklist's stages.
 *
 * Colour runs cool → warm as a request moves towards being finished, so a
 * technician scanning the list sees where the backlog is without reading it.
 * The two ends are the ones that matter: an unaccessioned request is the one
 * most likely to be forgotten, and an unverified result is the one holding a
 * patient's visit open.
 */
const STAGE_TONES: Record<WorklistStage, string> = {
  AWAITING_ACCESSION: "bg-critical/10 text-critical border-critical/30",
  AWAITING_COLLECTION: "bg-caution/15 text-caution-foreground border-caution/40",
  AWAITING_RECEIPT: "bg-info/10 text-info border-info/25",
  AWAITING_RESULTS: "bg-primary/10 text-primary border-primary/25",
  AWAITING_VERIFICATION: "bg-caution/15 text-caution-foreground border-caution/40",
};

const INVOICE_TONES: Record<InvoiceStatus, string> = {
  DRAFT: "bg-muted text-muted-foreground border-border",
  ISSUED: "bg-info/10 text-info border-info/25",
  PARTIALLY_PAID: "bg-caution/15 text-caution-foreground border-caution/40",
  PAID: "bg-success/10 text-success border-success/25",
  CANCELLED: "bg-muted text-muted-foreground border-border",
  WRITTEN_OFF: "bg-critical/10 text-critical border-critical/30",
};

const BASE =
  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 " +
  "text-xs font-medium whitespace-nowrap";

export function EncounterStatusChip({
  status,
  className,
}: {
  status: EncounterStatus;
  className?: string;
}) {
  const t = useTranslations("status");
  return (
    <span className={cn(BASE, ENCOUNTER_TONES[status], className)} data-status={status}>
      {t(status)}
    </span>
  );
}

export function QueueStatusChip({
  status,
  className,
}: {
  status: QueueStatus;
  className?: string;
}) {
  const t = useTranslations("queueStatus");
  return (
    <span className={cn(BASE, QUEUE_TONES[status], className)} data-status={status}>
      {t(status)}
    </span>
  );
}

export function WorklistStageChip({
  stage,
  className,
}: {
  stage: WorklistStage;
  className?: string;
}) {
  const t = useTranslations("lab");
  return (
    <span className={cn(BASE, STAGE_TONES[stage], className)} data-status={stage}>
      {t(`stage${stage}` as "stageAWAITING_ACCESSION")}
    </span>
  );
}

export function InvoiceStatusChip({
  status,
  className,
}: {
  status: InvoiceStatus;
  className?: string;
}) {
  const t = useTranslations("billing");
  return (
    <span className={cn(BASE, INVOICE_TONES[status], className)} data-status={status}>
      {t(`invoiceStatus${status}` as "invoiceStatusDRAFT")}
    </span>
  );
}
