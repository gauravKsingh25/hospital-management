"use client";

import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import type { QueueBoardEntry } from "@/types/api";

// `priority` is an IntEnum: 10 emergency, 20 priority, 30 normal.
const EMERGENCY = 10;
const NORMAL = 30;

/**
 * The small tags after a queue row's name: why the patient is ahead of the
 * line, and — for an emergency arrival nobody has named — that they still
 * need identifying. Shared by reception's board and the nurse's queue so the
 * two never disagree about what red means.
 */
export function PriorityTags({
  entry,
  className,
}: {
  entry: Pick<QueueBoardEntry, "priority" | "priority_reason" | "patient_is_unidentified">;
  className?: string;
}) {
  const t = useTranslations("queue");
  const tEmergency = useTranslations("emergency");
  const emergency = entry.priority === EMERGENCY;

  return (
    <>
      {entry.priority < NORMAL ? (
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-xs font-medium",
            emergency
              ? "bg-destructive/15 text-destructive"
              : "bg-caution/20 text-caution-foreground",
            className,
          )}
          data-testid={emergency ? "emergency-tag" : undefined}
        >
          {emergency ? tEmergency("tag") : (entry.priority_reason ?? t("runningLate"))}
        </span>
      ) : null}
      {entry.patient_is_unidentified ? (
        <span
          className={cn(
            "border-destructive/40 text-destructive rounded border px-1.5 py-0.5 text-xs font-medium",
            className,
          )}
          data-testid="unidentified-tag"
        >
          {tEmergency("notIdentifiedShort")}
        </span>
      ) : null}
    </>
  );
}
