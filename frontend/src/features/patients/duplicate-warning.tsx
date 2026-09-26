"use client";

import { useTranslations } from "next-intl";
import { Users } from "lucide-react";

import { LinkButton } from "@/components/ui/link-button";
import type { DuplicateCandidate } from "@/types/api";

/**
 * Possible existing records, shown while reception types (CLAUDE.md §7b).
 *
 * Duplicate patient records are one of the most expensive quiet failures in a
 * hospital: a second UHID means a split history, so the allergy recorded last
 * year is not on the chart the doctor is looking at today.
 *
 * Three things this deliberately does:
 *
 * **It shows the reason in words.** The backend returns "same mobile" or
 * "similar name, same mobile" alongside a score. Staff can act on a reason;
 * nobody can calibrate "0.82".
 *
 * **It blocks only on an identity collision.** `is_exact` means the same
 * normalised name *and* the same mobile — the pair the database's unique
 * index refuses outright. Everything else is advisory and does not disable
 * the submit button. That distinction matters more than it looks: in a
 * hospital where a large share of patients are named Sharma, Kumar or Singh,
 * blocking on name similarity would make staff tick "different person" on
 * nearly every registration, and a confirmation everyone ticks by reflex
 * protects nobody. The backend already draws this line (see
 * `DuplicateMatch.is_exact`); the UI must not redraw it more strictly.
 *
 * **It offers the existing record as a link, not just a name.** The whole
 * point is that opening the right record must be easier than creating a
 * wrong one.
 */
export function DuplicateWarning({
  candidates,
  confirmed,
  onConfirm,
}: {
  candidates: DuplicateCandidate[];
  confirmed: boolean;
  onConfirm: (value: boolean) => void;
}) {
  const t = useTranslations("registration");

  // Only an identity collision needs a decision. A resemblance is shown and
  // left alone.
  const blocking = candidates.some((candidate) => candidate.is_exact);

  return (
    <section
      role="alert"
      className="border-caution/50 bg-caution/10 space-y-3 rounded-lg border p-4"
      data-testid="duplicate-warning"
      data-blocking={blocking}
    >
      <div className="flex items-start gap-3">
        <Users aria-hidden className="text-caution-foreground mt-0.5 size-5 shrink-0" />
        <div>
          <p className="text-sm font-semibold">{t("duplicatesTitle")}</p>
          <p className="text-muted-foreground text-sm">{t("duplicatesHint")}</p>
        </div>
      </div>

      <ul className="space-y-2">
        {candidates.map(({ patient, reason }) => (
          <li
            key={patient.id}
            className="bg-background flex flex-wrap items-center gap-3 rounded-md border px-3 py-2"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{patient.full_name}</p>
              <p className="text-muted-foreground tabular text-xs">
                {patient.uhid} · {patient.phone}
              </p>
              <p className="text-caution-foreground text-xs">{reason}</p>
            </div>
            <LinkButton variant="outline" size="sm" className="h-tap shrink-0" href={`/patients/${patient.id}`}>
              {t("useExisting")}
            </LinkButton>
          </li>
        ))}
      </ul>

      {blocking ? (
        <label className="flex min-h-tap cursor-pointer items-center gap-3 text-sm font-medium">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => onConfirm(event.target.checked)}
            className="border-input accent-primary size-5 rounded"
          />
          {confirmed ? t("confirmedNotDuplicate") : t("registerAnyway")}
        </label>
      ) : null}
    </section>
  );
}
