import { Stethoscope } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import type { ProblemEntry } from "@/types/api";

/**
 * "What has this patient been treated for?" — one line per condition, across
 * every visit, newest first.
 *
 * `problems === null` and `problems.length === 0` are different sentences on
 * purpose. Null is the server saying this role does not read diagnoses
 * (reception, the cash counter); empty is the chart saying nothing has been
 * diagnosed. Rendering both as "none" would tell a receptionist that a
 * diabetic has no history.
 */
export function ProblemList({ problems }: { problems: ProblemEntry[] | null | undefined }) {
  const t = useTranslations("record");
  const format = useFormatter();
  const date = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium" });

  return (
    <section className="bg-card rounded-xl border" data-testid="problem-list">
      <div className="flex items-start gap-3 border-b px-4 py-3">
        <Stethoscope aria-hidden className="text-primary mt-0.5 size-5 shrink-0" />
        <div>
          <h2 className="text-base font-semibold">{t("treatedFor")}</h2>
          <p className="text-muted-foreground text-xs">{t("treatedForHint")}</p>
        </div>
      </div>

      {problems === null || problems === undefined ? (
        <p className="text-muted-foreground px-4 py-6 text-center text-sm">{t("notShownToRole")}</p>
      ) : problems.length === 0 ? (
        <p className="text-muted-foreground px-4 py-6 text-center text-sm">{t("noProblems")}</p>
      ) : (
        <ul className="divide-y">
          {problems.map((problem) => (
            <li
              key={`${problem.code ?? ""}-${problem.description}`}
              className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3"
              data-testid="problem-row"
            >
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">
                  {problem.description}
                  {problem.code ? (
                    <span className="text-muted-foreground tabular ml-2 text-xs font-normal">
                      {problem.code}
                    </span>
                  ) : null}
                </p>
                <p className="text-muted-foreground text-xs">
                  {t("firstSeen")} {date(problem.first_recorded_at)}
                  {problem.visit_count > 1 ? ` · ${t("lastSeen")} ${date(problem.last_recorded_at)}` : ""}
                </p>
              </div>
              <span
                className={cn(
                  "rounded-full border px-2 py-0.5 text-xs font-medium",
                  problem.diagnosis_type === "FINAL"
                    ? "border-success/25 bg-success/10 text-success"
                    : "border-caution/40 bg-caution/15 text-caution-foreground",
                )}
              >
                {t(`dx${problem.diagnosis_type}` as "dxFINAL")}
              </span>
              <span className="text-muted-foreground tabular w-20 text-right text-xs">
                {t("visitTimes", { count: problem.visit_count })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
