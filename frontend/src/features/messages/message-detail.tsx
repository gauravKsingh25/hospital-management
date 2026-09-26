"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
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
import { MessageStatusChip } from "@/features/messages/message-status";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Message } from "@/types/api";

/**
 * One message, and every channel that was tried for it.
 *
 * **The attempt ladder is the whole point.** "We sent it" is not an answer to
 * a patient who received nothing; "WhatsApp rejected the number at 14:31, the
 * SMS was accepted at 14:31" is — and it tells the receptionist which of the
 * two numbers on the record is wrong. Without this screen that information
 * exists only in the database.
 *
 * A dialog rather than its own page, unlike the lab report and the visit
 * account. Those are work surfaces somebody sits at; this is a thing reception
 * checks with a patient waiting, and it should not cost them their place in
 * the list.
 */
export function MessageDetail({
  messageId,
  canSend,
  canCancel,
  onOpenChange,
  onChanged,
}: {
  messageId: string;
  canSend: boolean;
  canCancel: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged: () => void;
}) {
  const t = useTranslations("messages");
  const common = useTranslations("common");
  const [cancelling, setCancelling] = useState(false);

  const message = useQuery({
    queryKey: ["notifications", messageId],
    queryFn: ({ signal }) => api.get<Message>(`/notifications/${messageId}`, { signal }),
  });

  const retry = useMutation({
    mutationFn: () => api.post<Message>(`/notifications/${messageId}/retry`, {}),
    onSuccess: () => {
      toast.success(t("retried"));
      void message.refetch();
      onChanged();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const data = message.data;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("detailTitle")}</DialogTitle>
          <DialogDescription>
            {data ? `${data.template_code} · ${data.category}` : common("loading")}
          </DialogDescription>
        </DialogHeader>

        {data ? (
          <div className="space-y-4 text-left">
            <div className="flex flex-wrap items-center gap-2">
              <MessageStatusChip status={data.status} />
              {/*
                Why a message was not sent matters more than that it was not.
                A suppressed message has a reason and the reason is the answer.
              */}
              {data.suppression_reason ? (
                <span className="text-muted-foreground text-xs">
                  {t(`reason${data.suppression_reason}` as "reasonOPTED_OUT")}
                </span>
              ) : null}
              {data.cancellation_reason ? (
                <span className="text-muted-foreground text-xs">{data.cancellation_reason}</span>
              ) : null}
            </div>

            {/* Who it was actually addressed to — snapshotted when it was
                composed, so it is what was really used rather than whatever the
                record says today. */}
            <dl className="grid grid-cols-2 gap-3 rounded-lg border p-3 text-sm">
              <div>
                <dt className="text-muted-foreground text-xs">{t("recipient")}</dt>
                <dd className="font-medium">{data.recipient_name ?? common("notRecorded")}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground text-xs">{t("sentTo")}</dt>
                <dd className="tabular">
                  {data.recipient_phone ?? data.recipient_email ?? common("notRecorded")}
                </dd>
              </div>
            </dl>

            <div className="space-y-1.5">
              <p className="text-muted-foreground text-xs">{t("body")}</p>
              <p className="bg-muted/40 rounded-lg border p-3 text-sm whitespace-pre-wrap">
                {data.body}
              </p>
            </div>

            <AttemptLadder message={data} />

            {data.last_error ? (
              <p className="text-destructive text-xs">{data.last_error}</p>
            ) : null}
          </div>
        ) : null}

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("close")}
          </Button>

          {/* Only a message that actually failed can be retried. Offering the
              button on a suppressed one would invite somebody to try to
              override a suppression by pressing it. */}
          {canSend && data?.status === "FAILED" ? (
            <Button className="h-tap" disabled={retry.isPending} onClick={() => retry.mutate()}>
              {t("retry")}
            </Button>
          ) : null}

          {canCancel && data?.status === "PENDING" ? (
            <Button variant="outline" className="h-tap" onClick={() => setCancelling(true)}>
              {t("cancel")}
            </Button>
          ) : null}
        </DialogFooter>

        {cancelling && data ? (
          <CancelDialog
            messageId={data.id}
            onOpenChange={(next) => {
              if (!next) setCancelling(false);
            }}
            onDone={() => {
              setCancelling(false);
              void message.refetch();
              onChanged();
            }}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

/**
 * The ladder, rung by rung.
 *
 * CLAUDE.md §9 sets the channel order WhatsApp → SMS → email, and each attempt
 * records which one, whether it worked, and what the gateway said. Rendered
 * oldest-first, because the interesting reading is "it fell down to SMS", and
 * that only reads as a fall in order.
 */
function AttemptLadder({ message }: { message: Message }) {
  const t = useTranslations("messages");
  const attempts = message.attempt_log ?? [];

  if (attempts.length === 0) {
    return (
      <p className="text-muted-foreground rounded-lg border border-dashed p-3 text-xs">
        {/* An empty ladder on a suppressed message is not missing data — it is
            the proof that nothing was tried, which is the point. */}
        {message.status === "SUPPRESSED" ? t("noAttemptsSuppressed") : t("noAttempts")}
      </p>
    );
  }

  return (
    <ol className="space-y-1.5" data-testid="attempt-ladder">
      {attempts.map((attempt) => (
        <li
          key={attempt.id}
          className={cn(
            "flex flex-wrap items-baseline gap-x-3 gap-y-0.5 rounded-lg border px-3 py-2 text-sm",
            attempt.succeeded
              ? "border-success/25 bg-success/5"
              : "border-destructive/25 bg-destructive/5",
          )}
          data-testid="attempt-row"
          data-channel={attempt.channel}
          data-succeeded={attempt.succeeded}
        >
          <span className="font-medium">{attempt.channel}</span>
          <span className="text-muted-foreground tabular text-xs">{attempt.address}</span>
          <span className="ml-auto text-xs">
            {attempt.succeeded ? t("accepted") : (attempt.error_code ?? t("refused"))}
          </span>
          <span className="text-muted-foreground w-full text-xs">
            {new Date(attempt.created_at).toLocaleString()}
            {attempt.gateway ? ` · ${attempt.gateway}` : ""}
            {attempt.error_detail ? ` · ${attempt.error_detail}` : ""}
          </span>
        </li>
      ))}
    </ol>
  );
}

function CancelDialog({
  messageId,
  onOpenChange,
  onDone,
}: {
  messageId: string;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const t = useTranslations("messages");
  const common = useTranslations("common");
  const [reason, setReason] = useState("");

  const cancel = useMutation({
    mutationFn: () =>
      api.post<Message>(`/notifications/${messageId}/cancel`, { reason: reason.trim() }),
    onSuccess: () => {
      toast.success(t("cancelled"));
      onDone();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("cancelTitle")}</DialogTitle>
        </DialogHeader>

        <div className="space-y-1.5 text-left">
          <Label htmlFor="cancel-reason">{t("reason")}</Label>
          <Input
            id="cancel-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            className="h-tap"
            autoFocus
          />
          {/* Required by the backend. A message withdrawn with no explanation
              is one the next person re-sends. */}
          <p className="text-muted-foreground text-xs">{t("cancelReasonHint")}</p>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={reason.trim().length === 0 || cancel.isPending}
            onClick={() => cancel.mutate()}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
