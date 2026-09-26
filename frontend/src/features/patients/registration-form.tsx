"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { ChevronDown, ChevronUp, UserPlus } from "lucide-react";
import { toast } from "sonner";

import { DuplicateWarning } from "@/features/patients/duplicate-warning";
import { TokenSlip } from "@/features/queue/token-slip";
import { useQueueBoard } from "@/features/queue/use-queue-board";
import {
  genderValues,
  registrationFieldNames,
  registrationSchema,
  toApiPayload,
  type RegistrationForm as FormValues,
  type RegistrationPayload,
} from "@/features/patients/schema";
import { FieldError } from "@/components/field-error";
import { Button } from "@/components/ui/button";
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
import { applyFieldErrors } from "@/lib/api/field-errors";
import { useDebounced } from "@/lib/hooks/use-debounced";
import { cn } from "@/lib/utils";
import type {
  Doctor,
  DuplicateCandidate,
  Page,
  QuickOpdResult,
  RegistrationResult,
} from "@/types/api";

/**
 * Rapid registration (CLAUDE.md §7b) — the whole form.
 *
 * The acceptance gate is a registration in under thirty seconds, and every
 * decision here is in service of it:
 *
 * * **Four fields, all visible, no scrolling.** Address, guardian and blood
 *   group are behind a disclosure toggle. They are not required, and a
 *   receptionist should never have to tab past them.
 * * **Gender is three buttons, not a dropdown.** A select costs a click to
 *   open, a movement and a click to choose. Radio buttons cost one click, and
 *   with a keyboard they cost one arrow key.
 * * **The doctor is chosen on this same screen.** Registering and sending a
 *   patient to a doctor is one action with one button, not two screens
 *   (§7b's one-click OPD). Reception almost never registers somebody who is
 *   not about to see somebody.
 * * **Duplicates surface while typing**, not on submit, so the decision
 *   happens before the record exists rather than after.
 *
 * The whole flow is register → quick-OPD, two calls behind one button. They
 * are not one endpoint because registering a patient who then does not get a
 * token is perfectly normal (a pre-admission, a records request), and a
 * combined endpoint would make the common case fast at the cost of making the
 * uncommon one impossible.
 */
