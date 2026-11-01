"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Plus } from "lucide-react";
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
import { formatMoney } from "@/features/billing/money";
import { DoseRow } from "@/features/chart/dose-row";
import { MedicinePicker } from "@/features/medicines/medicine-picker";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type {
  Admission,
  Dose,
  DrugSchedule,
  MedicationOrder,
  MedicationRoute,
  MedicineItem,
  Page,
} from "@/types/api";

/** One allergy named by an `allergy_conflict` refusal (SAFE-01). */
type AllergyClash = { id: string; label: string; severity: string; detail: string | null };

/**
 * Bring an element into view once, when it first renders. Module-level so its
 * identity is stable: React calls a ref callback again whenever it changes.
 */
function revealOnMount(node: HTMLElement | null) {
  node?.scrollIntoView({ block: "nearest" });
}

const ROUTES: MedicationRoute[] = [
  "ORAL",
  "IV",
  "IM",
  "SC",
  "INHALED",
  "TOPICAL",
  "NASOGASTRIC",
  "RECTAL",
  "OPHTHALMIC",
  "OTHER",
];

const SCHEDULES: DrugSchedule[] = ["NONE", "H", "H1", "X", "NARCOTIC"];

/**
 * One patient's medication chart.
 *
 * The separation on this screen is a patient-safety one and the RBAC enforces
 * it: a **doctor prescribes and does not sign for a dose at the bedside**, a
 * **nurse gives the drug and does not prescribe it**. That is the second pair
 * of eyes, and it is most of what a medication chart is for. The screen shows
 * both halves to both people — a nurse must be able to read the prescription
 * — and offers each only the actions their role holds.
 *
 * Stopping a drug is a prescriber's act and needs a reason, because a drug
 * that vanishes from a chart with no explanation is one a covering doctor
 * restarts.
 *
 * A drug picked from the medicine list is billed per dose given, and the chart
 * says so beside it — as it says "not billed" beside one typed free-hand, so
 * the gap is visible to the one person who can close it.
 */
