"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeftRight, ClipboardCheck, FileText, LogOut, Pill } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";

import { PatientIdentityStrip, SafetyBanner } from "@/components/patient/safety-banner";
import { Button } from "@/components/ui/button";
import { LinkButton } from "@/components/ui/link-button";
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
import { Textarea } from "@/components/ui/textarea";
import { ItemsUsedPanel, StayCostPanel } from "@/features/medicines/stay-medicines";
import { VisitOutcomeAction } from "@/features/outcomes/visit-outcome-action";
import { LatestVitals } from "@/features/vitals/latest-vitals";
import {
  ClinicalPanel,
  DischargeReadiness,
  DueNowPanel,
  StayPanel,
} from "@/features/wards/stay-panels";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Admission, BoardWard, DischargeType } from "@/types/api";

/**
 * One inpatient stay.
 *
 * Three acts live here and the order between them is the point.
 * **Fit for discharge** is the doctor saying the patient may go; it starts the
 * paperwork and *does not free the bed*. **Discharge** is the patient actually
 * leaving. A system that frees the bed at the doctor's signature is a system
 * that double-books it, because the patient is still in it settling the bill
 * and waiting for medicines — which is exactly why `DISCHARGE_INITIATED` is a
 * status rather than a flag.
 *
 * **Transfer** moves them between beds, releasing the old one to cleaning.
 *
 * Layout: a sticky header — who, where, the safety banner, every act on the
 * stay — then two columns. On the left what needs doing (doses due, vitals)
 * and the clinical facts; on the right the stay's practical facts, what it has
 * cost so far (from the admission on — the same number the IPD bill starts
 * from), what it has used, and how close it is to discharge. Every missing
 * detail can be filled in where it shows "Not recorded".
 *
 * Two discharge outcomes are deliberately missing from the picker: death and
 * leaving against advice. Both are recorded against the *visit* through
 * `clinical`, which captures what CLAUDE.md §6 requires — who certified, the
 * cause, whether the LAMA form was signed — and the admission follows
 * automatically. The backend's schema refuses them here; offering them would
 * create a second, thinner way to record a death.
 */
const DISCHARGE_TYPES: DischargeType[] = ["RECOVERED", "REFERRED", "TRANSFERRED_OUT"];

