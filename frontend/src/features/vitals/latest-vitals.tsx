"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";
import { Plus } from "lucide-react";

import { LoadError } from "@/components/load-error";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { VitalsDialog } from "@/features/vitals/vitals-dialog";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type { Vitals } from "@/types/api";

/**
 * The most recent vitals for a visit, with a way to take another set.
 *
 * On the admission page, so the set the desk took at the counter is the first
 * thing the ward sees — the consultation screen that used to show vitals no
 * longer exists.
 */
export function LatestVitals({
  encounterId,
  patientName,
  canRecord,
  live,
}: {
  encounterId: string;
  patientName: string;
  canRecord: boolean;
  live: boolean;
}) {
  const t = useTranslations("vitals");
  const tWards = useTranslations("wards");
  const format = useFormatter();
  const [recording, setRecording] = useState(false);

  const vitals = useQuery({
    queryKey: ["ipd", "vitals", encounterId],
    queryFn: ({ signal }) => api.get<Vitals[]>(`/encounters/${encounterId}/vitals`, { signal }),
    staleTime: 0,
  });

  const latest = [...(vitals.data ?? [])].sort(
    (a, b) => new Date(b.recorded_at).getTime() - new Date(a.recorded_at).getTime(),
  )[0];

  const readings: { label: string; value: string | null }[] = latest
    ? [
        {
          label: t("bloodPressure"),
          value:
            latest.systolic_bp !== null && latest.diastolic_bp !== null
              ? `${latest.systolic_bp}/${latest.diastolic_bp} mmHg`
              : null,
        },
        { label: t("pulse"), value: latest.pulse_bpm !== null ? `${latest.pulse_bpm} bpm` : null },
        {
          label: t("temperature"),
          value: latest.temperature_c !== null ? `${latest.temperature_c} °C` : null,
        },
        {
          label: t("spo2"),
          value: latest.spo2_percent !== null ? `${latest.spo2_percent}%` : null,
        },
        {
          label: t("respiratoryRate"),
          value: latest.respiratory_rate !== null ? `${latest.respiratory_rate}/min` : null,
        },
        { label: t("weight"), value: latest.weight_kg !== null ? `${latest.weight_kg} kg` : null },
        {
          label: t("glucose"),
          value:
            latest.blood_glucose_mgdl !== null ? `${latest.blood_glucose_mgdl} mg/dL` : null,
        },
      ].filter((reading) => reading.value !== null)
    : [];

  return (
    <section className="space-y-3 rounded-lg border p-4" aria-labelledby="vitals-heading">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 id="vitals-heading" className="text-sm font-semibold">
            {tWards("latestVitals")}
          </h2>
          {latest ? (
            <p className="text-muted-foreground text-xs">
              {format.dateTime(new Date(latest.recorded_at), {
                day: "numeric",
                month: "short",
                hour: "2-digit",
                minute: "2-digit",
              })}
            </p>
          ) : null}
        </div>
        {canRecord && live ? (
          <Button size="sm" variant="outline" className="h-tap" onClick={() => setRecording(true)}>
            <Plus aria-hidden className="size-4" />
            {tWards("recordVitals")}
          </Button>
        ) : null}
      </div>

      {vitals.isPending ? (
        <Skeleton className="h-10 w-full" />
      ) : vitals.isError && !vitals.data ? (
        // A failed request must not read as "no vitals recorded yet".
        <LoadError onRetry={() => void vitals.refetch()} />
      ) : readings.length === 0 ? (
        <p className="text-muted-foreground text-sm">{tWards("noVitals")}</p>
      ) : (
        <dl
          className={cn("grid gap-3 text-sm sm:grid-cols-4", latest?.is_abnormal && "text-foreground")}
          data-testid="latest-vitals"
        >
          {readings.map((reading) => (
            <div key={reading.label}>
              <dt className="text-muted-foreground text-xs">{reading.label}</dt>
              <dd className="tabular font-medium">{reading.value}</dd>
            </div>
          ))}
        </dl>
      )}

      <VitalsDialog
        encounterId={encounterId}
        patientName={patientName}
        open={recording}
        onOpenChange={setRecording}
        onRecorded={() => void vitals.refetch()}
      />
    </section>
  );
}
