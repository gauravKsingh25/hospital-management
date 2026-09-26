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
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ADMISSION_REQUESTS_KEY } from "@/features/admissions/use-admission-requests";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { AdmissionRequest } from "@/types/api";

/**
 * Reception hands a seen patient to the admission desk.
 *
 * A dialog rather than a one-click button: this sends a person across the
 * building, and the confirmation is where reception writes the one line the
 * desk needs ("ICU bed", "family waiting at gate 2"). The note is optional —
 * the doctor's advice to admit is the whole request.
 *
 * Reception does not pick a bed here. That is the desk's job, done on its own
 * screen with the live bed board in front of it.
 */
export function SendToAdmissionDialog({
  encounterId,
  patientName,
  open,
  onOpenChange,
}: {
  encounterId: string;
  patientName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admissions");
  const common = useTranslations("common");
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");

  const close = (next: boolean) => {
    if (!next) setNote("");
    onOpenChange(next);
  };

  const send = useMutation({
    mutationFn: () =>
      api.post<AdmissionRequest>("/ipd/admission-requests", {
        encounter_id: encounterId,
        note: note.trim() || null,
      }),
    onSuccess: () => {
      toast.success(t("sent", { name: patientName }));
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
          <DialogTitle>{t("sendTitle")}</DialogTitle>
          <DialogDescription>{patientName}</DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            send.mutate();
          }}
          className="space-y-4 text-left"
          noValidate
        >
          <div className="space-y-2">
            <Label htmlFor="admission-note">
              {t("note")}{" "}
              <span className="text-muted-foreground font-normal">({common("optional")})</span>
            </Label>
            <Textarea
              id="admission-note"
              value={note}
              maxLength={500}
              rows={2}
              onChange={(event) => setNote(event.target.value)}
              placeholder={t("notePlaceholder")}
            />
          </div>
          <p className="text-muted-foreground text-sm">{t("sendHint")}</p>

          <DialogFooter>
            <Button type="button" variant="outline" className="h-tap" onClick={() => close(false)}>
              {common("cancel")}
            </Button>
            <Button type="submit" className="h-tap" disabled={send.isPending}>
              {send.isPending ? t("sending") : t("send")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