export function AdmissionScreen({
  admissionId,
  initial,
  canTransfer,
  canDischarge,
  canIssue = false,
  canSeeCost = false,
  canRecordDeath = false,
  canRecordReferral = false,
  canRecordLama = false,
  currentUserId,
  canReadVitals = false,
  canRecordVitals = false,
  canUpdate = false,
  canReadChart = false,
  canAdminister = false,
}: {
  admissionId: string;
  initial: Admission;
  canTransfer: boolean;
  canDischarge: boolean;
  /** Record medicines and supplies used (`medicine:issue`). */
  canIssue?: boolean;
  /** See what the stay has cost (`charge:read`). */
  canSeeCost?: boolean;
  canRecordDeath?: boolean;
  canRecordReferral?: boolean;
  canRecordLama?: boolean;
  /** Who is signed in, so a doctor recording a death starts as its certifier. */
  currentUserId?: string;
  canReadVitals?: boolean;
  canRecordVitals?: boolean;
  /** Edit the stay's details inline (`admission:update`). */
  canUpdate?: boolean;
  /** See doses due (`medication:read`). */
  canReadChart?: boolean;
  /** Sign for a dose from "Due now" (`medication:administer`). */
  canAdminister?: boolean;
}) {
  const t = useTranslations("wards");
  const common = useTranslations("common");

  const [transferring, setTransferring] = useState(false);
  const [discharging, setDischarging] = useState(false);

  const admission = useQuery({
    queryKey: ["ipd", "admission", admissionId],
    queryFn: ({ signal }) => api.get<Admission>(`/ipd/admissions/${admissionId}`, { signal }),
    initialData: initial,
    staleTime: 0,
  });

  const data = admission.data;
  const live = data.status === "ADMITTED" || data.status === "DISCHARGE_INITIATED";

  const initiate = useMutation({
    mutationFn: () =>
      api.post<Admission>(`/ipd/admissions/${admissionId}/initiate-discharge`, {}),
    onSuccess: () => {
      toast.success(t("dischargeInitiated"));
      void admission.refetch();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const banner = data.safety_banner;
  const editable = canUpdate && live;

  return (
    <div className="space-y-4">
      {/* Sticky under the app bar: who, where, what is dangerous about them,
          and every act on the stay — never a scroll away. */}
      <header
        className={cn(
          "bg-background/95 supports-[backdrop-filter]:bg-background/85 sticky top-16 z-20 space-y-3 rounded-lg border p-4 shadow-sm backdrop-blur",
          data.patient_is_deceased && "border-foreground/40",
        )}
        data-testid="stay-header"
      >
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <div className="min-w-0 space-y-1">
            {banner ? (
              <PatientIdentityStrip banner={banner} as="h1" />
            ) : (
              <h1 className="text-xl font-semibold tracking-tight">
                {data.patient_name ?? common("notRecorded")}
              </h1>
            )}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
              <span
                className={cn(
                  "rounded-full border px-2.5 py-0.5 text-xs font-medium",
                  data.status === "ADMITTED" && "border-info/30 bg-info/10 text-info",
                  data.status === "DISCHARGE_INITIATED" &&
                    "border-caution/40 bg-caution/15 text-caution-foreground",
                  data.status === "DISCHARGED" && "border-success/25 bg-success/10 text-success",
                  data.status === "CANCELLED" && "bg-muted text-muted-foreground border-border",
                )}
                data-status={data.status}
              >
                {t(`admission${data.status}` as "admissionADMITTED")}
              </span>
              <span className="font-medium" data-testid="stay-day">
                {data.expected_stay_days
                  ? t("dayOfExpected", {
                      day: Math.max(1, data.length_of_stay_days),
                      expected: data.expected_stay_days,
                    })
                  : t("dayOfStay", { day: Math.max(1, data.length_of_stay_days) })}
              </span>
              <span className="text-muted-foreground">
                {data.attending_doctor_name ?? t("noDoctorYet")}
              </span>
            </div>
          </div>
          <div className="text-right">
            <div className="font-semibold">
              {data.ward?.name ?? "—"} · {data.bed?.code ?? "—"}
            </div>
            <div className="text-muted-foreground tabular text-xs">{data.admission_number}</div>
          </div>
        </div>

        {banner ? <SafetyBanner banner={banner} /> : null}

        {data.status === "DISCHARGE_INITIATED" ? (
          // The distinction that stops a bed being double-booked.
          <p className="border-caution/50 bg-caution/10 rounded-md border px-3 py-2 text-sm">
            {t("dischargeInitiatedHint")}
          </p>
        ) : null}

        {/* The chart and the summary stay their own pages (a nurse bookmarks
            the chart), linked from here; the acts on the stay sit beside them. */}
        <nav className="flex flex-wrap items-center gap-2" aria-label={t("stayNav")}>
          <LinkButton variant="outline" size="sm" className="h-tap" href={`/admissions/${admissionId}/chart`}>
            <Pill aria-hidden />
            {t("openChart")}
          </LinkButton>
          <LinkButton variant="outline" size="sm" className="h-tap" href={`/admissions/${admissionId}/summary`}>
            <FileText aria-hidden />
            {t("openSummary")}
          </LinkButton>
          {live && canTransfer ? (
            <Button variant="outline" size="sm" className="h-tap" onClick={() => setTransferring(true)}>
              <ArrowLeftRight aria-hidden />
              {t("transfer")}
            </Button>
          ) : null}
          {live && canDischarge && data.status === "ADMITTED" ? (
            <Button
              variant="outline"
              size="sm"
              className="h-tap"
              disabled={initiate.isPending}
              onClick={() => initiate.mutate()}
            >
              <ClipboardCheck aria-hidden />
              {t("markFitForDischarge")}
            </Button>
          ) : null}
          {live && canDischarge ? (
            <Button size="sm" className="h-tap" onClick={() => setDischarging(true)} data-testid="discharge">
              <LogOut aria-hidden />
              {t("discharge")}
            </Button>
          ) : null}
          {live ? (
            <div className="ml-auto">
              <VisitOutcomeAction
                encounterId={data.encounter_id}
                patientName={data.patient_name ?? ""}
                canRecordDeath={canRecordDeath}
                canRecordReferral={canRecordReferral}
                canRecordLama={canRecordLama}
                currentUserId={currentUserId}
              />
            </div>
          ) : null}
        </nav>
      </header>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="min-w-0 space-y-4">
          {canReadChart ? (
            <DueNowPanel admissionId={admissionId} canAdminister={canAdminister} live={live} />
          ) : null}
          {canReadVitals ? (
            <LatestVitals
              encounterId={data.encounter_id}
              patientName={data.patient_name ?? ""}
              canRecord={canRecordVitals}
              live={live}
            />
          ) : null}
          <ClinicalPanel admission={data} canEdit={editable} />
        </div>
        <div className="min-w-0 space-y-4">
          <StayPanel admission={data} canEdit={editable} />
          {canSeeCost ? <StayCostPanel encounterId={data.encounter_id} /> : null}
          <ItemsUsedPanel
            admissionId={admissionId}
            encounterId={data.encounter_id}
            canIssue={canIssue}
            live={live}
          />
          <DischargeReadiness admission={data} />
        </div>
      </div>

      {transferring ? (
        <TransferDialog
          admissionId={admissionId}
          currentBedId={data.bed?.id ?? null}
          open
          onOpenChange={(next) => {
            if (!next) setTransferring(false);
          }}
          onDone={() => {
            setTransferring(false);
            void admission.refetch();
          }}
        />
      ) : null}

      {discharging ? (
        <DischargeDialog
          admissionId={admissionId}
          patientName={data.patient_name ?? ""}
          open
          onOpenChange={(next) => {
            if (!next) setDischarging(false);
          }}
          onDone={() => {
            setDischarging(false);
            void admission.refetch();
          }}
        />
      ) : null}
    </div>
  );
}

function TransferDialog({
  admissionId,
  currentBedId,
  open,
  onOpenChange,
  onDone,
}: {
  admissionId: string;
  currentBedId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const t = useTranslations("wards");
  const common = useTranslations("common");
  const [bedId, setBedId] = useState("");
  const [reason, setReason] = useState("");

  const board = useQuery({
    queryKey: ["ipd", "board"],
    queryFn: ({ signal }) => api.get<BoardWard[]>("/ipd/wards/board", { signal }),
    staleTime: 5_000,
  });

  const transfer = useMutation({
    mutationFn: () =>
      api.post<Admission>(`/ipd/admissions/${admissionId}/transfer`, {
        to_bed_id: bedId,
        reason: reason.trim() || null,
      }),
    onSuccess: () => {
      toast.success(t("transferred"));
      onDone();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const free = (board.data ?? []).flatMap((ward) =>
    (ward.beds ?? [])
      .filter((bed) => bed.status === "AVAILABLE" && bed.id !== currentBedId)
      .map((bed) => ({ ...bed, wardName: ward.name })),
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("transfer")}</DialogTitle>
          <DialogDescription>{t("transferHint")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="space-y-1.5">
            <Label htmlFor="transfer-bed">{t("toBed")}</Label>
            <Select value={bedId} onValueChange={(value) => setBedId(value ?? "")}>
              <SelectTrigger id="transfer-bed" className="h-tap w-full">
                <SelectValue placeholder={t("chooseBed")} />
              </SelectTrigger>
              <SelectContent>
                {free.map((bed) => (
                  <SelectItem key={bed.id} value={bed.id}>
                    {bed.wardName} · {bed.code}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {!board.isPending && free.length === 0 ? (
              <p className="text-muted-foreground text-xs">{t("noFreeBeds")}</p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="transfer-reason">
              {t("reason")} <span className="text-muted-foreground">({common("optional")})</span>
            </Label>
            <Input
              id="transfer-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              className="h-tap"
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!bedId || transfer.isPending}
            onClick={() => transfer.mutate()}
          >
            {t("transfer")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DischargeDialog({
  admissionId,
  patientName,
  open,
  onOpenChange,
  onDone,
}: {
  admissionId: string;
  patientName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const t = useTranslations("wards");
  const common = useTranslations("common");

  // No default. A default would be RECOVERED, and a system that records a
  // referral as a recovery because nobody changed a dropdown is worse than one
  // that asks — which is exactly why the backend gives this field no default
  // either.
  const [type, setType] = useState("");
  const [condition, setCondition] = useState("");
  const [followUp, setFollowUp] = useState("");

  const discharge = useMutation({
    mutationFn: () =>
      api.post<Admission>(`/ipd/admissions/${admissionId}/discharge`, {
        discharge_type: type,
        condition_at_discharge: condition.trim() || null,
        follow_up_date: followUp || null,
      }),
    onSuccess: () => {
      toast.success(t("discharged"));
      onDone();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("discharge")}</DialogTitle>
          <DialogDescription>{patientName}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="space-y-1.5">
            <Label htmlFor="discharge-type">{t("dischargeType")}</Label>
            <Select value={type} onValueChange={(value) => setType(value ?? "")}>
              <SelectTrigger id="discharge-type" className="h-tap w-full">
                <SelectValue placeholder={t("chooseOutcome")} />
              </SelectTrigger>
              <SelectContent>
                {DISCHARGE_TYPES.map((option) => (
                  <SelectItem key={option} value={option}>
                    {t(`discharge${option}` as "dischargeRECOVERED")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="discharge-condition">
              {t("conditionAtDischarge")}{" "}
              <span className="text-muted-foreground">({common("optional")})</span>
            </Label>
            <Textarea
              id="discharge-condition"
              value={condition}
              onChange={(event) => setCondition(event.target.value)}
              rows={2}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="discharge-followup">
              {t("followUpDate")}{" "}
              <span className="text-muted-foreground">({common("optional")})</span>
            </Label>
            <Input
              id="discharge-followup"
              type="date"
              value={followUp}
              onChange={(event) => setFollowUp(event.target.value)}
              className="h-tap"
            />
          </div>

          <p className="text-muted-foreground text-xs">{t("terminalOutcomesHint")}</p>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!type || discharge.isPending}
            onClick={() => discharge.mutate()}
          >
            {t("discharge")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
