"use client";

import { useMutation } from "@tanstack/react-query";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { DiagnosticReport, WorklistEntry } from "@/types/api";

import { useCatalogue } from "@/features/lab/worklist";

/**
 * Accepting a doctor's request onto the bench.
 *
 * The one decision here is which catalogue entry the request means, and the
 * lab makes it rather than the doctor. That is deliberate on the backend's
 * part (`AccessionRequest.catalogue_item_id`) and worth preserving here: a
 * doctor writes "CBC" at speed, and the technician is the person who knows
 * which panel that is and can correct a typo without bouncing the request
 * back to the consulting room.
 *
 * The written request is shown verbatim above the picker, because matching it
 * to a catalogue entry is exactly the judgement being asked for.
 */
export function AccessionDialog({
  open,
  onOpenChange,
  entry,
  onAccessioned,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  entry: WorklistEntry;
  onAccessioned: () => void;
}) {
  const t = useTranslations("lab");
  const common = useTranslations("common");
  const [itemId, setItemId] = useState("");

  const discipline = entry.order_type === "RADIOLOGY" ? "RADIOLOGY" : "LAB";
  const catalogue = useCatalogue(discipline);

  // Only active entries: a test the hospital has retired must not be
  // accessionable, and an inactive one in the list is a trap.
  const items = (catalogue.data?.items ?? []).filter((item) => item.is_active);

  const accession = useMutation({
    mutationFn: () =>
      api.post<DiagnosticReport>("/diagnostics/reports", {
        order_id: entry.order_id,
        catalogue_item_id: itemId,
      }),
    onSuccess: (report) => {
      toast.success(
        report.specimen
          ? t("accessionedWithSample", { accession: report.specimen.accession_number })
          : t("accessioned", { report: report.report_number }),
      );
      setItemId("");
      onAccessioned();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("accessionTitle")}</DialogTitle>
          <DialogDescription>
            {entry.patient_name} · {entry.patient_uhid}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="bg-muted/50 rounded-md border p-3">
            <div className="text-muted-foreground text-xs">{t("asRequested")}</div>
            <div className="font-medium">{entry.item_name}</div>
            {entry.instructions ? (
              <div className="text-muted-foreground mt-1 text-xs italic">{entry.instructions}</div>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="catalogue-item">{t("catalogueItem")}</Label>
            <Select value={itemId} onValueChange={(value) => setItemId(value ?? "")}>
              <SelectTrigger id="catalogue-item" className="h-tap w-full">
                <SelectValue placeholder={catalogue.isPending ? common("loading") : t("choose")} />
              </SelectTrigger>
              <SelectContent>
                {items.map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    {item.code} — {item.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {!catalogue.isPending && items.length === 0 ? (
              <p className="text-muted-foreground text-xs">{t("emptyCatalogue")}</p>
            ) : null}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!itemId || accession.isPending}
            onClick={() => accession.mutate()}
          >
            {t("accession")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
