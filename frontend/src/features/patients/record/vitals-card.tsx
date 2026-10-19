import { Activity } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import type { Vitals } from "@/types/api";

/**
 * The most recent set of vitals the hospital took, from any visit.
 *
 * Read-only on purpose: vitals are recorded against a visit (the queue, the
 * admission page), and a second place to type them here would be a second
 * place to get the visit wrong.
 */
export function VitalsCard({ vitals }: { vitals: Vitals | null | undefined }) {
  const t = useTranslations("record");
  const tVitals = useTranslations("vitals");
  const format = useFormatter();

  const readings: { label: string; value: string | null }[] = vitals
    ? [
        {
          label: tVitals("bloodPressure"),
          value:
            vitals.systolic_bp !== null && vitals.diastolic_bp !== null
              ? `${vitals.systolic_bp}/${vitals.diastolic_bp} mmHg`
              : null,
        },
        { label: tVitals("pulse"), value: vitals.pulse_bpm !== null ? `${vitals.pulse_bpm} bpm` : null },
        {
          label: tVitals("temperature"),
          value: vitals.temperature_c !== null ? `${vitals.temperature_c} °C` : null,
        },
        { label: tVitals("spo2"), value: vitals.spo2_percent !== null ? `${vitals.spo2_percent}%` : null },
        {
          label: tVitals("respiratoryRate"),
          value: vitals.respiratory_rate !== null ? `${vitals.respiratory_rate} /min` : null,
        },
        { label: tVitals("weight"), value: vitals.weight_kg !== null ? `${vitals.weight_kg} kg` : null },
        {
          label: tVitals("glucose"),
          value: vitals.blood_glucose_mgdl !== null ? `${vitals.blood_glucose_mgdl} mg/dL` : null,
        },
      ].filter((reading) => reading.value !== null)
    : [];

  return (
    <section className="bg-card rounded-xl border" data-testid="latest-vitals">
      <div className="flex items-start gap-3 border-b px-4 py-3">
        <Activity aria-hidden className="text-primary mt-0.5 size-5 shrink-0" />
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold">{t("latestVitals")}</h2>
          {vitals ? (
            <p className="text-muted-foreground text-xs">
              {t("recordedOn", {
                date: format.dateTime(new Date(vitals.recorded_at), {
                  dateStyle: "medium",
                  timeStyle: "short",
                }),
              })}
            </p>
          ) : null}
        </div>
        {vitals?.is_abnormal ? (
          <span className="border-caution/40 bg-caution/15 text-caution-foreground rounded-full border px-2 py-0.5 text-xs font-medium">
            {t("abnormal")}
          </span>
        ) : null}
      </div>

      {vitals === undefined ? (
        <p className="text-muted-foreground px-4 py-6 text-center text-sm">{t("notShownToRole")}</p>
      ) : vitals === null || readings.length === 0 ? (
        <p className="text-muted-foreground px-4 py-6 text-center text-sm">{t("noVitals")}</p>
      ) : (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 px-4 py-3">
          {readings.map((reading) => (
            <div key={reading.label}>
              <dt className="text-muted-foreground text-xs">{reading.label}</dt>
              <dd className="tabular text-sm font-semibold">{reading.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}
