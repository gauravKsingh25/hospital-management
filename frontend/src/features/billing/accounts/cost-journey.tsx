"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { Pager } from "@/components/pager";
import { Skeleton } from "@/components/ui/skeleton";
import { CATEGORIES } from "@/features/billing/accounts/ipd-bill-editor";
import { formatMoney } from "@/features/billing/money";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type { ChargeCategory, Journey } from "@/types/api";

/**
 * The patient's whole journey, itemised, latest first (spec §5.4): every
 * medicine, consumable, test, room-day and consultation charged across every
 * visit, with totals by category for the current filter. Not a ranking, and
 * read only — corrections belong to the IPD bill editor.
 */
const PAGE_SIZE = 50;

export function CostJourney({ patientId }: { patientId: string }) {
  const t = useTranslations("journey");
  const categories = useTranslations("categories");
  const format = useFormatter();
  const [category, setCategory] = useState<ChargeCategory | "">("");
  const [offset, setOffset] = useState(0);

  const journey = useQuery({
    queryKey: ["billing", "journey", patientId, category, offset],
    queryFn: ({ signal }) =>
      api.get<Journey>(`/billing/patients/${patientId}/journey`, {
        query: { category: category || undefined, limit: PAGE_SIZE, offset },
        signal,
      }),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <label htmlFor="journey-category" className="text-sm font-medium">
            {t("category")}
          </label>
          <select
            id="journey-category"
            value={category}
            onChange={(event) => {
              setCategory(event.target.value as ChargeCategory | "");
              setOffset(0);
            }}
            className="border-input bg-background h-tap block rounded-md border px-3 text-base"
          >
            <option value="">{t("allCategories")}</option>
            {CATEGORIES.map((option) => (
              <option key={option} value={option}>
                {categories(option)}
              </option>
            ))}
          </select>
        </div>
        {journey.data ? (
          <ul className="flex flex-wrap gap-2" data-testid="journey-totals" aria-label={t("totals")}>
            {Object.entries(journey.data.totals_by_category).map(([key, amount]) => (
              <li key={key} className="rounded-full border px-3 py-1 text-sm">
                {categories(key as "OTHER")}{" "}
                <span className="tabular font-medium">{formatMoney(amount)}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {journey.isPending ? (
        <Skeleton className="h-40 w-full rounded-lg" />
      ) : journey.data && journey.data.lines.items.length === 0 ? (
        <p className="text-muted-foreground rounded-lg border border-dashed py-10 text-center text-sm">
          {t("empty")}
        </p>
      ) : journey.data ? (
        <>
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full min-w-2xl text-sm">
              <caption className="sr-only">{t("title")}</caption>
              <thead className="bg-muted/50 text-muted-foreground">
                <tr>
                  <th scope="col" className="px-3 py-2 text-left font-medium">{t("when")}</th>
                  <th scope="col" className="px-3 py-2 text-left font-medium">{t("visit")}</th>
                  <th scope="col" className="px-3 py-2 text-left font-medium">{t("what")}</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">{t("qty")}</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">{t("amount")}</th>
                  <th scope="col" className="px-3 py-2 text-left font-medium">{t("state")}</th>
                </tr>
              </thead>
              <tbody className="divide-border divide-y">
                {journey.data.lines.items.map((line) => (
                  <tr
                    key={line.charge_id}
                    data-testid="journey-line"
                    className={cn(line.status === "WAIVED" && "text-muted-foreground")}
                  >
                    <td className="tabular px-3 py-2 whitespace-nowrap">
                      {format.dateTime(new Date(line.captured_at), {
                        day: "2-digit",
                        month: "short",
                        hour: "numeric",
                        minute: "2-digit",
                      })}
                    </td>
                    <td className="px-3 py-2">{line.kind}</td>
                    <td className="px-3 py-2">
                      <div className={cn(line.status === "WAIVED" && "line-through")}>
                        {line.description}
                      </div>
                      <div className="text-muted-foreground text-xs">
                        {categories(line.category)} · {t(`source_${line.source_module}` as "source_billing")}
                      </div>
                    </td>
                    <td className="tabular px-3 py-2 text-right">{Number(line.quantity)}</td>
                    <td className="tabular px-3 py-2 text-right">{formatMoney(line.total_amount)}</td>
                    <td className="px-3 py-2 text-xs">{t(`status_${line.status}` as "status_PENDING")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager page={journey.data.lines} offset={offset} pageSize={PAGE_SIZE} onOffsetChange={setOffset} />
        </>
      ) : null}
    </div>
  );
}
