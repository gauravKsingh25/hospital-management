import { IdCard, MapPin, Phone, ShieldCheck, TriangleAlert, Users } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";
import type { Patient } from "@/types/api";

function Card({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <section className="bg-card rounded-xl border">
      <h2 className="flex items-center gap-2 border-b px-4 py-3 text-base font-semibold">
        <span className="text-primary">{icon}</span>
        {title}
      </h2>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

function Fields({ rows }: { rows: { label: string; value: ReactNode }[] }) {
  const common = useTranslations("common");
  return (
    <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
      {rows.map((row) => (
        <div key={row.label}>
          <dt className="text-muted-foreground text-xs">{row.label}</dt>
          <dd className={cn("text-sm", !row.value && "text-muted-foreground")}>
            {row.value || common("notRecorded")}
          </dd>
        </div>
      ))}
    </dl>
  );
}

const SEVERITY_TONES: Record<string, string> = {
  CRITICAL: "border-critical/30 bg-critical/10 text-critical",
  HIGH: "border-caution/40 bg-caution/15 text-caution-foreground",
  MODERATE: "border-info/25 bg-info/10 text-info",
  INFO: "border-border bg-muted text-muted-foreground",
};

/**
 * The demographic record in full: contact, address, next of kin, identity,
 * safety alerts and consents. Registration captures four fields so the queue
 * is never held up (CLAUDE.md §7b); this is where the rest is seen once
 * someone has had time to collect it — and where a gap is visible as a gap.
 */
export function ProfilePanel({ patient }: { patient: Patient }) {
  const t = useTranslations("record");
  const tPatient = useTranslations("patient");
  const common = useTranslations("common");
  const format = useFormatter();
  const date = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium" });

  const address = [
    patient.address_line1,
    patient.address_line2,
    patient.city,
    patient.district,
    patient.state,
    patient.pincode,
  ]
    .filter(Boolean)
    .join(", ");

  const guardian = patient.guardian_name
    ? `${patient.guardian_name}${patient.guardian_relation ? ` (${patient.guardian_relation})` : ""}`
    : null;

  const alerts = patient.alerts ?? [];
  const consents = patient.consents ?? [];

  return (
    <div className="grid gap-4 lg:grid-cols-2" data-testid="profile-panel">
      <Card icon={<Phone aria-hidden className="size-4" />} title={t("contact")}>
        <Fields
          rows={[
            { label: t("mobile"), value: <span className="tabular">{patient.phone}</span> },
            {
              label: t("alternateMobile"),
              value: patient.alternate_phone ? <span className="tabular">{patient.alternate_phone}</span> : null,
            },
            { label: t("email"), value: patient.email },
            { label: t("preferredLanguage"), value: patient.preferred_language.toUpperCase() },
          ]}
        />
      </Card>

      <Card icon={<MapPin aria-hidden className="size-4" />} title={t("address")}>
        <p className={cn("text-sm", !address && "text-muted-foreground")}>
          {address || common("notRecorded")}
        </p>
        <p className="text-muted-foreground mt-1 text-xs">{patient.country}</p>
      </Card>

      <Card icon={<Users aria-hidden className="size-4" />} title={t("guardian")}>
        <Fields
          rows={[
            { label: tPatient("name"), value: guardian },
            {
              label: t("guardianPhone"),
              value: patient.guardian_phone ? <span className="tabular">{patient.guardian_phone}</span> : null,
            },
          ]}
        />
      </Card>

      <Card icon={<IdCard aria-hidden className="size-4" />} title={t("identity")}>
        <Fields
          rows={[
            { label: tPatient("uhid"), value: <span className="tabular">{patient.uhid}</span> },
            {
              label: t("birthDate"),
              value: patient.birth_date
                ? `${date(patient.birth_date)}${patient.birth_date_is_estimated ? ` (${t("estimated")})` : ""}`
                : null,
            },
            { label: t("abhaNumber"), value: patient.abha_id },
            { label: t("abhaAddress"), value: patient.abha_address },
            { label: t("nationality"), value: patient.nationality },
            { label: t("maritalStatus"), value: patient.marital_status },
            { label: t("occupation"), value: patient.occupation },
            { label: t("registered"), value: date(patient.registered_at) },
          ]}
        />
      </Card>

      <Card icon={<TriangleAlert aria-hidden className="size-4" />} title={t("safetyAlerts")}>
        {alerts.length === 0 ? (
          <p className="text-muted-foreground text-sm">{t("noAlerts")}</p>
        ) : (
          <ul className="space-y-2">
            {alerts.map((alert) => (
              <li key={alert.id} className={cn("flex flex-wrap items-center gap-2", !alert.is_active && "opacity-60")}>
                <span
                  className={cn(
                    "rounded-full border px-2 py-0.5 text-xs font-medium",
                    SEVERITY_TONES[alert.severity],
                  )}
                >
                  {t(`severity${alert.severity}` as "severityHIGH")}
                </span>
                <span className="text-sm font-medium">{alert.label}</span>
                {alert.detail ? <span className="text-muted-foreground text-sm">— {alert.detail}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card icon={<ShieldCheck aria-hidden className="size-4" />} title={t("consents")}>
        {consents.length === 0 ? (
          <p className="text-muted-foreground text-sm">{t("noConsents")}</p>
        ) : (
          <ul className="space-y-2">
            {consents.map((consent) => (
              <li key={consent.id} className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
                <span className="font-medium">{t(`consent${consent.consent_type}` as "consentTREATMENT")}</span>
                <span
                  className={cn(
                    "text-xs",
                    consent.withdrawn_at || !consent.granted ? "text-critical" : "text-success",
                  )}
                >
                  {consent.withdrawn_at
                    ? t("consentWithdrawn", { date: date(consent.withdrawn_at) })
                    : consent.granted
                      ? t("consentGiven", { date: date(consent.granted_at) })
                      : t("consentRefused", { date: date(consent.granted_at) })}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
