"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";
import { Printer, UserPlus } from "lucide-react";

import { PatientQr } from "@/components/patient/qr-code";
import { Button } from "@/components/ui/button";
import { LinkButton } from "@/components/ui/link-button";
import type { QuickOpdResult } from "@/types/api";

/**
 * The token slip, shown the instant one-click OPD returns.
 *
 * Everything on it comes from the single `quick-opd` response — token
 * number, doctor, department, room and how many people are ahead. No second
 * lookup, so there is nothing to wait for between pressing the button and
 * being able to tell the patient where to go.
 *
 * The token number is the largest thing on the screen because it is read out
 * loud across a counter, often to someone who does not have their glasses on.
 *
 * Printing uses the browser's own dialog against a print stylesheet rather
 * than generating a PDF: hospital counters have a thermal or A5 printer
 * already configured, and `window.print()` reaches it with no new
 * dependency and no server round trip.
 *
 * ## The patient's identity, and the QR
 *
 * The slip used to carry a token, a doctor and a room and nothing naming the
 * person holding it — so the piece of paper a patient walked away with could
 * not be matched back to their record. It now carries their name, their UHID,
 * and the same UHID as a QR (CLAUDE.md §7b).
 *
 * The QR is the reason to keep the slip: on the next visit a scanner reads it
 * in one action instead of a receptionist typing fourteen characters with
 * somebody waiting. See `PatientQr` for why the payload is the plain UHID.
 */
export function TokenSlip({
  result,
  onRegisterAnother,
}: {
  result: QuickOpdResult;
  onRegisterAnother: () => void;
}) {
  const t = useTranslations("token");
  const headingRef = useRef<HTMLHeadingElement>(null);

  // Move focus to the result. A screen reader user who pressed "Register"
  // otherwise hears nothing change, and a keyboard user's next Tab starts
  // from wherever the old form was.
  useEffect(() => headingRef.current?.focus(), []);

  return (
    <div className="mx-auto max-w-md space-y-6">
      <div
        className="bg-card rounded-xl border p-6 text-center shadow-sm print:border-0 print:shadow-none"
        data-testid="token-slip"
      >
        <h2 ref={headingRef} tabIndex={-1} className="text-muted-foreground text-sm font-medium">
          {t("title")}
        </h2>

        {/* Who this slip belongs to, directly under the heading — before the
            token, because a slip that cannot identify its holder is a slip
            nobody can act on. */}
        <p className="mt-1 text-base font-semibold">{result.patient_name}</p>
        <p className="text-muted-foreground tabular text-xs">{result.uhid}</p>

        <p className="text-muted-foreground mt-4 text-xs tracking-widest uppercase">
          {t("tokenNumber")}
        </p>
        <p className="text-primary tabular text-7xl leading-none font-bold">
          {result.token_number}
        </p>

        <dl className="mt-6 space-y-2 text-left text-sm">
          <Row label={t("doctor")} value={result.doctor_name} />
          {result.department_name ? (
            <Row label={t("department")} value={result.department_name} />
          ) : null}
          {result.location ? <Row label={t("location")} value={result.location} /> : null}
        </dl>

        <p className="bg-muted mt-5 rounded-md px-3 py-2 text-sm font-medium print:bg-transparent print:px-0">
          {t("patientsAhead", { count: result.patients_ahead })}
        </p>

        {/* The whole reason to keep the slip: next visit, one scan instead of
            fourteen typed characters. Centred and given room — a QR crowded
            against a border loses its quiet zone to the paper edge. */}
        <div className="mt-5 flex flex-col items-center gap-1">
          <PatientQr uhid={result.uhid} />
          <p className="text-muted-foreground text-[10px]">{t("scanHint")}</p>
        </div>
      </div>

      <div className="flex flex-wrap gap-2 no-print">
        <Button onClick={() => window.print()} variant="outline" className="h-tap flex-1">
          <Printer aria-hidden className="size-4" />
          {t("printSlip")}
        </Button>
        {/* The primary action after issuing a token is the next patient, not
            admiring this one — there is a queue at the counter. */}
        <Button onClick={onRegisterAnother} className="h-tap flex-1" autoFocus>
          <UserPlus aria-hidden className="size-4" />
          {t("registerAnother")}
        </Button>
      </div>

      <div className="text-center no-print">
        <LinkButton variant="link" size="sm" href={`/consultation/${result.encounter_id}`}>
          {t("openChart")}
        </LinkButton>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b pb-2 last:border-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium">{value}</dd>
    </div>
  );
}
