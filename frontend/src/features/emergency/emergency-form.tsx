"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Siren } from "lucide-react";
import { toast } from "sonner";

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
import { ADMISSION_REQUESTS_KEY } from "@/features/admissions/use-admission-requests";
import { TokenSlip } from "@/features/queue/token-slip";
import { useQueueBoard } from "@/features/queue/use-queue-board";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Doctor, EmergencyResult, Gender, Page } from "@/types/api";

const GENDERS = ["MALE", "FEMALE", "UNKNOWN"] as const satisfies readonly Gender[];

// The doctor on casualty duty changes by shift, not by patient. Remembered on
// this device so the second arrival of the night is one tap fewer.
const DOCTOR_KEY = "hms.emergency-doctor";

function rememberedDoctor(): string {
  try {
    return window.localStorage.getItem(DOCTOR_KEY) ?? "";
  } catch {
    return "";
  }
}

function rememberDoctor(doctorId: string) {
  try {
    window.localStorage.setItem(DOCTOR_KEY, doctorId);
  } catch {
    // Private window or blocked storage: the next arrival picks again.
  }
}

/**
 * The casualty door (UX-01): a patient nobody can name.
 *
 * An unconscious road-accident patient has no name and no mobile. The four-
 * field registration would make staff invent both — breaking duplicate
 * detection and messaging a stranger — so this asks for only what can be seen:
 * gender (or "can't tell"), a guessed age, and which doctor. The server
 * registers `Unknown Male EM-0034`, puts them first in that doctor's line with
 * an EMERGENCY chart, and optionally on the admission desk's list — all in one
 * transaction. Who they are is recorded later, from the "Needs identification"
 * list on Reception.
 */
