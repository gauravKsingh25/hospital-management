"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
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
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Doctor, Encounter, Page } from "@/types/api";

/**
 * Death, referral out, and leaving against medical advice.
 *
 * CLAUDE.md §6 makes these **first-class terminal states, not afterthoughts**,
 * and until now they were the only part of the state machine with no screen:
 * recordable exclusively by an API call, which in practice means not
 * recordable. A hospital that cannot record a death in its own system keeps a
 * paper register beside it, and then the two disagree.
 *
 * ## Why they are behind one subdued control rather than three buttons
 *
 * These are rare, irreversible, and consequential. Three peers next to
 * "Complete visit" would put "record a death" one mis-click from the button a
 * doctor presses thirty times a day. So there is a single quiet entry point,
 * and choosing which outcome is a deliberate act inside the dialog.
 *
 * The friction after that point is the metadata the state machine already
 * demands — a death needs a time, a certifying doctor and a cause; a referral
 * needs a destination and a reason; a LAMA needs a reason. None of that is
 * ceremony: `REQUIRED_METADATA` in `state_machine.py` refuses the transition
 * without it, and each field is something a coroner, a receiving hospital, or
 * a lawyer will later ask for.
 */
type Outcome = "DEATH" | "REFERRAL" | "LAMA";

export function RecordOutcome({
  encounterId,
  patientName,
  canRecordDeath,
  canRecordReferral,
  canRecordLama,
  onRecorded,
}: {
  encounterId: string;
  patientName: string;
  canRecordDeath: boolean;
  canRecordReferral: boolean;
  canRecordLama: boolean;
  onRecorded: () => void;
}) {
  const t = useTranslations("outcome");
  const [open, setOpen] = useState(false);

  const allowed: Outcome[] = [
    ...(canRecordDeath ? (["DEATH"] as const) : []),
    ...(canRecordReferral ? (["REFERRAL"] as const) : []),
    ...(canRecordLama ? (["LAMA"] as const) : []),
  ];

  if (allowed.length === 0) return null;

  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        // Deliberately the quietest control on the screen. It is not a
        // secondary action, it is a rare one, and those are different things.
        className="text-muted-foreground h-tap"
        onClick={() => setOpen(true)}
        data-testid="record-outcome"
      >
        {t("open")}
      </Button>

      {open ? (
        <OutcomeDialog
          encounterId={encounterId}
          patientName={patientName}
          allowed={allowed}
          onOpenChange={(next) => {
            if (!next) setOpen(false);
          }}
          onRecorded={() => {
            setOpen(false);
            onRecorded();
          }}
        />
      ) : null}
    </>
  );
}