export function RegistrationForm() {
  const t = useTranslations("registration");
  const tOpd = useTranslations("quickOpd");
  const tQueue = useTranslations("queue");
  const tPatient = useTranslations("patient");
  const common = useTranslations("common");
  const router = useRouter();

  const [showMore, setShowMore] = useState(false);
  const [duplicates, setDuplicates] = useState<DuplicateCandidate[]>([]);
  const [issued, setIssued] = useState<QuickOpdResult | null>(null);
  const [registeredOnly, setRegisteredOnly] = useState<RegistrationResult | null>(null);
  const [doctorId, setDoctorId] = useState<string>("");

  const form = useForm<FormValues>({
    resolver: zodResolver(registrationSchema),
    // Validate on blur, not on every keystroke: a red error appearing under a
    // phone number while it is still half-typed is noise that teaches people
    // to ignore errors.
    mode: "onBlur",
    defaultValues: {
      full_name: "",
      phone: "",
      gender: undefined,
      age_years: undefined,
      confirm_not_duplicate: false,
    },
  });

  // `useWatch` rather than `form.watch()`: the latter returns a fresh
  // function on every render, which the React Compiler cannot memoise, so it
  // silently skips optimising this whole component. `useWatch` subscribes to
  // just these two fields and re-renders only when they change.
  const name = useWatch({ control: form.control, name: "full_name" });
  const phone = useWatch({ control: form.control, name: "phone" });
  const confirmed = useWatch({
    control: form.control,
    name: "confirm_not_duplicate",
  });
  const debouncedName = useDebounced(name ?? "", 400);
  const debouncedPhone = useDebounced(phone ?? "", 400);

  // Duplicate detection while reception types (§7b).
  const duplicateCheck = useQuery({
    queryKey: ["patients", "duplicates", debouncedName, debouncedPhone],
    // Only once there is enough of both to be meaningful. Checking on a
    // two-letter name returns half the hospital and warns about nothing.
    enabled: debouncedName.trim().length >= 3 && debouncedPhone.replace(/\D/g, "").length >= 6,
    queryFn: ({ signal }) =>
      api.get<DuplicateCandidate[]>("/patients/check-duplicates", {
        query: {
          full_name: debouncedName.trim(),
          phone: debouncedPhone.trim(),
        },
        signal,
      }),
    staleTime: 30_000,
  });

  const doctors = useQuery({
    queryKey: ["doctors", "accepting"],
    queryFn: ({ signal }) =>
      api.get<Page<Doctor>>("/doctors", {
        query: { accepting_only: true, limit: 100 },
        signal,
      }),
    // The list of who is on today changes rarely; a minute of staleness is
    // invisible and saves a request on every registration.
    staleTime: 60_000,
  });

  // Per-doctor waiting counts for the picker below. The same query the
  // reception board uses, so the number here is the number on the board.
  const board = useQueueBoard();
  const waitingByDoctor = new Map(
    (board.data ?? []).map((column) => [column.doctor_id, column.waiting]),
  );

  const register = useMutation({
    mutationFn: async (values: RegistrationPayload) => {
      const patient = await api.post<RegistrationResult>("/patients", toApiPayload(values));

      if (!doctorId) return { patient, opd: null };

      const opd = await api.post<QuickOpdResult>("/queue/quick-opd", {
        patient_id: patient.id,
        doctor_id: doctorId,
      });
      return { patient, opd };
    },
    onSuccess: ({ patient, opd }) => {
      toast.success(t("registered", { name: patient.full_name, uhid: patient.uhid }));
      if (opd) {
        setIssued(opd);
      } else {
        setRegisteredOnly(patient);
      }
    },
    onError: (error) => {
      if (!isApiError(error)) throw error;

      // The backend returns field-level errors in a fixed shape, so they can
      // be attached to the inputs that caused them rather than shown as a
      // banner the user has to map back onto the form themselves. Anything it
      // names that this form does not render still falls through to a toast.
      if (applyFieldErrors(error, form.setError, registrationFieldNames)) return;
      toast.error(error.message);
    },
  });

  const startOver = () => {
    setIssued(null);
    setRegisteredOnly(null);
    setDuplicates([]);
    setDoctorId("");
    form.reset();
    // Focus returns to the first field so the next registration begins with
    // typing rather than with finding the cursor.
    setTimeout(() => form.setFocus("full_name"), 0);
  };

  if (issued) {
    return <TokenSlip result={issued} onRegisterAnother={startOver} />;
  }

  if (registeredOnly) {
    return (
      <div className="mx-auto max-w-lg space-y-4 text-center">
        <p className="text-lg font-medium">
          {t("registered", {
            name: registeredOnly.full_name,
            uhid: registeredOnly.uhid,
          })}
        </p>
        <div className="flex flex-wrap justify-center gap-2">
          <Button onClick={startOver} className="h-tap">
            {t("submit")}
          </Button>
          <Button
            variant="outline"
            className="h-tap"
            onClick={() => router.push(`/patients/${registeredOnly.id}`)}
          >
            {common("next")}
          </Button>
        </div>
      </div>
    );
  }

  const candidates = duplicateCheck.data ?? duplicates;
  // Only an identity collision — same name AND same mobile — holds the form.
  // Blocking on a name resemblance would have staff confirming "different
  // person" for every Sharma and Kumar, which is how a safety check becomes a
  // reflex. See `DuplicateWarning` for the full reasoning.
  const blockedByDuplicate = candidates.some((candidate) => candidate.is_exact) && !confirmed;

  return (
    <form
      onSubmit={form.handleSubmit((values) => register.mutate(values as RegistrationPayload))}
      className="mx-auto max-w-2xl space-y-6"
      noValidate
    >
      <div className="grid gap-5 sm:grid-cols-2">
        <div className="space-y-2 sm:col-span-2">
          <Label htmlFor="full_name">{t("fullName")}</Label>
          <Input
            id="full_name"
            autoFocus
            autoComplete="off"
            // Names are typed from an ID card; the browser's own capitalisation
            // and spellcheck fight Indian names constantly.
            autoCapitalize="words"
            spellCheck={false}
            placeholder={t("fullNamePlaceholder")}
            className="h-tap text-base"
            aria-invalid={Boolean(form.formState.errors.full_name)}
            {...form.register("full_name")}
          />
          <FieldError message={form.formState.errors.full_name?.message} translate={t} />
        </div>

        <div className="space-y-2">
          <Label htmlFor="phone">{t("phone")}</Label>
          <Input
            id="phone"
            type="tel"
            // `numeric` rather than `tel`: on an Android tablet the tel pad
            // shows +*# keys nobody needs and hides the digits behind them.
            inputMode="numeric"
            autoComplete="off"
            placeholder={t("phonePlaceholder")}
            className="h-tap text-base tabular"
            aria-invalid={Boolean(form.formState.errors.phone)}
            {...form.register("phone")}
          />
          <FieldError message={form.formState.errors.phone?.message} translate={t} />
        </div>

        <div className="space-y-2">
          <Label htmlFor="age_years">{t("age")}</Label>
          <Input
            id="age_years"
            type="number"
            inputMode="numeric"
            min={0}
            max={130}
            placeholder={t("agePlaceholder")}
            className="h-tap text-base tabular"
            aria-invalid={Boolean(form.formState.errors.age_years)}
            {...form.register("age_years")}
          />
          <FieldError message={form.formState.errors.age_years?.message} translate={t} />
        </div>

        <fieldset className="space-y-2 sm:col-span-2">
          <legend className="mb-2 text-sm font-medium">{t("gender")}</legend>
          <div className="flex flex-wrap gap-2">
            {genderValues.map((value) => (
              <label
                key={value}
                className={cn(
                  "min-h-tap flex flex-1 cursor-pointer items-center justify-center rounded-lg border px-4 text-sm font-medium transition-colors",
                  "has-[:checked]:border-primary has-[:checked]:bg-primary/10 has-[:checked]:text-primary",
                  "has-[:focus-visible]:outline-ring has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2",
                )}
              >
                <input
                  type="radio"
                  value={value}
                  className="sr-only"
                  {...form.register("gender")}
                />
                {tPatient(`gender${value}` as "genderMALE")}
              </label>
            ))}
          </div>
          <FieldError message={form.formState.errors.gender?.message} translate={t} />
        </fieldset>
      </div>

      {candidates.length > 0 ? (
        <DuplicateWarning
          candidates={candidates}
          confirmed={Boolean(confirmed)}
          onConfirm={(value) => {
            form.setValue("confirm_not_duplicate", value);
            setDuplicates(candidates);
          }}
        />
      ) : null}

      {/* Progressive disclosure: the fields that do not gate the queue. */}
      <div className="rounded-lg border">
        <button
          type="button"
          onClick={() => setShowMore((previous) => !previous)}
          className="min-h-tap flex w-full items-center justify-between px-4 text-sm font-medium"
          aria-expanded={showMore}
        >
          {showMore ? t("fewerDetails") : t("moreDetails")}
          {showMore ? (
            <ChevronUp aria-hidden className="size-4" />
          ) : (
            <ChevronDown aria-hidden className="size-4" />
          )}
        </button>

        {showMore ? (
          <div className="grid gap-4 border-t p-4 sm:grid-cols-2">
            <OptionalField id="guardian_name" label={t("guardianName")} form={form} />
            <OptionalField id="guardian_relation" label={t("guardianRelation")} form={form} />
            <OptionalField id="guardian_phone" label={t("guardianPhone")} form={form} />
            <OptionalField id="address_line1" label={t("addressLine1")} form={form} />
            <OptionalField id="city" label={t("city")} form={form} />
            <OptionalField id="state" label={t("state")} form={form} />
            <OptionalField id="pincode" label={t("pincode")} form={form} />
          </div>
        ) : null}
      </div>

      <div className="space-y-2">
        <Label htmlFor="doctor">{tOpd("selectDoctor")}</Label>
        {/* Base UI hands back `null` when a select is cleared; the empty
            string is what "no doctor chosen" means to the submit handler. */}
        <Select value={doctorId} onValueChange={(value) => setDoctorId(value ?? "")}>
          <SelectTrigger id="doctor" className="h-tap w-full text-base">
            <SelectValue placeholder={tOpd("selectDoctorPlaceholder")}>
              {(value: string | null) =>
                doctors.data?.items.find((doctor) => doctor.id === value)?.display_name ??
                tOpd("selectDoctorPlaceholder")
              }
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {doctors.data?.items.map((doctor) => {
              const waiting = waitingByDoctor.get(doctor.id);
              return (
                <SelectItem key={doctor.id} value={doctor.id}>
                  {doctor.display_name}
                  {doctor.specialty ? ` · ${doctor.specialty}` : ""}
                  {/* The load, right where the choice is made. A receptionist
                      who can see "0 waiting" next to one name and "11" next to
                      another does not need a policy to balance the clinics. */}
                  {waiting !== undefined ? ` · ${tQueue("waitingCount", { count: waiting })}` : ""}
                </SelectItem>
              );
            })}
          </SelectContent>
        </Select>
        <p className="text-muted-foreground text-xs">{tOpd("subtitle")}</p>
      </div>

      <Button
        type="submit"
        // Sized and placed to be the obvious end of the form. On a counter
        // touchscreen the primary action must be hittable without aiming.
        className="h-tap w-full text-base"
        disabled={register.isPending || blockedByDuplicate}
      >
        {register.isPending ? (
          t("submitting")
        ) : (
          <>
            <UserPlus aria-hidden className="size-4" />
            {doctorId ? t("submitAndQueue") : t("submit")}
          </>
        )}
      </Button>
    </form>
  );
}

function OptionalField({
  id,
  label,
  form,
}: {
  id: keyof FormValues;
  label: string;
  form: ReturnType<typeof useForm<FormValues>>;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} className="h-tap" autoComplete="off" {...form.register(id)} />
    </div>
  );
}
