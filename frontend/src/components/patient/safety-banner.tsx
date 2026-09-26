import { useTranslations } from "next-intl";
import { AlertTriangle, ShieldAlert } from "lucide-react";

import { cn } from "@/lib/utils";
import type { SafetyBanner as SafetyBannerData } from "@/types/api";

/**
 * The persistent patient-safety header (CLAUDE.md §7b).
 *
 * Drug allergies, infectious precautions, fall risk and high-risk conditions,
 * shown above everything on any screen where a clinical decision is made.
 *
 * Three deliberate choices:
 *
 * **It arrives with the chart, not after it.** The backend carries this on
 * the same response as the consultation data. A banner that loads one request
 * later is a banner that is absent for the second or two in which a doctor
 * glances at the screen and starts typing.
 *
 * **It never collapses.** Not behind a toggle, not scrolled away, not
 * "expand to see 3 alerts". The whole value of a safety banner is that
 * nobody has to choose to look at it.
 *
 * **Deceased comes first and reads as an instruction.** It is the one alert
 * whose consequence is a rule about what the *system* must not do — never
 * send a follow-up message to a deceased patient (CLAUDE.md §14). The
 * backend enforces that; this makes sure a human at a counter does not
 * cheerfully do it by hand.
 */
export function SafetyBanner({
  banner,
  className,
}: {
  banner: SafetyBannerData;
  className?: string;
}) {
  const t = useTranslations("safety");
  const patient = useTranslations("patient");

  const hasAlerts = banner.alerts.length > 0;

  if (!hasAlerts && !banner.is_deceased) {
    // Nothing to warn about. Rendering an empty "no alerts" bar on every
    // screen would train people to ignore the space the real warning uses.
    return null;
  }

  return (
    <div
      // `alert` announces immediately on render, which is what a critical
      // allergy warrants; a non-critical list uses `status` so a screen
      // reader finishes the current sentence first.
      role={banner.has_critical_alert || banner.is_deceased ? "alert" : "status"}
      className={cn(
        "flex flex-wrap items-start gap-x-3 gap-y-2 rounded-lg border px-4 py-3",
        banner.has_critical_alert || banner.is_deceased
          ? "border-critical/50 bg-critical/10 text-critical"
          : "border-caution/50 bg-caution/15 text-caution-foreground",
        className,
      )}
      data-testid="safety-banner"
    >
      {banner.has_critical_alert || banner.is_deceased ? (
        <ShieldAlert aria-hidden className="mt-0.5 size-5 shrink-0" />
      ) : (
        <AlertTriangle aria-hidden className="mt-0.5 size-5 shrink-0" />
      )}

      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm font-semibold">
          {banner.has_critical_alert ? t("criticalTitle") : t("title")}
        </p>

        {banner.is_deceased ? (
          <p className="text-sm font-medium">
            {patient("deceased")} — {t("deceasedNotice")}
          </p>
        ) : null}

        {hasAlerts ? (
          <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
            {banner.alerts.map((alert) => (
              <li key={alert} className="font-medium">
                {alert}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The patient's identity line, shown beside the banner.
 *
 * Separate from the banner because it must render even when there is nothing
 * to warn about — and because confirming *who* this is happens to be the
 * other half of avoiding a safety incident.
 */
export function PatientIdentityStrip({
  banner,
  className,
}: {
  banner: SafetyBannerData;
  className?: string;
}) {
  const t = useTranslations("patient");
  const common = useTranslations("common");

  return (
    <div className={cn("flex flex-wrap items-baseline gap-x-4 gap-y-1", className)}>
      <h2 className="text-xl font-semibold tracking-tight">{banner.full_name}</h2>
      <span className="text-muted-foreground tabular text-sm">
        {t("uhid")} {banner.uhid}
      </span>
      <span className="text-muted-foreground text-sm">
        {banner.age_years === null
          ? common("notRecorded")
          : t("ageYears", { age: banner.age_years })}
        {" · "}
        {t(`gender${banner.gender}` as "genderMALE")}
      </span>
      {banner.blood_group !== "UNKNOWN" ? (
        <span className="border-border rounded border px-1.5 py-0.5 text-xs font-medium">
          {banner.blood_group}
        </span>
      ) : null}
    </div>
  );
}
