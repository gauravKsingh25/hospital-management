"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Admission, DischargeSummary } from "@/types/api";

/**
 * The discharge summary.
 *
 * **Compiled, never invented.** The backend assembles it from what was already
 * recorded during the stay — the diagnoses, the course, the investigations,
 * the drugs — and the doctor edits what is wrong rather than writing the
 * document from a blank page. That is the whole reason this screen exists at
 * all: a summary somebody types from memory at the end of a shift is the least
 * reliable record in the hospital, and it is the one the patient takes home.
 *
 * Every section is optional to edit, matching `SummaryUpdate`. Signing freezes
 * it: after that a correction is an amendment, which leaves both versions.
 */
type Section = { key: keyof DischargeSummary; labelKey: string; rows: number };

const SECTIONS: Section[] = [
  { key: "presenting_complaint", labelKey: "presentingComplaint", rows: 2 },
  { key: "diagnoses", labelKey: "diagnoses", rows: 2 },
  { key: "course_in_hospital", labelKey: "courseInHospital", rows: 5 },
  { key: "investigations", labelKey: "investigations", rows: 4 },
  { key: "procedures", labelKey: "procedures", rows: 2 },
  { key: "treatment_given", labelKey: "treatmentGiven", rows: 4 },
  { key: "condition_at_discharge", labelKey: "conditionAtDischarge", rows: 2 },
  { key: "discharge_medications", labelKey: "dischargeMedications", rows: 4 },
  { key: "follow_up_instructions", labelKey: "followUpInstructions", rows: 3 },
  { key: "diet_and_activity", labelKey: "dietAndActivity", rows: 2 },
  { key: "warning_signs", labelKey: "warningSigns", rows: 3 },
];

