import { QrCode } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { LinkButton } from "@/components/ui/link-button";
import { cn } from "@/lib/utils";
import type { Patient } from "@/types/api";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const first = parts[0]?.[0] ?? "";
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? "") : "";
  return (first + last).toUpperCase();
}

/**
 * Who this is — the first thing on the record, and the half of patient safety
 * that is confirming identity before anything else is read.
 *
 * Everything a counter is asked to read back is here at a glance: name, UHID,
 * age and sex, blood group, mobile. The deceased badge sits beside the name
 * rather than further down, because it changes what every action below it
 * means.
 */
export function RecordHeader({ patient }: { patient: Patient }) {
  const t = useTranslations("patient");
  const tRecord = useTranslations("record");
  const tCard = useTranslations("card");
  const common = useTranslations("common");
  const format = useFormatter();

  return (
    <header className="bg-card flex flex-wrap items-start gap-4 rounded-xl border p-5 shadow-sm">
      <div
        aria-hidden
        className={cn(
          "flex size-14 shrink-0 items-center justify-center rounded-full text-lg font-semibold",
          patient.is_deceased ? "bg-foreground/85 text-background" : "bg-primary/10 text-primary",
        )}
      >
        {initials(patient.full_name)}
      </div>

      {/* `basis` lets the card button drop below on a phone instead of
          squeezing the name into a column. */}
      <div className="min-w-0 flex-1 basis-56 space-y-1.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h1 className="text-2xl font-semibold tracking-tight" data-testid="record-name">
            {patient.full_name}
          </h1>
          {patient.is_deceased ? (
            <span className="bg-foreground/85 text-background rounded px-2 py-0.5 text-xs font-medium">
              {t("deceased")}
            </span>
          ) : null}
          {patient.abha_id ? (
            <span className="border-success/30 bg-success/10 text-success rounded-full border px-2 py-0.5 text-xs font-medium">
              {tRecord("abhaLinked")}
            </span>
          ) : null}
        </div>

        <dl className="text-muted-foreground flex flex-wrap items-center gap-x-4 gap-y-1 text-sm whitespace-nowrap">
          <div className="flex gap-1">
            <dt className="sr-only">{t("uhid")}</dt>
            <dd className="text-foreground tabular font-medium">{patient.uhid}</dd>
          </div>
          <div className="flex gap-1">
            <dt className="sr-only">{t("age")}</dt>
            <dd>
              {patient.age_years === null || patient.age_years === undefined
                ? common("notRecorded")
                : t("ageYears", { age: patient.age_years })}
              {" · "}
              {t(`gender${patient.gender}` as "genderMALE")}
            </dd>
          </div>
          {patient.blood_group !== "UNKNOWN" ? (
            <div className="flex gap-1">
              <dt className="sr-only">{t("bloodGroup")}</dt>
              <dd className="border-critical/30 text-critical rounded border px-1.5 text-xs font-semibold">
                {patient.blood_group}
              </dd>
            </div>
          ) : null}
          <div className="flex gap-1">
            <dt className="sr-only">{t("phone")}</dt>
            <dd className="tabular">{patient.phone}</dd>
          </div>
          {patient.city ? (
            <div className="flex gap-1">
              <dt className="sr-only">{t("city")}</dt>
              <dd>{patient.city}</dd>
            </div>
          ) : null}
        </dl>

        <p className="text-muted-foreground text-xs">
          {tRecord("patientSince", {
            date: format.dateTime(new Date(patient.registered_at), { dateStyle: "medium" }),
          })}
        </p>
      </div>

      {/* Reprinting a lost card is a counter task, so it lives on the record
          rather than behind an administration screen. */}
      <LinkButton variant="outline" size="sm" className="h-tap" href={`/patients/${patient.id}/card`}>
        <QrCode aria-hidden className="size-4" />
        {tCard("title")}
      </LinkButton>
    </header>
  );
}