export function EmergencyForm({ canRequestAdmission }: { canRequestAdmission: boolean }) {
  const t = useTranslations("emergency");
  const tOpd = useTranslations("quickOpd");
  const tQueue = useTranslations("queue");
  const tPatient = useTranslations("patient");
  const queryClient = useQueryClient();

  const [gender, setGender] = useState<Gender | null>(null);
  const [age, setAge] = useState("");
  const [complaint, setComplaint] = useState("");
  const [picked, setDoctorId] = useState<string | null>(null);
  const [toAdmission, setToAdmission] = useState(false);
  const [issued, setIssued] = useState<EmergencyResult | null>(null);

  const doctors = useQuery({
    queryKey: ["doctors", "accepting"],
    queryFn: ({ signal }) =>
      api.get<Page<Doctor>>("/doctors", {
        query: { accepting_only: true, limit: 100 },
        signal,
      }),
    staleTime: 60_000,
  });
  const board = useQueueBoard();
  const waitingByDoctor = new Map(
    (board.data ?? []).map((column) => [column.doctor_id, column.waiting]),
  );

  // Preselect the remembered doctor, but only one who is still accepting —
  // a doctor gone off shift must not silently receive the next emergency.
  // Read once the list has loaded, which is never during server rendering.
  const doctorList = doctors.data?.items;
  const remembered = picked === null && doctorList ? rememberedDoctor() : "";
  const doctorId =
    picked ?? (doctorList?.some((doctor) => doctor.id === remembered) ? remembered : "");

  const ageNumber = age.trim() === "" ? null : Number(age);
  const ageValid = ageNumber === null || (Number.isInteger(ageNumber) && ageNumber >= 0 && ageNumber <= 130);

  const register = useMutation({
    mutationFn: () =>
      api.post<EmergencyResult>("/queue/emergency", {
        gender,
        age_years: ageNumber,
        doctor_id: doctorId,
        complaint: complaint.trim() || null,
        send_to_admission: toAdmission,
      }),
    onSuccess: (result) => {
      rememberDoctor(doctorId);
      toast.success(t("registered", { name: result.patient_name, uhid: result.uhid }));
      void queryClient.invalidateQueries({ queryKey: ["queue"] });
      void queryClient.invalidateQueries({ queryKey: ["patients"] });
      void queryClient.invalidateQueries({ queryKey: ADMISSION_REQUESTS_KEY });
      setIssued(result);
    },
    onError: (error) => {
      if (!isApiError(error)) throw error;
      toast.error(error.message);
    },
  });

  const startOver = () => {
    setIssued(null);
    setGender(null);
    setAge("");
    setComplaint("");
    setToAdmission(false);
  };

  if (issued) {
    return (
      <div className="space-y-4">
        <p
          className="border-destructive/40 bg-destructive/10 text-destructive mx-auto max-w-md rounded-lg border px-4 py-3 text-center text-sm font-medium no-print"
          data-testid="emergency-issued"
        >
          {t("issued", { tag: issued.emergency_tag })}
          {issued.admission_request_id ? ` ${t("sentToAdmission")}` : ""}
        </p>
        <TokenSlip result={issued} onRegisterAnother={startOver} />
      </div>
    );
  }

  const ready = gender !== null && doctorId !== "" && ageValid;

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (ready && !register.isPending) register.mutate();
      }}
      className="mx-auto max-w-2xl space-y-6"
      noValidate
    >
      <fieldset className="space-y-2">
        <legend className="mb-2 text-sm font-medium">{t("gender")}</legend>
        {/* Three big buttons: on a casualty tablet with gloves on, a select
            is two aimed taps; this is one. */}
        <div className="grid grid-cols-3 gap-2">
          {GENDERS.map((value, index) => (
            <button
              key={value}
              type="button"
              autoFocus={index === 0}
              aria-pressed={gender === value}
              onClick={() => setGender(value)}
              className={cn(
                "min-h-tap rounded-lg border px-3 py-4 text-base font-medium transition-colors",
                "focus-visible:outline-ring focus-visible:outline-2 focus-visible:outline-offset-2",
                gender === value
                  ? "border-destructive bg-destructive/10 text-destructive"
                  : "hover:bg-muted",
              )}
              data-testid={`emergency-gender-${value}`}
            >
              {value === "UNKNOWN" ? t("genderUnknown") : tPatient(`gender${value}`)}
            </button>
          ))}
        </div>
      </fieldset>

      <div className="grid gap-5 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="emergency-age">
            {t("age")}{" "}
            <span className="text-muted-foreground font-normal">({t("ageHint")})</span>
          </Label>
          <Input
            id="emergency-age"
            type="number"
            inputMode="numeric"
            min={0}
            max={130}
            className="h-tap tabular text-base"
            value={age}
            aria-invalid={!ageValid}
            onChange={(event) => setAge(event.target.value)}
          />
          {!ageValid ? (
            <p className="text-destructive text-xs">{t("ageInvalid")}</p>
          ) : null}
        </div>

        <div className="space-y-2">
          <Label htmlFor="emergency-doctor">{tOpd("selectDoctor")}</Label>
          <Select value={doctorId} onValueChange={(value) => setDoctorId(value ?? "")}>
            <SelectTrigger id="emergency-doctor" className="h-tap w-full text-base">
              <SelectValue placeholder={tOpd("selectDoctorPlaceholder")}>
                {(value: string | null) =>
                  doctorList?.find((doctor) => doctor.id === value)?.display_name ??
                  tOpd("selectDoctorPlaceholder")
                }
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {doctorList?.map((doctor) => {
                const waiting = waitingByDoctor.get(doctor.id);
                return (
                  <SelectItem key={doctor.id} value={doctor.id}>
                    {doctor.display_name}
                    {doctor.specialty ? ` · ${doctor.specialty}` : ""}
                    {waiting !== undefined
                      ? ` · ${tQueue("waitingCount", { count: waiting })}`
                      : ""}
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-2 sm:col-span-2">
          <Label htmlFor="emergency-complaint">
            {t("complaint")}{" "}
            <span className="text-muted-foreground font-normal">({t("optional")})</span>
          </Label>
          <Input
            id="emergency-complaint"
            className="h-tap text-base"
            maxLength={500}
            autoComplete="off"
            placeholder={t("complaintPlaceholder")}
            value={complaint}
            onChange={(event) => setComplaint(event.target.value)}
          />
        </div>
      </div>

      {canRequestAdmission ? (
        <label className="min-h-tap flex cursor-pointer items-center gap-3 rounded-lg border px-4 text-sm font-medium">
          <input
            type="checkbox"
            className="size-5"
            checked={toAdmission}
            onChange={(event) => setToAdmission(event.target.checked)}
            data-testid="emergency-to-admission"
          />
          {t("alsoAdmission")}
        </label>
      ) : null}

      <Button
        type="submit"
        className="h-tap bg-destructive hover:bg-destructive/90 w-full text-base text-white"
        disabled={!ready || register.isPending}
        data-testid="emergency-submit"
      >
        <Siren aria-hidden className="size-5" />
        {register.isPending ? t("submitting") : t("submit")}
      </Button>
      <p className="text-muted-foreground text-center text-xs">{t("identifyLater")}</p>
    </form>
  );
}
