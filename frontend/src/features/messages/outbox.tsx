"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Pager } from "@/components/pager";
import { Button } from "@/components/ui/button";
import { MessageDetail } from "@/features/messages/message-detail";
import { MessageStatusChip } from "@/features/messages/message-status";
import { api } from "@/lib/api/client";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { cn } from "@/lib/utils";
import type { MessageStatus, MessageSummary, Page } from "@/types/api";

/**
 * The outbox — reception's answer to "I never got the message".
 *
 * Everything about this screen follows from that one sentence. It is said by a
 * named person standing at a counter, so rows lead with the patient's name and
 * UHID rather than an id (which is why `NotificationSummary` gained them). The
 * filters are the three questions actually asked: *did anything go out at all*,
 * *what failed*, and *what did we choose not to send*.
 *
 * The last of those is the one most systems cannot answer. A suppressed
 * message is not a failure — it is a decision, usually a correct one — and
 * folding it into "not delivered" is how a hospital ends up unable to show an
 * auditor that it stopped messaging a deceased patient's family.
 */
const FILTERS = [
  { key: "all", status: undefined },
  { key: "failed", status: "FAILED" },
  { key: "suppressed", status: "SUPPRESSED" },
  { key: "pending", status: "PENDING" },
] as const;

type FilterKey = (typeof FILTERS)[number]["key"];

const OUTBOX_KEY = ["notifications", "outbox"] as const;

export function Outbox({
  initial,
  canSend,
  canCancel,
}: {
  initial: Page<MessageSummary>;
  canSend: boolean;
  canCancel: boolean;
}) {
  const t = useTranslations("messages");
  const queryClient = useQueryClient();

  const [filter, setFilter] = useState<FilterKey>("all");
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(null);

  const status = FILTERS.find((entry) => entry.key === filter)?.status;

  const outbox = useQuery({
    queryKey: [...OUTBOX_KEY, filter, offset],
    queryFn: ({ signal }) =>
      api.get<Page<MessageSummary>>("/notifications", {
        query: {
          limit: BOARD_PAGE_SIZE,
          offset,
          ...(status ? { message_status: status } : {}),
        },
        signal,
      }),
    initialData: filter === "all" && offset === 0 ? initial : undefined,
    staleTime: 10_000,
  });

  const rows = outbox.data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-1" role="group" aria-label={t("filter")}>
        {FILTERS.map((entry) => (
          <Button
            key={entry.key}
            size="sm"
            variant={entry.key === filter ? "default" : "outline"}
            className="h-tap"
            aria-pressed={entry.key === filter}
            onClick={() => {
              setFilter(entry.key);
              // Back to page one: a filter change makes the old offset
              // meaningless, and landing on an empty page three reads as "there
              // are none" when there are plenty.
              setOffset(0);
            }}
            data-testid={`filter-${entry.key}`}
          >
            {t(entry.key)}
          </Button>
        ))}
      </div>

      {rows.length === 0 ? (
        <p className="text-muted-foreground rounded-lg border border-dashed py-12 text-center text-sm">
          {t("empty")}
        </p>
      ) : (
        <ul className="space-y-2">
          {rows.map((row) => (
            <li key={row.id}>
              <button
                type="button"
                className={cn(
                  "hover:border-primary/50 hover:bg-accent/40 focus-visible:ring-ring w-full",
                  "rounded-lg border p-3 text-left transition-colors focus-visible:ring-2",
                  "focus-visible:outline-none",
                )}
                onClick={() => setOpen(row.id)}
                data-testid="message-row"
                data-status={row.status}
              >
                <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                  <div className="min-w-0">
                    {/* The name first. This is the field the whole schema
                        change was for. */}
                    <div className="truncate font-medium">
                      {row.patient_name ?? t("staffRecipient")}
                    </div>
                    <div className="text-muted-foreground tabular truncate text-xs">
                      {row.uhid ? `${row.uhid} · ` : ""}
                      {row.template_code}
                      {row.channel ? ` · ${row.channel}` : ""}
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-2">
                    {row.needs_template ? (
                      // Sent from the fallback copy because the hospital has
                      // written none. It went out, so this is not an error —
                      // but it went out in the shipped wording, in English.
                      <span className="border-caution/40 bg-caution/15 text-caution-foreground rounded-full border px-2 py-0.5 text-[10px] font-medium">
                        {t("needsTemplate")}
                      </span>
                    ) : null}
                    <MessageStatusChip status={row.status as MessageStatus} />
                  </div>
                </div>

                <div className="text-muted-foreground tabular mt-1 text-xs">
                  {new Date(row.sent_at ?? row.created_at).toLocaleString()}
                  {row.attempts > 1 ? ` · ${t("attemptCount", { count: row.attempts })}` : ""}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      <Pager
        page={outbox.data}
        offset={offset}
        pageSize={BOARD_PAGE_SIZE}
        onOffsetChange={setOffset}
      />

      {open ? (
        <MessageDetail
          messageId={open}
          canSend={canSend}
          canCancel={canCancel}
          onOpenChange={(next) => {
            if (!next) setOpen(null);
          }}
          onChanged={() => void queryClient.invalidateQueries({ queryKey: OUTBOX_KEY })}
        />
      ) : null}
    </div>
  );
}
