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
import { useElapsed } from "@/lib/hooks/use-elapsed";
import { cn } from "@/lib/utils";
import type { Dose, DoseStatus } from "@/types/api";

/**
 * One dose, and the one tap that signs for it.
 *
 * **Given is a single click and everything else is not**, and that asymmetry
 * is the design. Giving a drug is the overwhelmingly common case and it is
 * held to §7b's fifteen seconds; not giving one is rare, consequential, and
 * the backend refuses it without a reason. So "Given" is a button and the
 * others are a dialog that asks why — the friction sits exactly where the
 * information is needed.
 *
 * The three ways of not giving are kept apart rather than collapsed into one
 * "not given", because a hospital has to be able to tell them apart: the
 * patient refused, the ward ran out, or a doctor held it. Each leads somewhere
 * different.
 */
const NOT_GIVEN: DoseStatus[] = ["REFUSED", "HELD", "MISSED"];

export function DoseRow({
  dose,
  showPatient,
  canAdminister,
  onSigned,
}: {
  dose: Dose;
  /** True on the ward round, false on one patient's own chart. */
  showPatient: boolean;
  canAdminister: boolean;
  onSigned: () => void;
}) {
  const t = useTranslations("chart");
  const common = useTranslations("common");
  const [explaining, setExplaining] = useState(false);

  const give = useMutation({
    mutationFn: () => api.post<Dose>(`/ipd/doses/${dose.id}`, { status: "GIVEN" }),
    onSuccess: (signed) => {
      toast.success(t("doseGiven", { drug: signed.drug_name }));
      onSigned();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const due = new Date(dose.due_at);
  // `useElapsed` rather than `Date.now()` in the body: reading the clock during
  // render is impure, and it also would not tick — a dose that becomes overdue
  // while the nurse is looking at the list has to start showing as overdue
  // without a refetch.
  const minutesLate = useElapsed(dose.due_at);
  const overdue = dose.status === "DUE" && minutesLate > 0;

  return (
    <li
      className={cn(
        "flex flex-wrap items-center gap-3 rounded-lg border p-3",
        overdue && "border-caution/50 bg-caution/10",
        dose.status === "GIVEN" && "opacity-60",
        dose.patient_is_deceased && "border-foreground/40 bg-foreground/5",
      )}
      data-testid="dose-row"
      data-status={dose.status}
    >
      <div className="tabular w-16 shrink-0 text-sm font-semibold">
        {due.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
      </div>

      <div className="min-w-0 flex-1">
        <div className="font-medium">
          {dose.drug_name} <span className="text-muted-foreground">{dose.dose}</span>
          <span className="text-muted-foreground ml-2 text-xs">{dose.route}</span>
        </div>

        {showPatient ? (
          // The check a nurse makes before giving anything: is this the right
          // patient. It is why `DoseRead` carries identity at all.
          <div className="text-muted-foreground tabular text-xs">
            {dose.patient_name} · {dose.uhid}
            {dose.bed_code ? ` · ${dose.bed_code}` : ""}
          </div>
        ) : null}

        {dose.status !== "DUE" ? (
          <div className="text-muted-foreground mt-0.5 text-xs">
            {t(`dose${dose.status}` as "doseGIVEN")}
            {dose.administered_by_name ? ` · ${dose.administered_by_name}` : ""}
            {dose.reason ? ` · ${dose.reason}` : ""}
            {dose.auto_missed ? ` · ${t("autoMissed")}` : ""}
          </div>
        ) : null}
      </div>

      {dose.patient_is_deceased ? (
        <span className="bg-foreground/85 text-background rounded px-2 py-1 text-xs font-medium">
          {t("patientDeceased")}
        </span>
      ) : dose.status === "DUE" && canAdminister ? (
        <div className="flex shrink-0 gap-2">
          <Button
            size="sm"
            className="h-tap"
            disabled={give.isPending}
            onClick={() => give.mutate()}
            data-testid="dose-given"
          >
            {t("given")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-tap"
            onClick={() => setExplaining(true)}
          >
            {common("no")}
          </Button>
        </div>
      ) : null}

      {explaining ? (
        <NotGivenDialog
          dose={dose}
          open
          onOpenChange={(next) => {
            if (!next) setExplaining(false);
          }}
          onDone={() => {
            setExplaining(false);
            onSigned();
          }}
        />
      ) : null}
    </li>
  );
}

function NotGivenDialog({
  dose,
  open,
  onOpenChange,
  onDone,
}: {
  dose: Dose;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const t = useTranslations("chart");
  const common = useTranslations("common");

  const [status, setStatus] = useState<DoseStatus>("REFUSED");
  const [reason, setReason] = useState("");

  const record = useMutation({
    mutationFn: () =>
      api.post<Dose>(`/ipd/doses/${dose.id}`, { status, reason: reason.trim() }),
    onSuccess: () => {
      toast.success(t("doseRecorded"));
      onDone();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("notGivenTitle")}</DialogTitle>
          <DialogDescription>
            {dose.drug_name} {dose.dose} · {dose.patient_name}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="space-y-1.5">
            <Label htmlFor="dose-status">{t("whatHappened")}</Label>
            <Select
              value={status}
              onValueChange={(value) => setStatus((value ?? "REFUSED") as DoseStatus)}
            >
              <SelectTrigger id="dose-status" className="h-tap w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {NOT_GIVEN.map((option) => (
                  <SelectItem key={option} value={option}>
                    {t(`dose${option}` as "doseREFUSED")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="dose-reason">{t("reason")}</Label>
            <Input
              id="dose-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              className="h-tap"
              autoFocus
            />
            {/* Required by the backend, and this is the sentence explaining
                why: a blank reason on a missed antibiotic is the gap an
                incident review cannot close. */}
            <p className="text-muted-foreground text-xs">{t("reasonHint")}</p>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={reason.trim().length === 0 || record.isPending}
            onClick={() => record.mutate()}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
