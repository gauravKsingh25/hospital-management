"use client";

import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Analyte, DiagnosticReport, ResultValue } from "@/types/api";

/**
 * Entering and signing one report.
 *
 * Three things here are safety features rather than layout:
 *
 * **The patient's name, age and sex sit above the values and stay there.** A
 * numeric result means nothing without the band it is judged against, and the
 * band is chosen by sex and age — so a screen that shows numbers without them
 * is a screen where the wrong reference range goes unnoticed. The backend
 * carries them on the report for the same reason.
 *
 * **Flags are never entered by hand.** The technician types the measurement;
 * the server decides normal, high, low or critical against the band that fits
 * this patient. Letting the bench overrule a range would defeat having ranges.
 *
 * **Verification is a separate act from entry, and the person who entered
 * cannot do it.** The backend refuses (`assert_may_verify`); this screen does
 * not offer the button rather than letting someone press it and be told no.
 */
export function ReportScreen({
  initialReport,
  analytes,
  canEnter,
  canVerify,
}: {
  initialReport: DiagnosticReport;
  analytes: Analyte[];
  canEnter: boolean;
  canVerify: boolean;
}) {
  const t = useTranslations("lab");
  const common = useTranslations("common");
  const router = useRouter();

  const [report, setReport] = useState(initialReport);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      (report.results ?? []).map((result) => [
        result.analyte_code,
        result.value_numeric ?? result.value_text ?? "",
      ]),
    ),
  );
  const [findings, setFindings] = useState(report.findings ?? "");
  const [impression, setImpression] = useState(report.impression ?? "");

  const isRadiology = report.discipline === "RADIOLOGY";
  // A verified report is superseded, never rewritten (CLAUDE.md's audit rule
  // rendered: an amendment leaves both versions and a reason).
  const editable = canEnter && ["REGISTERED", "IN_PROGRESS", "PRELIMINARY"].includes(report.status);

  const saveResults = useMutation({
    mutationFn: () =>
      api.post<DiagnosticReport>(`/diagnostics/reports/${report.id}/results`, {
        results: Object.entries(values)
          .filter(([, raw]) => raw.trim() !== "")
          .map(([analyte_code, raw]) => {
            const analyte = analytes.find((item) => item.code === analyte_code);
            return analyte?.is_numeric === false
              ? { analyte_code, value_text: raw.trim() }
              : { analyte_code, value_numeric: raw.trim() };
          }),
      }),
    onSuccess: (updated) => {
      setReport(updated);
      toast.success(t("resultsSaved"));
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const saveNarrative = useMutation({
    mutationFn: () =>
      api.post<DiagnosticReport>(`/diagnostics/reports/${report.id}/narrative`, {
        findings: findings.trim() || null,
        impression: impression.trim() || null,
      }),
    onSuccess: (updated) => {
      setReport(updated);
      toast.success(t("resultsSaved"));
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const verify = useMutation({
    mutationFn: () => api.post<DiagnosticReport>(`/diagnostics/reports/${report.id}/verify`),
    onSuccess: (updated) => {
      setReport(updated);
      toast.success(t("verified"));
      // Back to the bench: this request is finished, and if it was the last
      // thing holding the visit open the encounter has just closed itself.
      router.push("/lab");
      router.refresh();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const entered = Object.values(values).some((raw) => raw.trim() !== "");
  const hasNarrative = findings.trim() !== "" || impression.trim() !== "";
  const resultsFiled = (report.results ?? []).length > 0 || Boolean(report.findings ?? report.impression);

  return (
    <div className="space-y-6">
      <ReportHeader report={report} />

      {isRadiology ? (
        <section className="space-y-3 rounded-lg border p-4" aria-labelledby="narrative-heading">
          <h2 id="narrative-heading" className="text-sm font-semibold">
            {t("narrative")}
          </h2>

          <div className="space-y-1.5">
            <Label htmlFor="findings">{t("findings")}</Label>
            <Textarea
              id="findings"
              value={findings}
              onChange={(event) => setFindings(event.target.value)}
              disabled={!editable}
              rows={6}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="impression">{t("impression")}</Label>
            <Textarea
              id="impression"
              value={impression}
              onChange={(event) => setImpression(event.target.value)}
              disabled={!editable}
              rows={3}
            />
          </div>

          {editable ? (
            <Button
              className="h-tap"
              disabled={!hasNarrative || saveNarrative.isPending}
              onClick={() => saveNarrative.mutate()}
            >
              {common("save")}
            </Button>
          ) : null}
        </section>
      ) : (
        <section className="space-y-3 rounded-lg border p-4" aria-labelledby="results-heading">
          <h2 id="results-heading" className="text-sm font-semibold">
            {t("results")}
          </h2>

          {analytes.length === 0 ? (
            <p className="text-muted-foreground text-sm">{t("noAnalytes")}</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">{t("results")}</caption>
                <thead className="text-muted-foreground">
                  <tr>
                    <th scope="col" className="py-2 text-left font-medium">
                      {t("analyte")}
                    </th>
                    <th scope="col" className="py-2 text-left font-medium">
                      {t("value")}
                    </th>
                    <th scope="col" className="py-2 text-left font-medium">
                      {t("referenceRange")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-border divide-y">
                  {analytes.map((analyte) => (
                    <AnalyteRow
                      key={analyte.id}
                      analyte={analyte}
                      filed={(report.results ?? []).find(
                        (result) => result.analyte_code === analyte.code,
                      )}
                      value={values[analyte.code] ?? ""}
                      editable={editable}
                      onChange={(next) =>
                        setValues((current) => ({ ...current, [analyte.code]: next }))
                      }
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {editable && analytes.length > 0 ? (
            <Button
              className="h-tap"
              disabled={!entered || saveResults.isPending}
              onClick={() => saveResults.mutate()}
            >
              {t("saveResults")}
            </Button>
          ) : null}
        </section>
      )}

      {canVerify && report.status !== "FINAL" && report.status !== "AMENDED" ? (
        <section className="space-y-3 rounded-lg border p-4" aria-labelledby="verify-heading">
          <h2 id="verify-heading" className="text-sm font-semibold">
            {t("verifyTitle")}
          </h2>
          <p className="text-muted-foreground text-sm">{t("verifyHint")}</p>
          <Button
            className="h-tap"
            disabled={!resultsFiled || verify.isPending}
            onClick={() => verify.mutate()}
          >
            {t("verify")}
          </Button>
        </section>
      ) : null}
    </div>
  );
}

/**
 * Who this report belongs to, and what it was judged against.
 *
 * Deliberately not collapsible and not below the fold. This is the
 * lab's equivalent of the patient safety banner on the clinical screens.
 */
function ReportHeader({ report }: { report: DiagnosticReport }) {
  const t = useTranslations("lab");
  const common = useTranslations("common");

  return (
    <header
      className={cn(
        "rounded-lg border p-4",
        report.patient_is_deceased ? "border-foreground/40 bg-foreground/5" : "bg-muted/30",
      )}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            {report.patient_name ?? common("notRecorded")}
          </h1>
          <p className="text-muted-foreground tabular text-sm">
            {report.patient_uhid}
            {report.patient_age_years !== null && report.patient_age_years !== undefined
              ? ` · ${t("years", { count: report.patient_age_years })}`
              : ""}
            {report.patient_gender ? ` · ${report.patient_gender}` : ""}
          </p>
        </div>
        <div className="text-right">
          <div className="font-medium">{report.test_name}</div>
          <div className="text-muted-foreground tabular text-xs">{report.report_number}</div>
        </div>
      </div>

      {report.patient_is_deceased ? (
        <p className="text-foreground mt-2 text-sm font-medium">{t("patientDeceased")}</p>
      ) : null}

      {report.verified_by_name ? (
        <p className="text-muted-foreground mt-2 text-xs">
          {t("verifiedBy", {
            name: report.verified_by_name,
            registration: report.verifier_registration_number ?? "—",
          })}
        </p>
      ) : null}
    </header>
  );
}

function AnalyteRow({
  analyte,
  filed,
  value,
  editable,
  onChange,
}: {
  analyte: Analyte;
  filed: ResultValue | undefined;
  value: string;
  editable: boolean;
  onChange: (value: string) => void;
}) {
  const t = useTranslations("lab");

  const critical = filed?.is_critical ?? false;
  const abnormal = filed !== undefined && filed.flag !== "NORMAL";

  return (
    <tr>
      <th scope="row" className="py-2 pr-3 text-left font-normal">
        {analyte.name}
        {analyte.unit ? <span className="text-muted-foreground"> ({analyte.unit})</span> : null}
      </th>

      <td className="py-2 pr-3">
        <div className="flex items-center gap-2">
          <Input
            value={value}
            onChange={(event) => onChange(event.target.value)}
            disabled={!editable}
            inputMode={analyte.is_numeric ? "decimal" : "text"}
            className="h-tap tabular w-32"
            aria-label={analyte.name}
          />
          {abnormal ? (
            <span
              className={cn(
                "rounded px-1.5 py-0.5 text-xs font-semibold",
                critical
                  ? "bg-critical/15 text-critical"
                  : "bg-caution/20 text-caution-foreground",
              )}
            >
              {t(`flag${filed.flag}` as "flagHIGH")}
            </span>
          ) : null}
        </div>
      </td>

      <td className="text-muted-foreground tabular py-2 text-xs">
        {filed?.ref_text
          ? filed.ref_text
          : filed?.ref_low !== null && filed?.ref_low !== undefined
            ? `${trim(filed.ref_low)} – ${trim(filed.ref_high)}`
            : "—"}
      </td>
    </tr>
  );
}

/** Money and measurements arrive as decimal strings; drop the trailing zeros. */
function trim(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const asNumber = Number(value);
  return Number.isFinite(asNumber) ? String(asNumber) : value;
}
