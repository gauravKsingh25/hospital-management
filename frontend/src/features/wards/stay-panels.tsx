"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Circle, Pencil } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState, type FormEvent, type ReactNode } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LinkButton } from "@/components/ui/link-button";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { DoseRow } from "@/features/chart/dose-row";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Admission, Doctor, Dose, Page } from "@/types/api";

/**
 * The panels of the stay screen (layout B): what is due now, the clinical
 * facts, the stay's practical facts and how close it is to discharge.
 *
 * Every "Not recorded" is a place to record it — whoever may edit the
 * admission fixes a missing diagnosis or attendant where they see it, rather
 * than hunting for an edit screen. One field at a time, saved on its own.
 */
export type AdmissionPatch = Partial<
  Pick<
    Admission,
    | "provisional_diagnosis"
    | "admission_notes"
    | "expected_stay_days"
    | "attending_doctor_id"
    | "attendant_name"
    | "attendant_phone"
    | "attendant_relation"
  >
>;

export function useUpdateAdmission(admissionId: string) {
  const client = useQueryClient();
  const t = useTranslations("wards");
  return useMutation({
    mutationFn: (patch: AdmissionPatch) =>
      api.patch<Admission>(`/ipd/admissions/${admissionId}`, patch),
    onSuccess: (updated) => {
      client.setQueryData(["ipd", "admission", admissionId], updated);
      toast.success(t("detailsSaved"));
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });
}

export function Panel({
  title,
  action,
  children,
  testId,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section className="space-y-3 rounded-lg border p-4" data-testid={testId}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Due now
// ---------------------------------------------------------------------------
/** Doses due (or overdue) in the next few hours, each one tap from Given. */
export function DueNowPanel({
  admissionId,
  canAdminister,
  live,
}: {
  admissionId: string;
  canAdminister: boolean;
  live: boolean;
}) {
  const t = useTranslations("wards");
  const client = useQueryClient();
  const [until] = useState(() => new Date(Date.now() + 4 * 3_600_000).toISOString());
  const doses = useQuery({
    queryKey: ["ipd", "doses", admissionId, "due"],
    queryFn: ({ signal }) =>
      api.get<Page<Dose>>("/ipd/doses", {
        query: { admission_id: admissionId, status: "DUE", due_to: until, limit: 20 },
        signal,
      }),
    enabled: live,
    refetchInterval: 30_000,
  });
  const rows = doses.data?.items ?? [];

  return (
    <Panel
      title={t("dueNow")}
      testId="due-now"
      action={
        <LinkButton variant="outline" size="sm" className="h-tap" href={`/admissions/${admissionId}/chart`}>
          {t("openChart")}
        </LinkButton>
      }
    >
      {!live ? (
        <p className="text-muted-foreground text-sm">{t("stayClosed")}</p>
      ) : doses.isPending ? (
        <Skeleton className="h-14 w-full" />
      ) : rows.length === 0 ? (
        <p className="text-muted-foreground text-sm">{t("nothingDue")}</p>
      ) : (
        <ul className="divide-border divide-y">
          {rows.map((dose) => (
            <li key={dose.id}>
              <DoseRow
                dose={dose}
                showPatient={false}
                canAdminister={canAdminister}
                onSigned={() => {
                  void client.invalidateQueries({ queryKey: ["ipd", "doses", admissionId] });
                }}
              />
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Inline edit
// ---------------------------------------------------------------------------
function Field({
  label,
  value,
  canEdit,
  editing,
  onEdit,
  children,
}: {
  label: string;
  value: ReactNode;
  canEdit: boolean;
  editing: boolean;
  onEdit: () => void;
  children?: ReactNode;
}) {
  const t = useTranslations("wards");
  return (
    <div className="space-y-1">
      <dt className="text-muted-foreground text-xs">{label}</dt>
      {editing ? (
        <dd>{children}</dd>
      ) : (
        <dd className="flex items-start justify-between gap-2">
          <span className="min-w-0 break-words">{value}</span>
          {canEdit ? (
            <Button
              variant="ghost"
              size="icon"
              className="size-tap -my-2 shrink-0"
              aria-label={t("editField", { field: label })}
              onClick={onEdit}
            >
              <Pencil aria-hidden />
            </Button>
          ) : null}
        </dd>
      )}
    </div>
  );
}

function EditorActions({ pending, onCancel }: { pending: boolean; onCancel: () => void }) {
  const t = useTranslations("wards");
  return (
    <div className="mt-2 flex gap-2">
      <Button type="submit" size="sm" className="h-tap" disabled={pending}>
        {t("save")}
      </Button>
      <Button type="button" variant="ghost" size="sm" className="h-tap" onClick={onCancel}>
        {t("cancelEdit")}
      </Button>
    </div>
  );
}

function Missing() {
  const common = useTranslations("common");
  return <span className="text-muted-foreground italic">{common("notRecorded")}</span>;
}

type Editing = "diagnosis" | "notes" | "doctor" | "expected" | "attendant" | null;

/** Diagnosis, attending doctor and notes — the clinical side of the stay. */
export function ClinicalPanel({
  admission,
  canEdit,
}: {
  admission: Admission;
  canEdit: boolean;
}) {
  const t = useTranslations("wards");
  const update = useUpdateAdmission(admission.id);
  const [editing, setEditing] = useState<Editing>(null);
  const [draft, setDraft] = useState("");
  const editable = canEdit && (admission.status === "ADMITTED" || admission.status === "DISCHARGE_INITIATED");

  const start = (field: Editing, value: string | null | undefined) => {
    setDraft(value ?? "");
    setEditing(field);
  };
  const save = (patch: AdmissionPatch) => (event: FormEvent) => {
    event.preventDefault();
    update.mutate(patch, { onSuccess: () => setEditing(null) });
  };

  return (
    <Panel title={t("clinical")} testId="clinical-panel">
      <dl className="space-y-3 text-sm">
        <Field
          label={t("provisionalDiagnosis")}
          value={admission.provisional_diagnosis || <Missing />}
          canEdit={editable}
          editing={editing === "diagnosis"}
          onEdit={() => start("diagnosis", admission.provisional_diagnosis)}
        >
          <form onSubmit={save({ provisional_diagnosis: draft.trim() || null })}>
            <Input
              aria-label={t("provisionalDiagnosis")}
              value={draft}
              maxLength={500}
              autoFocus
              onChange={(event) => setDraft(event.target.value)}
              className="h-tap"
            />
            <EditorActions pending={update.isPending} onCancel={() => setEditing(null)} />
          </form>
        </Field>

        <Field
          label={t("attendingDoctor")}
          value={admission.attending_doctor_name || <Missing />}
          canEdit={editable}
          editing={editing === "doctor"}
          onEdit={() => start("doctor", admission.attending_doctor_id)}
        >
          <DoctorEditor
            value={draft}
            onChange={setDraft}
            pending={update.isPending}
            onCancel={() => setEditing(null)}
            onSubmit={save({ attending_doctor_id: draft || null })}
          />
        </Field>

        <Field
          label={t("admissionNotes")}
          value={admission.admission_notes ? <span className="whitespace-pre-line">{admission.admission_notes}</span> : <Missing />}
          canEdit={editable}
          editing={editing === "notes"}
          onEdit={() => start("notes", admission.admission_notes)}
        >
          <form onSubmit={save({ admission_notes: draft.trim() || null })}>
            <Textarea
              aria-label={t("admissionNotes")}
              value={draft}
              maxLength={2000}
              rows={3}
              autoFocus
              onChange={(event) => setDraft(event.target.value)}
            />
            <EditorActions pending={update.isPending} onCancel={() => setEditing(null)} />
          </form>
        </Field>
      </dl>
    </Panel>
  );
}

function DoctorEditor({
  value,
  onChange,
  pending,
  onCancel,
  onSubmit,
}: {
  value: string;
  onChange: (value: string) => void;
  pending: boolean;
  onCancel: () => void;
  onSubmit: (event: FormEvent) => void;
}) {
  const t = useTranslations("wards");
  const doctors = useQuery({
    queryKey: ["doctors", "all"],
    queryFn: ({ signal }) => api.get<Page<Doctor>>("/doctors", { query: { limit: 100 }, signal }),
    staleTime: 5 * 60_000,
  });
  return (
    <form onSubmit={onSubmit}>
      <select
        aria-label={t("attendingDoctor")}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="border-input bg-background h-tap w-full rounded-md border px-3 text-base"
      >
        <option value="">{t("noDoctor")}</option>
        {doctors.data?.items.map((doctor) => (
          <option key={doctor.id} value={doctor.id}>
            {doctor.display_name}
            {doctor.specialty ? ` — ${doctor.specialty}` : ""}
          </option>
        ))}
      </select>
      <EditorActions pending={pending} onCancel={onCancel} />
    </form>
  );
}

// ---------------------------------------------------------------------------
// The stay
// ---------------------------------------------------------------------------
/** When, how long, and who to call — the practical side of the stay. */
export function StayPanel({ admission, canEdit }: { admission: Admission; canEdit: boolean }) {
  const t = useTranslations("wards");
  const format = useFormatter();
  const update = useUpdateAdmission(admission.id);
  const [editing, setEditing] = useState<Editing>(null);
  const [expected, setExpected] = useState("");
  const [attendant, setAttendant] = useState({ name: "", relation: "", phone: "" });
  const editable = canEdit && (admission.status === "ADMITTED" || admission.status === "DISCHARGE_INITIATED");

  const admitted = new Date(admission.admitted_at);
  const expectedOut = admission.expected_stay_days
    ? new Date(admitted.getTime() + admission.expected_stay_days * 86_400_000)
    : null;
  const day = (value: Date) =>
    format.dateTime(value, { day: "2-digit", month: "short", year: "numeric" });

  const attendantText = admission.attendant_name
    ? [
        admission.attendant_name,
        admission.attendant_relation ? `(${admission.attendant_relation})` : null,
      ]
        .filter(Boolean)
        .join(" ")
    : null;

  return (
    <Panel title={t("stayDetails")} testId="stay-panel">
      <dl className="grid gap-3 text-sm sm:grid-cols-2">
        <div className="space-y-1">
          <dt className="text-muted-foreground text-xs">{t("admittedAt")}</dt>
          <dd>
            {format.dateTime(admitted, {
              day: "2-digit",
              month: "short",
              year: "numeric",
              hour: "numeric",
              minute: "2-digit",
            })}
          </dd>
        </div>
        <div className="space-y-1">
          <dt className="text-muted-foreground text-xs">{t("bedDaysLabel")}</dt>
          <dd>
            {t("lengthOfStay", { days: admission.length_of_stay_days })} ·{" "}
            {t("bedDaysCharged", { days: admission.bed_days_charged })}
          </dd>
        </div>

        <Field
          label={t("expectedStay")}
          value={
            admission.expected_stay_days ? (
              <>
                {t("expectedDays", { days: admission.expected_stay_days })}
                {expectedOut ? (
                  <span className="text-muted-foreground"> · {t("expectedOut", { date: day(expectedOut) })}</span>
                ) : null}
              </>
            ) : (
              <Missing />
            )
          }
          canEdit={editable}
          editing={editing === "expected"}
          onEdit={() => {
            setExpected(admission.expected_stay_days ? String(admission.expected_stay_days) : "");
            setEditing("expected");
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const days = expected.trim() ? Number(expected) : null;
              if (days !== null && (!Number.isInteger(days) || days < 0 || days > 365)) {
                toast.error(t("expectedInvalid"));
                return;
              }
              update.mutate({ expected_stay_days: days }, { onSuccess: () => setEditing(null) });
            }}
          >
            <Input
              aria-label={t("expectedStay")}
              type="number"
              inputMode="numeric"
              min={0}
              max={365}
              value={expected}
              autoFocus
              onChange={(event) => setExpected(event.target.value)}
              className="h-tap w-32"
            />
            <EditorActions pending={update.isPending} onCancel={() => setEditing(null)} />
          </form>
        </Field>

        <Field
          label={t("attendantName")}
          value={
            attendantText ? (
              <>
                {attendantText}
                {admission.attendant_phone ? (
                  <span className="tabular text-muted-foreground"> · {admission.attendant_phone}</span>
                ) : null}
              </>
            ) : (
              <Missing />
            )
          }
          canEdit={editable}
          editing={editing === "attendant"}
          onEdit={() => {
            setAttendant({
              name: admission.attendant_name ?? "",
              relation: admission.attendant_relation ?? "",
              phone: admission.attendant_phone ?? "",
            });
            setEditing("attendant");
          }}
        >
          <form
            className="space-y-2"
            onSubmit={(event) => {
              event.preventDefault();
              update.mutate(
                {
                  attendant_name: attendant.name.trim() || null,
                  attendant_relation: attendant.relation.trim() || null,
                  attendant_phone: attendant.phone.trim() || null,
                },
                { onSuccess: () => setEditing(null) },
              );
            }}
          >
            <Input
              aria-label={t("attendantNameField")}
              placeholder={t("attendantNameField")}
              value={attendant.name}
              maxLength={200}
              autoFocus
              onChange={(event) => setAttendant({ ...attendant, name: event.target.value })}
              className="h-tap"
            />
            <div className="grid grid-cols-2 gap-2">
              <Input
                aria-label={t("attendantRelation")}
                placeholder={t("attendantRelation")}
                value={attendant.relation}
                maxLength={50}
                onChange={(event) => setAttendant({ ...attendant, relation: event.target.value })}
                className="h-tap"
              />
              <Input
                aria-label={t("attendantPhoneField")}
                placeholder={t("attendantPhoneField")}
                type="tel"
                inputMode="tel"
                value={attendant.phone}
                maxLength={15}
                onChange={(event) => setAttendant({ ...attendant, phone: event.target.value })}
                className="h-tap"
              />
            </div>
            <EditorActions pending={update.isPending} onCancel={() => setEditing(null)} />
          </form>
        </Field>
      </dl>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Discharge readiness
// ---------------------------------------------------------------------------
/** The three things that stand between the bed and the next patient. */
export function DischargeReadiness({ admission }: { admission: Admission }) {
  const t = useTranslations("wards");
  const fit = admission.status === "DISCHARGE_INITIATED" || admission.status === "DISCHARGED";
  const summary = admission.discharge_summary_status;
  const steps: { label: string; done: boolean; detail: string; href?: string }[] = [
    {
      label: t("readyFit"),
      done: fit,
      detail: fit ? t("readyFitDone") : t("readyFitPending"),
    },
    {
      label: t("readySummary"),
      done: summary === "SIGNED",
      detail:
        summary === "SIGNED"
          ? t("readySummarySigned")
          : summary
            ? t("readySummaryDraft")
            : t("readySummaryNone"),
      href: `/admissions/${admission.id}/summary`,
    },
    {
      label: t("readyLeft"),
      done: admission.status === "DISCHARGED",
      detail: admission.status === "DISCHARGED" ? t("readyLeftDone") : t("readyLeftPending"),
    },
  ];

  return (
    <Panel title={t("dischargeReadiness")} testId="discharge-readiness">
      <ol className="space-y-2 text-sm">
        {steps.map((step) => (
          <li key={step.label} className="flex items-start gap-2">
            {step.done ? (
              <Check className="text-success mt-0.5 size-4 shrink-0" aria-hidden />
            ) : (
              <Circle className="text-muted-foreground mt-0.5 size-4 shrink-0" aria-hidden />
            )}
            <div className="min-w-0">
              <p className={cn("font-medium", step.done && "text-muted-foreground")}>{step.label}</p>
              <p className="text-muted-foreground text-xs">
                {step.detail}
                {step.href && !step.done ? (
                  <>
                    {" · "}
                    <a href={step.href} className="text-primary underline-offset-2 hover:underline">
                      {t("openSummary")}
                    </a>
                  </>
                ) : null}
              </p>
            </div>
          </li>
        ))}
      </ol>
    </Panel>
  );
}
