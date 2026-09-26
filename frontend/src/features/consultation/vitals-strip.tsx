"use client";

import { useFormatter, useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import type { Vitals } from "@/types/api";

/**
 * The most recent vitals, read-only.
 *
 * Read-only on purpose. CLAUDE.md §7 puts vitals entry with the nurse; a
 * doctor's screen shows them and does not invite editing, because a doctor
 * retyping a nurse's measurement is both wasted time and a second version of
 * a number that should have one.
 *
 * Presented as a strip rather than a table because the doctor's question is
 * "is anything off?", answered by scanning six numbers, not by reading a
 * history. Earlier readings are on the timeline for whoever wants them.
 *
 * `is_abnormal` comes from the backend, which owns the reference ranges. The
 * frontend deciding what counts as a high pulse would be a second clinical
 * rule to keep in step with the first.
 */
export function VitalsStrip({ vitals }: { vitals: Vitals[] }) {
  const t = useTranslations("consultation");
  const format = useFormatter();

  const latest = vitals.at(0);

  if (!latest) {
    return (
      <p className="text-muted-foreground rounded-lg border border-dashed px-4 py-3 text-sm">
        {t("noVitals")}
      </p>
    );
  }

  const readings: { label: string; value: string | null }[] = [
    { label: t("temperature"), value: latest.temperature_c ? `${latest.temperature_c}°C` : null },
    { label: t("pulse"), value: latest.pulse_bpm ? `${latest.pulse_bpm}` : null },
    {
      label: t("bloodPressure"),
      value:
        latest.systolic_bp && latest.diastolic_bp
          ? `${latest.systolic_bp}/${latest.diastolic_bp}`
          : null,
    },
    { label: t("spo2"), value: latest.spo2_percent ? `${latest.spo2_percent}%` : null },
    { label: t("respiratoryRate"), value: latest.respiratory_rate ? `${latest.respiratory_rate}` : null },
    { label: t("weight"), value: latest.weight_kg ? `${latest.weight_kg} kg` : null },
    { label: t("glucose"), value: latest.blood_glucose_mgdl ? `${latest.blood_glucose_mgdl}` : null },
    { label: t("painScore"), value: latest.pain_score !== null ? `${latest.pain_score}/10` : null },
  ].filter((reading) => reading.value !== null);

  return (
    <section
      className={cn(
        "flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border px-4 py-3",
        // One amber border for the whole strip when anything is out of range.
        // Colouring individual numbers would need the per-reading ranges the
        // backend does not send, and guessing them here would be worse than
        // saying "look at these".
        latest.is_abnormal && "border-caution/50 bg-caution/10",
      )}
      aria-label={t("vitals")}
    >
      <h2 className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
        {t("vitals")}
      </h2>

      {readings.map((reading) => (
        <div key={reading.label} className="flex items-baseline gap-1.5">
          <span className="text-muted-foreground text-xs">{reading.label}</span>
          <span className="tabular text-base font-semibold">{reading.value}</span>
        </div>
      ))}

      <span className="text-muted-foreground ml-auto text-xs">
        {format.dateTime(new Date(latest.recorded_at), "time")}
      </span>
    </section>
  );
}
