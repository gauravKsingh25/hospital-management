"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
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
import type { DoctorQueue, QueueBoardEntry, QueueEntry } from "@/types/api";

/**
 * Move a waiting patient to another doctor's queue.
 *
 * The receptionist's load-balancing lever (CLAUDE.md §7b). The destination
 * list shows each doctor's waiting count, because that number *is* the
 * decision — a dialog that made them remember the board they just left would
 * be a dialog they got wrong.
 *
 * Doctors not accepting patients are listed but disabled rather than hidden:
 * "Dr Rao is not here" is information reception wants when the patient in
 * front of them asked for Dr Rao by name.
 */
export function ReassignDialog({
  entry,
  columns,
  open,
  onOpenChange,
}: {
  entry: QueueBoardEntry;
  /** The board, for the destination list and its counts. */
  columns: DoctorQueue[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("queue");
  const common = useTranslations("common");
  const queryClient = useQueryClient();

  const [doctorId, setDoctorId] = useState<string>("");
  const [reason, setReason] = useState("");

  const destinations = columns.filter((column) => column.doctor_id !== entry.doctor_id);
  const chosen = destinations.find((column) => column.doctor_id === doctorId);

  const move = useMutation({
    mutationFn: () =>
      api.post<QueueEntry>(`/queue/${entry.id}/reassign`, {
        doctor_id: doctorId,
        reason: reason.trim() || null,
      }),
    onSuccess: (moved) => {
      toast.success(
        t("moved", {
          name: entry.patient_name ?? common("notRecorded"),
          doctor: chosen?.doctor_name ?? "",
          token: moved.token_number,
        }),
      );
      // The whole prefix: the board, the flat queue, and the doctor's own
      // list all changed — and the reports snapshot counts per doctor too.
      void queryClient.invalidateQueries({ queryKey: ["queue"] });
      void queryClient.invalidateQueries({ queryKey: ["reports", "queue"] });
      close(false);
    },
    onError: (error) => {
      if (!isApiError(error)) throw error;
      toast.error(error.message);
    },
  });

  const close = (next: boolean) => {
    if (!next) {
      setDoctorId("");
      setReason("");
    }
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("moveTitle")}</DialogTitle>
          <DialogDescription>
            {entry.patient_name ?? common("notRecorded")} · {t("token")} {entry.token_number}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (doctorId) move.mutate();
          }}
          className="space-y-4 text-left"
          noValidate
        >
          <div className="space-y-2">
            <Label htmlFor="reassign-doctor">{t("moveTo")}</Label>
            <Select value={doctorId} onValueChange={(value) => setDoctorId(value ?? "")}>
              <SelectTrigger id="reassign-doctor" className="h-tap w-full text-base">
                {/* Base UI shows the raw value — a UUID — unless told how to
                    label it. */}
                <SelectValue placeholder={t("moveToPlaceholder")}>
                  {(value: string | null) =>
                    destinations.find((column) => column.doctor_id === value)?.doctor_name ??
                    t("moveToPlaceholder")
                  }
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {destinations.map((column) => (
                  <SelectItem
                    key={column.doctor_id}
                    value={column.doctor_id}
                    disabled={!column.is_accepting_appointments}
                  >
                    {column.doctor_name}
                    {column.specialty ? ` · ${column.specialty}` : ""}
                    {" · "}
                    {column.is_accepting_appointments
                      ? t("waitingCount", { count: column.waiting })
                      : t("notAccepting")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {destinations.length === 0 ? (
              <p className="text-muted-foreground text-xs">{t("nowhereToMove")}</p>
            ) : null}
          </div>

          <div className="space-y-2">
            <Label htmlFor="reassign-reason">
              {t("moveReason")}{" "}
              <span className="text-muted-foreground font-normal">({common("optional")})</span>
            </Label>
            <Input
              id="reassign-reason"
              className="h-tap"
              maxLength={255}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={t("moveReasonPlaceholder")}
            />
          </div>

          {/* The one thing worth saying before they click: the patient gets a
              new number, and somebody has to tell them. */}
          <p className="text-muted-foreground text-sm">{t("moveNewToken")}</p>

          <DialogFooter>
            <Button type="button" variant="outline" className="h-tap" onClick={() => close(false)}>
              {common("cancel")}
            </Button>
            <Button type="submit" className="h-tap" disabled={!doctorId || move.isPending}>
              {move.isPending ? t("moving") : t("move")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
