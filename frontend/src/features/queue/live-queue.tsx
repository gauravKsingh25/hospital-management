"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Activity, Users } from "lucide-react";

import { QueueStatusChip } from "@/components/status-chip";
import { Button } from "@/components/ui/button";
import { LinkButton } from "@/components/ui/link-button";
import { Skeleton } from "@/components/ui/skeleton";
import { VitalsDialog } from "@/features/vitals/vitals-dialog";
import { api } from "@/lib/api/client";
import { useElapsed } from "@/lib/hooks/use-elapsed";
import { cn } from "@/lib/utils";
import type { QueueBoardEntry } from "@/types/api";

/**
 * The live OPD queue.
 *
 * Refetched every fifteen seconds, and on window focus. That number is not
 * arbitrary: several people watch this screen at once — the receptionist who
 * just checked someone in, the nurse calling for vitals, the doctor waiting
 * for a name — and a queue that is a minute stale is a doctor calling a
 * patient who has already been seen.
 *
 * Polling rather than a websocket, deliberately. A websocket would be more
 * elegant and would cost a persistent connection per open screen, a
 * reconnection strategy, and sticky routing at the load balancer, in exchange
 * for saving a handful of small requests a minute. That trade only pays at a
 * scale this system does not have yet; when it does, this is one hook to
 * change.
 */
export function LiveQueue({
  mine = false,
  showConsultLink = false,
  canRecordVitals = false,
}: {
  /** Use `/queue/mine` — the signed-in doctor's own list. */
  mine?: boolean;
  /** Render a link into the consultation screen on each row. */
  showConsultLink?: boolean;
  /** Offer the nurse's one-click vitals action on each row (CLAUDE.md §7b). */
  canRecordVitals?: boolean;
}) {
  const t = useTranslations("queue");

  const queue = useQuery({
    queryKey: ["queue", mine ? "mine" : "all"],
    queryFn: ({ signal }) =>
      api.get<QueueBoardEntry[]>(mine ? "/queue/mine" : "/queue", {
        query: mine ? undefined : { active_only: true },
        signal,
      }),
    refetchInterval: 15_000,
    // Zero, so switching back to this tab always shows the true queue rather
    // than whatever was there when the user left.
    staleTime: 0,
  });

  if (queue.isPending) {
    return (
      <div className="space-y-2" aria-busy="true">
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-16 w-full rounded-lg" />
        ))}
      </div>
    );
  }

  const entries = queue.data ?? [];

  if (entries.length === 0) {
    return (
      <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-lg border border-dashed py-16 text-center">
        <Users aria-hidden className="size-6" />
        <p className="text-foreground text-sm font-medium">{t("empty")}</p>
        <p className="text-sm">{t("emptyHint")}</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-xl text-sm">
        <caption className="sr-only">{t("title")}</caption>
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th scope="col" className="px-4 py-2.5 text-left font-medium">
              {t("token")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-left font-medium">
              {t("patient")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-left font-medium">
              {t("waitingFor")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">
              {t("title")}
            </th>
          </tr>
        </thead>
        <tbody className="divide-border divide-y">
          {entries.map((entry) => (
            <QueueRow
              key={entry.id}
              entry={entry}
              showConsultLink={showConsultLink}
              canRecordVitals={canRecordVitals}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QueueRow({
  entry,
  showConsultLink,
  canRecordVitals,
}: {
  entry: QueueBoardEntry;
  showConsultLink: boolean;
  canRecordVitals: boolean;
}) {
  const t = useTranslations("queue");
  const common = useTranslations("common");
  const elapsed = useElapsed(entry.checked_in_at);
  const [recordingVitals, setRecordingVitals] = useState(false);

  // Thirty minutes is the point at which people start coming to the desk to
  // ask — the same threshold the backend's reporting module uses for its
  // doctor-delay alert, so the counter and the dashboard agree.
  const overdue = elapsed >= 30 && entry.status === "WAITING";

  // `priority` is an IntEnum: 10 emergency, 20 priority, 30 normal.
  const prioritised = entry.priority < 30;

  return (
    <tr className={cn(overdue && "bg-caution/10")} data-testid="queue-row">
      <td className="tabular px-4 py-3 text-lg font-semibold">{entry.token_number}</td>

      <td className="px-4 py-3">
        <div className="font-medium">{entry.patient_name ?? common("notRecorded")}</div>
        <div className="text-muted-foreground tabular text-xs">
          {entry.patient_uhid}
          {entry.patient_age_years !== null && entry.patient_age_years !== undefined
            ? ` · ${entry.patient_age_years}`
            : ""}
          {entry.patient_gender ? ` · ${entry.patient_gender[0]}` : ""}
        </div>
        {prioritised ? (
          <span className="bg-caution/20 text-caution-foreground mt-1 inline-block rounded px-1.5 py-0.5 text-[10px] font-medium">
            {entry.priority_reason ?? t("runningLate")}
          </span>
        ) : null}
      </td>

      <td className={cn("tabular px-4 py-3", overdue && "text-caution-foreground font-medium")}>
        {t("minutes", { count: elapsed })}
        {overdue ? <div className="text-xs">{t("runningLate")}</div> : null}
      </td>

      <td className="px-4 py-3 text-right">
        <div className="flex items-center justify-end gap-2">
          <QueueStatusChip status={entry.status} />

          {/*
            The nurse's fifteen-second action. A dialog rather than a screen so
            they keep their place in the list — navigating away and back is
            most of the difference between fifteen seconds and forty.
          */}
          {canRecordVitals && entry.encounter_id ? (
            <>
              <Button
                size="sm"
                variant="outline"
                className="h-tap"
                onClick={() => setRecordingVitals(true)}
              >
                <Activity aria-hidden className="size-4" />
                <span className="sr-only sm:not-sr-only">{t("recordVitals")}</span>
              </Button>
              <VitalsDialog
                encounterId={entry.encounter_id}
                patientName={entry.patient_name ?? common("notRecorded")}
                open={recordingVitals}
                onOpenChange={setRecordingVitals}
              />
            </>
          ) : null}

          {showConsultLink && entry.encounter_id ? (
            <LinkButton size="sm" className="h-tap" href={`/consultation/${entry.encounter_id}`}>
              {entry.status === "IN_CONSULTATION" ? t("resume") : t("startConsultation")}
            </LinkButton>
          ) : null}
        </div>
      </td>
    </tr>
  );
}