export function SummaryScreen({
  admission,
  initial,
  canWrite,
  canSign,
}: {
  admission: Admission;
  initial: DischargeSummary | null;
  canWrite: boolean;
  canSign: boolean;
}) {
  const t = useTranslations("summary");

  const summary = useQuery({
    queryKey: ["ipd", "summary", admission.id],
    queryFn: ({ signal }) =>
      api.get<DischargeSummary>(`/ipd/summaries/by-admission/${admission.id}`, { signal }),
    initialData: initial ?? undefined,
    enabled: initial !== null,
    staleTime: 0,
  });

  const compile = useMutation({
    mutationFn: () =>
      api.post<DischargeSummary>(`/ipd/summaries/compile/${admission.id}`, {}),
    onSuccess: () => {
      toast.success(t("compiled"));
      void summary.refetch();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const data = summary.data;

  if (!data) {
    return (
      <div className="space-y-6">
        <SummaryHeader admission={admission} status={null} />
        <div className="text-muted-foreground flex flex-col items-center gap-3 rounded-lg border border-dashed py-16 text-center">
          <p className="text-foreground text-sm font-medium">{t("notCompiled")}</p>
          <p className="max-w-md text-sm">{t("notCompiledHint")}</p>
          {canWrite ? (
            <Button
              className="h-tap"
              disabled={compile.isPending}
              onClick={() => compile.mutate()}
              data-testid="compile-summary"
            >
              {t("compile")}
            </Button>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <SummaryHeader admission={admission} status={data.status} signedBy={data.signed_by_name} />
      <SummaryEditor
        summary={data}
        canWrite={canWrite}
        canSign={canSign}
        onSaved={() => summary.refetch()}
        onRecompile={() => compile.mutate()}
        recompiling={compile.isPending}
      />
    </div>
  );
}

function SummaryHeader({
  admission,
  status,
  signedBy,
}: {
  admission: Admission;
  status: DischargeSummary["status"] | null;
  signedBy?: string | null;
}) {
  const t = useTranslations("summary");
  const common = useTranslations("common");

  return (
    <header className="bg-muted/30 rounded-lg border p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            {admission.patient_name ?? common("notRecorded")}
          </h1>
          <p className="text-muted-foreground tabular text-sm">
            {admission.uhid} · {admission.admission_number}
          </p>
        </div>
        {status ? (
          <span
            className={cn(
              "rounded-full border px-2.5 py-0.5 text-xs font-medium",
              status === "DRAFT" && "bg-muted text-muted-foreground border-border",
              status === "FINAL" && "border-success/25 bg-success/10 text-success",
              status === "AMENDED" && "border-caution/40 bg-caution/15 text-caution-foreground",
            )}
            data-status={status}
          >
            {t(`status${status}` as "statusDRAFT")}
          </span>
        ) : null}
      </div>

      {signedBy ? (
        <p className="text-muted-foreground mt-2 text-xs">{t("signedBy", { name: signedBy })}</p>
      ) : null}
    </header>
  );
}

function SummaryEditor({
  summary,
  canWrite,
  canSign,
  onSaved,
  onRecompile,
  recompiling,
}: {
  summary: DischargeSummary;
  canWrite: boolean;
  canSign: boolean;
  onSaved: () => void;
  onRecompile: () => void;
  recompiling: boolean;
}) {
  const t = useTranslations("summary");
  const common = useTranslations("common");

  // Frozen once signed. A correction after that is an amendment, which leaves
  // both versions standing — somebody may already have acted on the first.
  const frozen = summary.status !== "DRAFT";
  const editable = canWrite && !frozen;

  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      SECTIONS.map((section) => [section.key, (summary[section.key] as string | null) ?? ""]),
    ),
  );
  const [registration, setRegistration] = useState("");

  // The backend refuses to sign a summary with no diagnosis, and it is right
  // to: a discharge document that does not say what was wrong with the patient
  // is not a document. Surfaced here rather than left to a toast, so the
  // doctor sees the reason next to the field that fixes it instead of pressing
  // a button and being told no.
  const hasDiagnosis = (draft.diagnoses ?? "").trim().length > 0;

  const save = useMutation({
    mutationFn: () =>
      api.patch<DischargeSummary>(`/ipd/summaries/${summary.id}`, {
        ...Object.fromEntries(
          Object.entries(draft).map(([key, value]) => [key, value.trim() || null]),
        ),
      }),
    onSuccess: () => {
      toast.success(t("saved"));
      onSaved();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const sign = useMutation({
    mutationFn: () =>
      api.post<DischargeSummary>(`/ipd/summaries/${summary.id}/sign`, {
        registration_number: registration.trim() || null,
      }),
    onSuccess: () => {
      toast.success(t("signed"));
      onSaved();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <div className="space-y-4">
      {frozen ? (
        <p className="border-success/30 bg-success/10 rounded-md border px-3 py-2 text-sm">
          {t("frozenHint")}
        </p>
      ) : null}

      <div className="space-y-4 rounded-lg border p-4">
        {SECTIONS.map((section) => (
          <div key={section.key} className="space-y-1.5">
            <Label htmlFor={`summary-${section.key}`}>
              {t(section.labelKey as "diagnoses")}
            </Label>
            <Textarea
              id={`summary-${section.key}`}
              value={draft[section.key] ?? ""}
              onChange={(event) =>
                setDraft((current) => ({ ...current, [section.key]: event.target.value }))
              }
              disabled={!editable}
              rows={section.rows}
            />
          </div>
        ))}
      </div>

      {editable ? (
        <div className="flex flex-wrap items-end gap-3">
          <Button className="h-tap" disabled={save.isPending} onClick={() => save.mutate()}>
            {common("save")}
          </Button>

          {/*
            The draft is compiled when the patient is admitted, so by the time
            anybody reads it the stay has moved on — drugs prescribed, results
            filed, notes written. Without this the doctor would be transcribing
            five days of record into a document the compiler could have filled,
            which is precisely the work this module exists to remove. Refused
            server-side once signed.
          */}
          <Button
            variant="outline"
            className="h-tap"
            disabled={recompiling}
            onClick={onRecompile}
            data-testid="recompile-summary"
          >
            {t("recompile")}
          </Button>

          {canSign ? (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="summary-registration">{t("registrationNumber")}</Label>
                <Input
                  id="summary-registration"
                  value={registration}
                  onChange={(event) => setRegistration(event.target.value)}
                  className="h-tap tabular w-56"
                />
              </div>
              <Button
                variant="outline"
                className="h-tap"
                disabled={!hasDiagnosis || sign.isPending}
                onClick={() => sign.mutate()}
                data-testid="sign-summary"
              >
                {t("sign")}
              </Button>
            </>
          ) : null}
        </div>
      ) : null}

      {editable && canSign ? (
        <p className={cn("text-xs", hasDiagnosis ? "text-muted-foreground" : "text-caution-foreground")}>
          {hasDiagnosis ? t("signHint") : t("diagnosisRequired")}
        </p>
      ) : null}
    </div>
  );
}
