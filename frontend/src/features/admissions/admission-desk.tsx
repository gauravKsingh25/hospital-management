"use client";

import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";
import { BedDouble, Inbox } from "lucide-react";
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
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  ADMISSION_REQUESTS_KEY,
  usePendingAdmissionRequests,
  useTodaysAdmissionRequests,
} from "@/features/admissions/use-admission-requests";
import { AdmitDialog } from "@/features/wards/admit-dialog";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { useElapsed } from "@/lib/hooks/use-elapsed";
import { cn } from "@/lib/utils";
import type { AdmissionRequest } from "@/types/api";

/**
 * The admission desk (the "new section").
 *
 * Patients the OPD has sent for admission, oldest first — the order they have
 * been standing at the counter. Each has two actions and only two:
 *
 * - **Admit** opens the same bed picker the doctor's screen uses. Admitting
 *   takes the bed, turns the visit into an inpatient stay and lands on the
 *   admission — the ordinary IPD flow from there.
 * - **Turn away** — sent in error, or the family chose another hospital —
 *   with a reason, because that decision has to be explicable afterwards.
 *
 * Below the list, what the desk has already dealt with today, so "did the
 * patient from Dr Rao get a bed?" is answerable without a phone call.
 */
export function AdmissionDesk() {
  const t = useTranslations("admissions");
  const pending = usePendingAdmissionRequests();
  const today = useTodaysAdmissionRequests();

  const waiting = pending.data?.items ?? [];
  const handled = (today.data?.items ?? []).filter((request) => request.status !== "PENDING");

  return (
    <div className="space-y-8">
      <section aria-labelledby="desk-waiting" className="space-y-3">
        <h2 id="desk-waiting" className="text-lg font-semibold">
          {t("waitingTitle", { count: waiting.length })}
        </h2>

        {pending.isPending ? (
          <div className="space-y-2" aria-busy="true">
            {[0, 1].map((row) => (
              <Skeleton key={row} className="h-24 w-full rounded-lg" />
            ))}
          </div>
        ) : waiting.length === 0 ? (
          <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-lg border border-dashed py-14 text-center">
            <Inbox aria-hidden className="size-6" />
            <p className="text-foreground text-sm font-medium">{t("empty")}</p>
            <p className="text-sm">{t("emptyHint")}</p>
          </div>
        ) : (
          <ul className="divide-border divide-y rounded-lg border" data-testid="admission-requests">
            {waiting.map((request) => (
              <WaitingRow key={request.id} request={request} />
            ))}
          </ul>
        )}
      </section>

      {handled.length > 0 ? (
        <section aria-labelledby="desk-handled" className="space-y-3">
          <h2 id="desk-handled" className="text-muted-foreground text-sm font-medium uppercase">
            {t("handledTitle", { count: handled.length })}
          </h2>
          <ul className="divide-border divide-y rounded-lg border" data-testid="handled-requests">
            {handled.map((request) => (
              <HandledRow key={request.id} request={request} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function identityLine(request: AdmissionRequest): string {
  return [
    request.patient_uhid,
    request.patient_age_years !== null && request.patient_age_years !== undefined
      ? String(request.patient_age_years)
      : null,
    request.patient_gender ? request.patient_gender[0] : null,
    request.patient_phone,
  ]
    .filter(Boolean)
    .join(" · ");
}

function WaitingRow({ request }: { request: AdmissionRequest }) {
  const t = useTranslations("admissions");
  const common = useTranslations("common");
  const waited = useElapsed(request.requested_at);
  const [admitting, setAdmitting] = useState(false);
  const [turningAway, setTurningAway] = useState(false);
  const name = request.patient_name ?? common("notRecorded");

  return (
    <li
      className={cn("flex flex-wrap items-center gap-3 p-3", waited >= 30 && "bg-caution/10")}
      data-testid="admission-request"
    >
      <div className="min-w-[14rem] flex-1 space-y-0.5">
        <div className="font-medium">{name}</div>
        <div className="text-muted-foreground tabular text-xs">{identityLine(request)}</div>
        <div className="text-muted-foreground text-xs">
          {[
            request.doctor_name ? t("fromDoctor", { doctor: request.doctor_name }) : null,
            request.requested_by_name ? t("sentBy", { name: request.requested_by_name }) : null,
            t("waiting", { minutes: waited }),
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
        {request.note ? (
          <p className="bg-muted mt-1 rounded px-2 py-1 text-sm">{request.note}</p>
        ) : null}
      </div>

      <div className="flex shrink-0 gap-2">
        <Button variant="outline" className="h-tap" onClick={() => setTurningAway(true)}>
          {t("turnAway")}
        </Button>
        <Button className="h-tap" onClick={() => setAdmitting(true)} data-testid="desk-admit">
          <BedDouble aria-hidden className="size-4" />
          {t("admit")}
        </Button>
      </div>

      <AdmitDialog
        requestId={request.id}
        patientName={name}
        patientGender={request.patient_gender}
        open={admitting}
        onOpenChange={setAdmitting}
      />
      <TurnAwayDialog
        request={request}
        patientName={name}
        open={turningAway}
        onOpenChange={setTurningAway}
      />
    </li>
  );
}

function HandledRow({ request }: { request: AdmissionRequest }) {
  const t = useTranslations("admissions");
  const common = useTranslations("common");
  const format = useFormatter();
  const admitted = request.status === "ADMITTED";

  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
      <span className="min-w-[10rem] flex-1 font-medium">
        {request.patient_name ?? common("notRecorded")}
      </span>
      <span
        className={cn(
          "rounded border px-1.5 py-0.5 text-xs font-medium",
          admitted
            ? "bg-success/10 text-success border-success/25"
            : "bg-muted text-muted-foreground border-border",
        )}
      >
        {admitted ? t("statusAdmitted") : t("statusTurnedAway")}
      </span>
      {request.handled_at ? (
        <span className="text-muted-foreground tabular text-xs">
          {format.dateTime(new Date(request.handled_at), { hour: "2-digit", minute: "2-digit" })}
        </span>
      ) : null}
      {admitted && request.admission_id ? (
        <Link
          href={`/admissions/${request.admission_id}`}
          className="text-sm font-medium underline underline-offset-4"
        >
          {t("openAdmission")}
        </Link>
      ) : request.cancellation_reason ? (
        <span className="text-muted-foreground w-full text-xs">{request.cancellation_reason}</span>
      ) : null}
    </li>
  );
}

function TurnAwayDialog({
  request,
  patientName,
  open,
  onOpenChange,
}: {
  request: AdmissionRequest;
  patientName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admissions");
  const common = useTranslations("common");
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");

  const close = (next: boolean) => {
    if (!next) setReason("");
    onOpenChange(next);
  };

  const turnAway = useMutation({
    mutationFn: () =>
      api.post<AdmissionRequest>(`/ipd/admission-requests/${request.id}/cancel`, {
        reason: reason.trim(),
      }),
    onSuccess: () => {
      toast.success(t("turnedAway", { name: patientName }));
      void queryClient.invalidateQueries({ queryKey: ADMISSION_REQUESTS_KEY });
      close(false);
    },
    onError: (error) => {
      if (!isApiError(error)) throw error;
      toast.error(error.message);
    },
  });

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("turnAwayTitle")}</DialogTitle>
          <DialogDescription>{patientName}</DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (reason.trim()) turnAway.mutate();
          }}
          className="space-y-3 text-left"
          noValidate
        >
          <div className="space-y-2">
            <Label htmlFor={`turn-away-${request.id}`}>{t("turnAwayReason")}</Label>
            <Textarea
              id={`turn-away-${request.id}`}
              value={reason}
              rows={2}
              maxLength={255}
              onChange={(event) => setReason(event.target.value)}
              placeholder={t("turnAwayPlaceholder")}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" className="h-tap" onClick={() => close(false)}>
              {common("cancel")}
            </Button>
            <Button
              type="submit"
              variant="destructive"
              className="h-tap"
              disabled={!reason.trim() || turnAway.isPending}
            >
              {t("turnAway")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