export function ChartScreen({
  admission,
  initialOrders,
  initialDoses,
  canPrescribe,
  canAdminister,
  canPickMedicine = false,
}: {
  admission: Admission;
  initialOrders: Page<MedicationOrder>;
  initialDoses: Page<Dose>;
  canPrescribe: boolean;
  canAdminister: boolean;
  /** Offer the medicine list when prescribing (`medicine:read`). */
  canPickMedicine?: boolean;
}) {
  const t = useTranslations("chart");
  const common = useTranslations("common");
  const queryClient = useQueryClient();

  const [prescribing, setPrescribing] = useState(false);
  const [stopping, setStopping] = useState<MedicationOrder | null>(null);

  const orders = useQuery({
    queryKey: ["ipd", "medications", admission.id],
    queryFn: ({ signal }) =>
      api.get<Page<MedicationOrder>>(`/ipd/admissions/${admission.id}/medications`, {
        query: { limit: 100 },
        signal,
      }),
    initialData: initialOrders,
    staleTime: 10_000,
  });

  const doses = useQuery({
    queryKey: ["ipd", "doses", admission.id],
    queryFn: ({ signal }) =>
      api.get<Page<Dose>>("/ipd/doses", {
        query: { admission_id: admission.id, limit: 100 },
        signal,
      }),
    initialData: initialDoses,
    refetchInterval: 15_000,
    staleTime: 0,
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["ipd", "medications", admission.id] });
    void queryClient.invalidateQueries({ queryKey: ["ipd", "doses", admission.id] });
  };

  const drugs = orders.data?.items ?? [];
  const slots = doses.data?.items ?? [];

  return (
    <div className="space-y-6">
      <header
        className={cn(
          "rounded-lg border p-4",
          admission.patient_is_deceased ? "border-foreground/40 bg-foreground/5" : "bg-muted/30",
        )}
      >
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">
              {admission.patient_name ?? common("notRecorded")}
            </h1>
            <p className="text-muted-foreground tabular text-sm">
              {admission.uhid}
              {admission.patient_age_years !== null && admission.patient_age_years !== undefined
                ? ` · ${admission.patient_age_years}`
                : ""}
              {admission.patient_gender ? ` · ${admission.patient_gender}` : ""}
            </p>
          </div>
          <div className="text-right">
            <div className="font-medium">
              {admission.ward?.name ?? "—"} · {admission.bed?.code ?? "—"}
            </div>
            <Link
              href={`/admissions/${admission.id}`}
              className="text-muted-foreground text-xs hover:underline"
            >
              {admission.admission_number}
            </Link>
          </div>
        </div>
      </header>

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="drugs-heading">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="drugs-heading" className="text-sm font-semibold">
            {t("prescribedDrugs")}
          </h2>
          {canPrescribe ? (
            <Button size="sm" className="h-tap" onClick={() => setPrescribing(true)}>
              <Plus aria-hidden className="size-4" />
              {t("prescribe")}
            </Button>
          ) : null}
        </div>

        {drugs.length === 0 ? (
          <p className="text-muted-foreground text-sm">{t("noDrugs")}</p>
        ) : (
          <ul className="divide-border divide-y">
            {drugs.map((order) => (
              <li
                key={order.id}
                className="flex flex-wrap items-center justify-between gap-3 py-3"
                data-testid="drug-row"
              >
                <div className="min-w-0">
                  <div className="font-medium">
                    {order.drug_name} <span className="text-muted-foreground">{order.dose}</span>
                  </div>
                  <div className="text-muted-foreground text-xs">
                    {order.route} · {order.frequency}
                    {order.is_prn ? ` · ${t("asNeeded")}` : ""}
                    {order.drug_schedule !== "NONE" ? (
                      // Schedule H/H1/X matter legally (CLAUDE.md §9), so they
                      // are stated on the chart rather than buried.
                      <span className="text-caution-foreground ml-1.5 font-medium">
                        {t("schedule", { schedule: order.drug_schedule })}
                      </span>
                    ) : null}
                  </div>
                  {order.instructions ? (
                    <div className="text-muted-foreground mt-0.5 text-xs italic">
                      {order.instructions}
                    </div>
                  ) : null}
                  {(order.allergy_conflicts ?? []).length > 0 ? (
                    // SAFE-01: the clash stays visible on the chart for as
                    // long as the drug is on it, with the doctor's reason.
                    <div
                      className="text-critical mt-0.5 text-xs font-medium"
                      data-testid="order-allergy"
                    >
                      {t("allergyClash", { allergies: (order.allergy_conflicts ?? []).join(", ") })}
                      {order.allergy_override_reason
                        ? ` · ${t("overrideReason", { reason: order.allergy_override_reason })}`
                        : ` · ${t("notReviewed")}`}
                    </div>
                  ) : null}
                  <BillingLine order={order} />
                  {!order.is_active ? (
                    <div className="text-muted-foreground mt-0.5 text-xs">
                      {t("stopped", { reason: order.stop_reason ?? "" })}
                    </div>
                  ) : null}
                </div>

                {canPrescribe && order.is_active ? (
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-tap"
                    onClick={() => setStopping(order)}
                  >
                    {t("stop")}
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="doses-heading">
        <h2 id="doses-heading" className="text-sm font-semibold">
          {t("doses")}
        </h2>

        {slots.length === 0 ? (
          <p className="text-muted-foreground text-sm">{t("noDosesForPatient")}</p>
        ) : (
          <ul className="space-y-2">
            {slots.map((dose) => (
              <DoseRow
                key={dose.id}
                dose={dose}
                // The patient's own chart already names them at the top.
                showPatient={false}
                canAdminister={canAdminister}
                onSigned={refresh}
              />
            ))}
          </ul>
        )}
      </section>

      <PrescribeDialog
        admissionId={admission.id}
        canPickMedicine={canPickMedicine}
        open={prescribing}
        onOpenChange={setPrescribing}
        onDone={refresh}
      />

      {stopping ? (
        <StopDialog
          order={stopping}
          open
          onOpenChange={(next) => {
            if (!next) setStopping(null);
          }}
          onDone={() => {
            setStopping(null);
            refresh();
          }}
        />
      ) : null}
    </div>
  );
}

/** "₹85.00 a vial · 1 vial a dose", or "Not billed" for a free-typed drug. */
function BillingLine({ order }: { order: MedicationOrder }) {
  const t = useTranslations("chart");
  if (!order.item_code) {
    return (
      <div className="text-muted-foreground mt-0.5 text-xs" data-testid="billing-line">
        {t("notBilled")}
      </div>
    );
  }
  const units = Number(order.units_per_dose);
  return (
    <div className="text-muted-foreground tabular mt-0.5 text-xs" data-testid="billing-line">
      {order.unit_price !== null && order.unit_price !== undefined
        ? t("billedPerDose", {
            price: formatMoney(order.unit_price),
            unit: order.unit ?? "",
            units: Number.isInteger(units) ? String(units) : order.units_per_dose,
          })
        : t("billedUnpriced")}
    </div>
  );
}

function PrescribeDialog({
  admissionId,
  canPickMedicine,
  open,
  onOpenChange,
  onDone,
}: {
  admissionId: string;
  canPickMedicine: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const t = useTranslations("chart");
  const common = useTranslations("common");

  // Picked from the medicine list: fills the fields in, and bills each dose.
  const [item, setItem] = useState<MedicineItem | null>(null);
  const [unitsPerDose, setUnitsPerDose] = useState("1");

  const [drug, setDrug] = useState("");
  const [dose, setDose] = useState("");
  const [route, setRoute] = useState<MedicationRoute>("ORAL");
  const [frequency, setFrequency] = useState("TDS");
  const [timesPerDay, setTimesPerDay] = useState("3");
  const [schedule, setSchedule] = useState<DrugSchedule>("NONE");
  const [isPrn, setIsPrn] = useState(false);
  const [prnIndication, setPrnIndication] = useState("");
  const [instructions, setInstructions] = useState("");
  // SAFE-01: the allergies the server said this drug clashes with, and the
  // doctor's reason for prescribing it anyway. Null until there is a clash.
  const [clash, setClash] = useState<AllergyClash[] | null>(null);
  const [overrideReason, setOverrideReason] = useState("");

  const prescribe = useMutation({
    mutationFn: () =>
      api.post<MedicationOrder>(`/ipd/admissions/${admissionId}/medications`, {
        drug_name: drug.trim(),
        dose: dose.trim(),
        route,
        frequency: frequency.trim(),
        // A PRN drug has no schedule to materialise, which is what
        // `times_per_day: 0` means to the backend.
        times_per_day: isPrn ? 0 : Number(timesPerDay),
        is_prn: isPrn,
        prn_indication: isPrn ? prnIndication.trim() : null,
        drug_schedule: schedule,
        instructions: instructions.trim() || null,
        medicine_item_id: item?.id ?? null,
        units_per_dose: item ? unitsPerDose.trim() || "1" : "1",
        allergy_override_reason: clash ? overrideReason.trim() : null,
      }),
    onSuccess: (order) => {
      toast.success(t("prescribed", { drug: order.drug_name }));
      setClash(null);
      setOverrideReason("");
      setItem(null);
      setUnitsPerDose("1");
      setDrug("");
      setDose("");
      setInstructions("");
      setPrnIndication("");
      onOpenChange(false);
      onDone();
    },
    onError: (error) => {
      if (isApiError(error) && error.code === "allergy_conflict") {
        // Not a toast: the doctor has to read it, and answer it, here.
        setClash((error.details?.allergies as AllergyClash[] | undefined) ?? []);
        return;
      }
      toast.error(isApiError(error) ? error.message : String(error));
    },
  });

  // Changing the drug answers a different question: the clash shown was for
  // the old one.
  const changeDrug = (next: string) => {
    setDrug(next);
    setClash(null);
    setOverrideReason("");
  };

  const pick = (next: MedicineItem | null) => {
    setClash(null);
    setOverrideReason("");
    setItem(next);
    if (!next) return;
    setDrug(next.name);
    if (next.dose) setDose(next.dose);
    if (next.route) setRoute(next.route);
    setSchedule(next.drug_schedule);
    setUnitsPerDose("1");
  };

  const unitsValid = !item || (Number(unitsPerDose) > 0 && Number(unitsPerDose) <= 100);
  const valid =
    drug.trim() &&
    dose.trim() &&
    frequency.trim() &&
    unitsValid &&
    (isPrn ? prnIndication.trim().length > 0 : Number(timesPerDay) >= 1);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("prescribe")}</DialogTitle>
          <DialogDescription>{t("prescribeHint")}</DialogDescription>
        </DialogHeader>

        <div className="max-h-[65vh] space-y-3 overflow-y-auto text-left">
          {canPickMedicine ? (
            <div className="space-y-2">
              <MedicinePicker
                label={t("fromMedicineList")}
                value={item}
                onChange={pick}
                autoFocus
              />
              {item ? (
                <div className="space-y-1.5">
                  <Label htmlFor="drug-units">
                    {t("unitsPerDose", { unit: item.unit })}
                  </Label>
                  <Input
                    id="drug-units"
                    value={unitsPerDose}
                    onChange={(event) => setUnitsPerDose(event.target.value)}
                    inputMode="decimal"
                    className="h-tap tabular w-28"
                  />
                  <p className="text-muted-foreground text-xs">{t("unitsPerDoseHint")}</p>
                </div>
              ) : (
                <p className="text-muted-foreground text-xs">{t("freeTypedHint")}</p>
              )}
            </div>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="drug-name">{t("drug")}</Label>
              <Input
                id="drug-name"
                value={drug}
                onChange={(event) => changeDrug(event.target.value)}
                className="h-tap"
                autoFocus={!canPickMedicine}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="drug-dose">{t("dose")}</Label>
              <Input
                id="drug-dose"
                value={dose}
                onChange={(event) => setDose(event.target.value)}
                className="h-tap"
                placeholder="500 mg"
              />
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="drug-route">{t("route")}</Label>
              <Select
                value={route}
                onValueChange={(value) => setRoute((value ?? "ORAL") as MedicationRoute)}
              >
                <SelectTrigger id="drug-route" className="h-tap w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ROUTES.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="drug-frequency">{t("frequency")}</Label>
              <Input
                id="drug-frequency"
                value={frequency}
                onChange={(event) => setFrequency(event.target.value)}
                className="h-tap"
                placeholder="TDS"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="drug-times">{t("timesPerDay")}</Label>
              <Input
                id="drug-times"
                value={timesPerDay}
                onChange={(event) => setTimesPerDay(event.target.value)}
                inputMode="numeric"
                disabled={isPrn}
                className="h-tap tabular"
              />
            </div>
          </div>

          <label className="flex min-h-tap cursor-pointer items-start gap-2.5 text-sm">
            <input
              type="checkbox"
              checked={isPrn}
              onChange={(event) => setIsPrn(event.target.checked)}
              className="accent-primary mt-3 size-4 shrink-0"
            />
            <span className="py-2.5">
              {t("asNeeded")}
              <span className="text-muted-foreground block text-xs">{t("asNeededHint")}</span>
            </span>
          </label>

          {isPrn ? (
            <div className="space-y-1.5">
              <Label htmlFor="drug-indication">{t("prnIndication")}</Label>
              <Input
                id="drug-indication"
                value={prnIndication}
                onChange={(event) => setPrnIndication(event.target.value)}
                className="h-tap"
                placeholder={t("prnIndicationPlaceholder")}
              />
            </div>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="drug-schedule">{t("drugSchedule")}</Label>
              <Select
                value={schedule}
                onValueChange={(value) => setSchedule((value ?? "NONE") as DrugSchedule)}
              >
                <SelectTrigger id="drug-schedule" className="h-tap w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SCHEDULES.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-muted-foreground text-xs">{t("drugScheduleHint")}</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="drug-instructions">
                {t("instructions")}{" "}
                <span className="text-muted-foreground">({common("optional")})</span>
              </Label>
              <Input
                id="drug-instructions"
                value={instructions}
                onChange={(event) => setInstructions(event.target.value)}
                className="h-tap"
              />
            </div>
          </div>

          {clash ? (
            <div
              role="alert"
              ref={revealOnMount}
              className="border-critical/50 bg-critical/10 space-y-2 rounded-lg border p-3 text-left"
              data-testid="allergy-conflict"
            >
              <p className="text-critical text-sm font-semibold">{t("allergyConflictTitle")}</p>
              <ul className="text-critical list-disc pl-5 text-sm">
                {clash.map((allergy) => (
                  <li key={allergy.id}>
                    {allergy.label}
                    {allergy.severity === "CRITICAL" ? ` · ${t("critical")}` : ""}
                    {allergy.detail ? ` — ${allergy.detail}` : ""}
                  </li>
                ))}
              </ul>
              <p className="text-sm">{t("allergyConflictHint")}</p>
              <Label htmlFor="allergy-override">{t("overrideLabel")}</Label>
              <Input
                id="allergy-override"
                autoFocus
                value={overrideReason}
                onChange={(event) => setOverrideReason(event.target.value)}
                className="h-tap"
                maxLength={300}
                placeholder={t("overridePlaceholder")}
              />
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className={cn("h-tap", clash && "bg-critical hover:bg-critical/90 text-white")}
            // The server wants at least ten characters: a reason, not "ok".
            disabled={
              !valid || prescribe.isPending || (clash !== null && overrideReason.trim().length < 10)
            }
            onClick={() => prescribe.mutate()}
            data-testid="prescribe-submit"
          >
            {clash ? t("prescribeAnyway") : t("prescribe")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function StopDialog({
  order,
  open,
  onOpenChange,
  onDone,
}: {
  order: MedicationOrder;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const t = useTranslations("chart");
  const common = useTranslations("common");
  const [reason, setReason] = useState("");

  const stop = useMutation({
    mutationFn: () =>
      api.post<MedicationOrder>(`/ipd/medications/${order.id}/stop`, {
        reason: reason.trim(),
      }),
    onSuccess: () => {
      toast.success(t("stopped", { reason: reason.trim() }));
      onDone();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("stop")}</DialogTitle>
          <DialogDescription>
            {order.drug_name} {order.dose}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5 text-left">
          <Label htmlFor="stop-reason">{t("reason")}</Label>
          <Input
            id="stop-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            className="h-tap"
            autoFocus
          />
          {/* A drug that vanishes from a chart with no explanation is one a
              covering doctor restarts. */}
          <p className="text-muted-foreground text-xs">{t("stopReasonHint")}</p>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={reason.trim().length === 0 || stop.isPending}
            onClick={() => stop.mutate()}
          >
            {t("stop")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
