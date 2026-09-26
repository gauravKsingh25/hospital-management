"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { TriangleAlert } from "lucide-react";

import { formatMinutes } from "@/features/reports/format";
import { useQueueSnapshot } from "@/features/reports/queue-snapshot";

/**
 * "Dr Rao is running forty minutes behind."
 *
 * CLAUDE.md §7b asks for doctor delay alerts *to reception* when a clinic runs
 * behind, and this is that alert. It sits on the screen reception already has
 * open, because a dashboard nobody opens is not an alert — the person who
 * needs this is the one a patient is standing in front of asking how long.
 *
 * ## It renders nothing when nothing is wrong
 *
 * No empty state, no "all clinics on time" strip. A banner that is always
 * present is a banner people stop seeing, and this one has to be noticed on
 * the day it appears. `running_late` is computed server-side against a
 * configurable threshold, so the definition of "behind" is the hospital's and
 * not this component's.
 */
export function DelayAlert() {
  const t = useTranslations("reports");
  const snapshot = useQueueSnapshot();
  const common = useTranslations("common");

  const late = snapshot.data?.running_late ?? [];
  if (late.length === 0) return null;

  return (
    <aside
      // `polite`, not `assertive`: a receptionist mid-registration should not
      // have a screen reader interrupt them for a clinic running behind.
      aria-live="polite"
      className="border-caution/50 bg-caution/10 flex flex-wrap items-start gap-3 rounded-lg border p-3"
      data-testid="delay-alert"
    >
      <TriangleAlert aria-hidden className="text-caution-foreground mt-0.5 size-4 shrink-0" />

      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm font-medium">
          {t("runningLate", { minutes: snapshot.data?.delay_threshold_minutes ?? 0 })}
        </p>
        <ul className="text-sm">
          {late.map((doctor) => (
            <li key={doctor.doctor_id ?? "unassigned"} className="tabular">
              {doctor.doctor_name ?? common("notRecorded")} ·{" "}
              {t("waitingCount", { count: doctor.waiting })} ·{" "}
              {t("longestIs", { minutes: formatMinutes(doctor.longest_wait_minutes) })}
            </li>
          ))}
        </ul>
      </div>

      <Link href="/reports" className="text-sm font-medium underline underline-offset-4">
        {t("openDashboard")}
      </Link>
    </aside>
  );
}
