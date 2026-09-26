"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";

import { FieldError } from "@/components/field-error";
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
  emptyVitals,
  hasAnyVital,
  toVitalsPayload,
  vitalsFieldNames,
  vitalsSchema,
  type VitalsForm,
} from "@/features/vitals/schema";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { applyFieldErrors } from "@/lib/api/field-errors";
import type { Vitals } from "@/types/api";

/**
 * Recording vitals from the queue — the nurse's quick action (CLAUDE.md §7b,
 * fifteen seconds).
 *
 * A dialog on the queue row rather than a screen of its own, because that is
 * the whole difference between fifteen seconds and forty: the nurse is already
 * looking at the list of people waiting, and navigating away means finding
 * their place in it again afterwards.
 *
 * **Every field is optional, and that is deliberate.** The backend allows it
 * (`VitalsCreate`) for a reason worth restating here: demanding a complete set
 * teaches staff to type zeros, and a fabricated respiratory rate is worse than
 * a missing one — it looks like a measurement.
 *
 * Validation is inline and local (`schema.ts`) so a transposed cuff reading or
 * a glucose value in the wrong unit is caught under the input that caused it,
 * before any request is sent. The backend re-checks everything and its verdict
 * still wins; anything only it can know — a visit closed while the dialog was
 * open — comes back through `applyFieldErrors` or a toast.
 */
type Field = {
  key: keyof VitalsForm;
  labelKey: string;
  unit?: string;
  inputMode: "decimal" | "numeric";
};

// Ordered as they are taken, not alphabetically — the form should follow the
// nurse's hands.
const FIELDS: Field[] = [
  { key: "temperature_c", labelKey: "temperature", unit: "°C", inputMode: "decimal" },
  { key: "pulse_bpm", labelKey: "pulse", unit: "bpm", inputMode: "numeric" },
  { key: "systolic_bp", labelKey: "systolic", unit: "mmHg", inputMode: "numeric" },
  { key: "diastolic_bp", labelKey: "diastolic", unit: "mmHg", inputMode: "numeric" },
  { key: "spo2_percent", labelKey: "spo2", unit: "%", inputMode: "numeric" },
  { key: "respiratory_rate", labelKey: "respiratoryRate", unit: "/min", inputMode: "numeric" },
  { key: "weight_kg", labelKey: "weight", unit: "kg", inputMode: "decimal" },
  { key: "blood_glucose_mgdl", labelKey: "glucose", unit: "mg/dL", inputMode: "numeric" },
];

export function VitalsDialog({
  encounterId,
  patientName,
  open,
  onOpenChange,
  onRecorded,
}: {
  encounterId: string;
  patientName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRecorded?: () => void;
}) {
  const t = useTranslations("vitals");
  const common = useTranslations("common");

  const form = useForm<VitalsForm>({
    resolver: zodResolver(vitalsSchema),
    // On blur, not on every keystroke: an error appearing under a systolic
    // reading while it is still half-typed ("7" of "70") is noise, and noise
    // teaches people to ignore errors.
    mode: "onBlur",
    defaultValues: emptyVitals,
  });

  // `useWatch` rather than `form.watch()` — the latter returns a fresh function
  // each render, which stops the React Compiler memoising this component.
  const values = useWatch({ control: form.control, defaultValue: emptyVitals }) as VitalsForm;

  // The BP pair is checked as the nurse types rather than on blur like
  // everything else, because it is the one rule about a field they have not
  // reached yet: the message has to say "you still owe me the other half"
  // while the cursor is in the first box. The Zod schema carries the same rule
  // for the keyboard path (Enter submits past a disabled button).
  const bothOrNeitherBp =
    (values.systolic_bp.trim() === "") === (values.diastolic_bp.trim() === "");

  const record = useMutation({
    mutationFn: (payload: VitalsForm) =>
      api.post<Vitals>(`/encounters/${encounterId}/vitals`, toVitalsPayload(payload)),
    onSuccess: (vitals) => {
      toast.success(vitals.is_abnormal ? t("savedAbnormal") : t("saved"));
      form.reset(emptyVitals);
      onOpenChange(false);
      onRecorded?.();
    },
    onError: (error) => {
      if (!isApiError(error)) throw error;

      // The client schema mirrors the backend's, so reaching here usually means
      // something only the server knows. Bind whatever it named to an input,
      // and fall back to a toast for anything this form does not render.
      if (applyFieldErrors(error, form.setError, vitalsFieldNames)) return;
      toast.error(error.message);
    },
  });

  const close = (next: boolean) => {
    // Discard a half-typed draft on close, so the next patient does not inherit
    // the previous one's readings.
    if (!next) form.reset(emptyVitals);
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{patientName}</DialogDescription>
        </DialogHeader>

        <form onSubmit={form.handleSubmit((payload) => record.mutate(payload))} noValidate>
          <div className="grid grid-cols-2 gap-3 text-left">
            {FIELDS.map((field) => {
              const message = form.formState.errors[field.key]?.message;
              return (
                <div key={field.key} className="space-y-1.5">
                  <Label htmlFor={`vitals-${field.key}`}>
                    {t(field.labelKey as "temperature")}
                    {field.unit ? (
                      <span className="text-muted-foreground"> ({field.unit})</span>
                    ) : null}
                  </Label>
                  <Input
                    id={`vitals-${field.key}`}
                    inputMode={field.inputMode}
                    className="h-tap tabular"
                    aria-invalid={Boolean(message)}
                    aria-describedby={message ? `vitals-${field.key}-error` : undefined}
                    {...form.register(field.key)}
                  />
                  <FieldError
                    id={`vitals-${field.key}-error`}
                    message={message}
                    translate={t as (key: string) => string}
                  />
                </div>
              );
            })}
          </div>

          {!bothOrNeitherBp ? (
            <p role="alert" className="text-caution-foreground mt-3 text-sm">
              {t("bloodPressurePair")}
            </p>
          ) : null}

          <DialogFooter className="mt-4">
            <Button
              type="button"
              variant="outline"
              className="h-tap"
              onClick={() => close(false)}
            >
              {common("cancel")}
            </Button>
            {/* Disabled on an empty form and on a half-recorded blood
                pressure — never on the other rules, because a dead button
                with the reason hidden behind a blur is the thing this change
                exists to remove. Submitting reveals those instead. */}
            <Button
              type="submit"
              className="h-tap"
              disabled={!hasAnyVital(values) || !bothOrNeitherBp || record.isPending}
            >
              {common("save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