function OutcomeDialog({
  encounterId,
  patientName,
  allowed,
  onOpenChange,
  onRecorded,
}: {
  encounterId: string;
  patientName: string;
  allowed: Outcome[];
  onOpenChange: (open: boolean) => void;
  onRecorded: () => void;
}) {
  const t = useTranslations("outcome");
  const common = useTranslations("common");

  // No default. Picking which of these happened is the decision, and a
  // pre-selected "Death" is a pre-selected wrong answer most of the time.
  const [outcome, setOutcome] = useState<Outcome | null>(
    allowed.length === 1 ? allowed[0] : null,
  );

  const [deceasedAt, setDeceasedAt] = useState(nowForInput);
  const [certifierId, setCertifierId] = useState("");
  const [cause, setCause] = useState("");
  const [deathPlace, setDeathPlace] = useState("");

  const [facility, setFacility] = useState("");
  const [referralReason, setReferralReason] = useState("");
  const [transport, setTransport] = useState("");

  const [lamaReason, setLamaReason] = useState("");
  const [formSigned, setFormSigned] = useState(false);

  // Only doctors may certify a death, and the backend refuses a user from
  // another hospital. The picker is therefore the doctor list, keyed on
  // `user_id` — the certifier is a *person*, not a clinic profile.
  const doctors = useQuery({
    queryKey: ["doctors", "certifiers"],
    queryFn: ({ signal }) => api.get<Page<Doctor>>("/doctors", { query: { limit: 100 }, signal }),
    enabled: outcome === "DEATH",
    staleTime: 300_000,
  });

  const record = useMutation({
    mutationFn: () => {
      if (outcome === "DEATH") {
        return api.post<Encounter>(`/encounters/${encounterId}/death`, {
          // `new Date(localValue).toISOString()` reads the field as *local*
          // wall time and sends a real instant. Sending the raw
          // `datetime-local` string would arrive naive, be read as UTC, and
          // put a death five and a half hours late on the certificate — the
          // same class of bug the queue's `queue_date` had.
          deceased_at: new Date(deceasedAt).toISOString(),
          death_certified_by_id: certifierId,
          cause_of_death: cause.trim(),
          death_place: deathPlace.trim() || null,
        });
      }
      if (outcome === "REFERRAL") {
        return api.post<Encounter>(`/encounters/${encounterId}/referral`, {
          referred_to_facility: facility.trim(),
          referral_reason: referralReason.trim(),
          referral_transport: transport.trim() || null,
        });
      }
      return api.post<Encounter>(`/encounters/${encounterId}/lama`, {
        lama_reason: lamaReason.trim(),
        lama_form_signed: formSigned,
      });
    },
    onSuccess: () => {
      toast.success(t(`recorded${outcome}` as "recordedDEATH"));
      onRecorded();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const ready =
    outcome === "DEATH"
      ? deceasedAt !== "" && certifierId !== "" && cause.trim().length >= 3
      : outcome === "REFERRAL"
        ? facility.trim().length >= 2 && referralReason.trim().length >= 3
        : outcome === "LAMA"
          ? lamaReason.trim().length >= 3
          : false;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{patientName}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4 text-left">
          {allowed.length > 1 ? (
            <div className="space-y-1.5">
              <Label htmlFor="outcome-kind">{t("whatHappened")}</Label>
              <Select
                value={outcome ?? ""}
                onValueChange={(value) => setOutcome((value || null) as Outcome | null)}
              >
                <SelectTrigger id="outcome-kind" className="h-tap w-full">
                  <SelectValue placeholder={t("choose")} />
                </SelectTrigger>
                <SelectContent>
                  {allowed.map((option) => (
                    <SelectItem key={option} value={option}>
                      {t(`kind${option}` as "kindDEATH")}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : null}

          {outcome === "DEATH" ? (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="deceased-at">{t("deceasedAt")}</Label>
                <Input
                  id="deceased-at"
                  type="datetime-local"
                  value={deceasedAt}
                  onChange={(event) => setDeceasedAt(event.target.value)}
                  className="h-tap"
                  // A time of death cannot be in the future; the backend
                  // refuses it, and the picker should not offer it either.
                  max={nowForInput()}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="certifier">{t("certifiedBy")}</Label>
                <Select value={certifierId} onValueChange={(value) => setCertifierId(value ?? "")}>
                  <SelectTrigger id="certifier" className="h-tap w-full">
                    <SelectValue placeholder={t("chooseDoctor")} />
                  </SelectTrigger>
                  <SelectContent>
                    {(doctors.data?.items ?? []).map((doctor) => (
                      <SelectItem key={doctor.id} value={doctor.user_id}>
                        {doctor.display_name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {/* Not the person recording it. A records officer types this
                    in; the doctor named here is the one who certified. */}
                <p className="text-muted-foreground text-xs">{t("certifiedByHint")}</p>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="cause">{t("cause")}</Label>
                <Textarea
                  id="cause"
                  value={cause}
                  onChange={(event) => setCause(event.target.value)}
                  rows={2}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="death-place">{t("deathPlace")}</Label>
                <Input
                  id="death-place"
                  value={deathPlace}
                  onChange={(event) => setDeathPlace(event.target.value)}
                  className="h-tap"
                  placeholder={t("deathPlacePlaceholder")}
                />
              </div>
            </>
          ) : null}

          {outcome === "REFERRAL" ? (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="facility">{t("facility")}</Label>
                <Input
                  id="facility"
                  value={facility}
                  onChange={(event) => setFacility(event.target.value)}
                  className="h-tap"
                  placeholder={t("facilityPlaceholder")}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="referral-reason">{t("referralReason")}</Label>
                <Textarea
                  id="referral-reason"
                  value={referralReason}
                  onChange={(event) => setReferralReason(event.target.value)}
                  rows={2}
                />
                {/* The receiving hospital reads this before the patient
                    arrives. It is the referral letter, not a form field. */}
                <p className="text-muted-foreground text-xs">{t("referralReasonHint")}</p>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="transport">{t("transport")}</Label>
                <Input
                  id="transport"
                  value={transport}
                  onChange={(event) => setTransport(event.target.value)}
                  className="h-tap"
                  placeholder={t("transportPlaceholder")}
                />
              </div>
            </>
          ) : null}

          {outcome === "LAMA" ? (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="lama-reason">{t("lamaReason")}</Label>
                <Textarea
                  id="lama-reason"
                  value={lamaReason}
                  onChange={(event) => setLamaReason(event.target.value)}
                  rows={2}
                />
              </div>

              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={formSigned}
                  onChange={(event) => setFormSigned(event.target.checked)}
                  className="mt-1"
                  data-testid="lama-form-signed"
                />
                <span>
                  {t("formSigned")}
                  {/* Recorded either way, on purpose. An unsigned LAMA is the
                      hospital's legal exposure, and a checkbox that is quietly
                      always ticked helps nobody the day it is examined. */}
                  <span className="text-muted-foreground block text-xs">
                    {t("formSignedHint")}
                  </span>
                </span>
              </label>
            </>
          ) : null}

          {outcome ? (
            <p className="border-destructive/30 bg-destructive/5 rounded-md border px-3 py-2 text-xs">
              {t("irreversible")}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className={cn("h-tap", outcome === "DEATH" && "bg-destructive hover:bg-destructive/90")}
            disabled={!ready || record.isPending}
            onClick={() => record.mutate()}
            data-testid="confirm-outcome"
          >
            {t("record")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * What a closed visit says about how it ended.
 *
 * Until this existed a terminal visit rendered as a status chip and nothing
 * else — the cause of death, the hospital a patient was referred to, whether
 * the LAMA form was signed were all recorded and none of it was readable. The
 * metadata is required precisely because somebody asks for it later, and
 * "later" is this panel.
 */
export function OutcomeSummary({ encounter }: { encounter: Encounter }) {
  const t = useTranslations("outcome");
  const common = useTranslations("common");

  const rows: { label: string; value: string | null }[] =
    encounter.status === "DECEASED"
      ? [
          { label: t("deceasedAt"), value: formatMoment(encounter.deceased_at) },
          // The name, not the id. A death record whose certifier is a UUID is
          // not a record anybody outside this system can use.
          { label: t("certifiedBy"), value: encounter.death_certified_by_name ?? null },
          { label: t("cause"), value: encounter.cause_of_death ?? null },
          { label: t("deathPlace"), value: encounter.death_place ?? null },
        ]
      : encounter.status === "REFERRED_OUT"
        ? [
            { label: t("referredAt"), value: formatMoment(encounter.referred_at) },
            { label: t("facility"), value: encounter.referred_to_facility ?? null },
            { label: t("referralReason"), value: encounter.referral_reason ?? null },
          ]
        : encounter.status === "LAMA"
          ? [
              { label: t("lamaAt"), value: formatMoment(encounter.lama_at) },
              { label: t("lamaReason"), value: encounter.lama_reason ?? null },
              {
                label: t("formSigned"),
                value: encounter.lama_form_signed ? common("yes") : t("formNotSigned"),
              },
            ]
          : [];

  if (rows.length === 0) return null;

  return (
    <section
      className={cn(
        "space-y-2 rounded-lg border p-4",
        encounter.status === "DECEASED" && "border-foreground/40 bg-foreground/5",
        encounter.status === "LAMA" && "border-caution/50 bg-caution/10",
      )}
      data-testid="outcome-summary"
      data-status={encounter.status}
    >
      <h2 className="text-sm font-semibold">{t(`heading${encounter.status}` as "headingLAMA")}</h2>
      <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
        {rows.map((row) => (
          <div key={row.label}>
            <dt className="text-muted-foreground text-xs">{row.label}</dt>
            <dd className="text-sm">{row.value ?? common("notRecorded")}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** `YYYY-MM-DDTHH:mm` for a `datetime-local` input, truncated to the minute. */
function nowForInput(): string {
  const now = new Date();
  // Local time, not UTC — `toISOString()` here would offer a doctor in IST a
  // default five and a half hours in the past. Truncating to the minute also
  // keeps the default safely at or before "now", which the backend requires.
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 16);
}

function formatMoment(value: string | null | undefined): string | null {
  if (!value) return null;
  return new Date(value).toLocaleString();
}
