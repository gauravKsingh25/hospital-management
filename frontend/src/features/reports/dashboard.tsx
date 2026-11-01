"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AutoClosedNote,
  FollowUpSection,
  FootfallBreakdown,
  FootfallStats,
  InpatientSection,
  OccupancySection,
  RevenueSection,
  TodaySection,
} from "@/features/reports/sections";
import { formatLocalDate } from "@/features/reports/format";
import { useQueueSnapshot } from "@/features/reports/queue-snapshot";
import { Section } from "@/features/reports/stat";
import { PERIODS, periodDays, type Period } from "@/features/reports/windows";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type { Dashboard, DateWindow } from "@/types/api";

/**
 * The KPI dashboard (CLAUDE.md §13 step 10).
 *
 * ## Laid out by the question it is opened to answer
 *
 * "What came in today?" first: one period filter above everything, opening on
 * Today, then the money, then patients, then what is happening right now,
 * then beds and the clinical outcomes. Long lists fold away. The filter used
 * to sit inside the Patients section while quietly changing the money below
 * it as well, which is the kind of thing that makes a number untrustworthy.
 *
 * ## One request, not six
 *
 * `/reports/dashboard` returns every section the caller's permissions allow,
 * with the rest as `null`. That is the backend's choice and this screen honours
 * it: composing six endpoints here would mean six round trips, a layout that
 * reflows as they land, and the permission logic written twice.
 *
 * ## Two queries, though — and the second one is the point
 *
 * The dashboard aggregates are expensive: a ninety-day footfall query, an ALOS
 * over every discharge, a revenue rollup. Polling all of that every thirty
 * seconds to keep a *waiting count* current would be a lot of database work to
 * move one number. So the report body is fetched once per period, and the live
 * strip — today's queue, and the delay alerts on it — polls `/reports/queue`
 * on its own. That endpoint reads one day.
 *
 * ## The chart is loaded lazily
 *
 * Recharts is the heaviest dependency in the frontend and only this screen uses
 * it, so it is behind `dynamic()` (CLAUDE.md §4: lazy-load heavy widgets). The
 * rest of the dashboard is drawn with `BarList`, which is CSS. On Today there
 * is no chart at all — one bar is not a trend — so the chunk is not even fetched.
 */

const FootfallChart = dynamic(
  () => import("@/features/reports/footfall-chart").then((module) => module.FootfallChart),
  {
    // No SSR: the chart measures its container to size itself, so rendering it
    // on the server produces markup that is thrown away and re-measured.
    ssr: false,
    loading: () => <Skeleton className="h-[240px] w-full rounded-lg" />,
  },
);

export function ReportsDashboard({
  initial,
  initialPeriod,
}: {
  initial: Dashboard;
  initialPeriod: Period;
}) {
  const t = useTranslations("reports");
  const [period, setPeriod] = useState<Period>(initialPeriod);

  const dashboard = useQuery({
    queryKey: ["reports", "dashboard", period],
    queryFn: ({ signal }) =>
      api.get<Dashboard>(`/reports/dashboard?trailing_days=${periodDays(period)}`, { signal }),
    // Only the period the page was opened on is server-rendered.
    initialData: period === initialPeriod ? initial : undefined,
    // Keep the previous period's numbers on screen while the new ones load.
    // Without this the whole page empties on every click of the filter —
    // headings and all — which reads as the dashboard breaking rather than
    // thinking. The filter dims instead.
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });

  const queue = useQueueSnapshot({
    initial: initial.queue,
    // Absent for a caller without `report:operational`; asking anyway would be
    // a guaranteed 403 on every poll.
    enabled: initial.queue !== null && initial.queue !== undefined,
  });

  /**
   * Choose a period, and write it into the address.
   *
   * `replaceState` rather than a router navigation: a navigation would re-run
   * the server component and fetch the dashboard a second time, on top of the
   * query above. Next keeps `useSearchParams` in step with the native history
   * API, and a replace rather than a push keeps Back meaning "the page before
   * Reports" instead of stepping through every period somebody tried.
   */
  const choose = (next: Period) => {
    setPeriod(next);
    const url = new URL(window.location.href);
    url.searchParams.set("period", next);
    window.history.replaceState(window.history.state, "", url);
  };

  const data = dashboard.data;
  const footfall = data?.footfall;
  const covered = reportWindow(data);

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
          {covered ? (
            <p className="text-muted-foreground text-sm" data-testid="period-dates">
              {covered.date_from === covered.date_to
                ? t("datesOneDay", { date: formatLocalDate(covered.date_to) })
                : t("datesRange", {
                    from: formatLocalDate(covered.date_from),
                    to: formatLocalDate(covered.date_to),
                  })}
            </p>
          ) : null}
        </div>
        <PeriodPicker value={period} onChange={choose} busy={dashboard.isFetching} />
      </header>

      <RevenueSection report={data?.revenue} />

      {footfall ? (
        <Section id="report-footfall" title={t("footfallTitle")}>
          <FootfallStats report={footfall} />
          <AutoClosedNote report={footfall} />
          {period === "today" ? null : <FootfallChart days={footfall.by_day ?? []} />}
          <FootfallBreakdown report={footfall} />
        </Section>
      ) : null}

      <TodaySection queue={queue.data ?? data?.queue} />
      <OccupancySection report={data?.occupancy} />
      <InpatientSection report={data?.inpatient} />
      <FollowUpSection report={data?.follow_up} />

      {data ? (
        <p className="text-muted-foreground text-xs">
          {t("generatedFor", { date: formatLocalDate(data.generated_for) })}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The dates the numbers cover, from the response rather than from the button
 * that was pressed — so the line under the title can never claim a period the
 * server did not answer for. Every windowed report shares one window; footfall
 * is always present (the page requires `report:operational`), the rest are
 * fallbacks.
 */
function reportWindow(data: Dashboard | undefined): DateWindow | null {
  return (
    data?.footfall?.window ??
    data?.revenue?.window ??
    data?.inpatient?.window ??
    data?.follow_up?.window ??
    null
  );
}

/**
 * Today, 7, 30 or 90 days. Fixed choices rather than a date-range picker: a
 * manager asking "how was today" or "how was last month" wants one click, and
 * the arbitrary range — which the API supports on the individual report
 * endpoints — is a different, rarer job.
 */
function PeriodPicker({
  value,
  onChange,
  busy,
}: {
  value: Period;
  onChange: (period: Period) => void;
  busy: boolean;
}) {
  const t = useTranslations("reports");

  return (
    <div className="flex flex-wrap gap-1" role="group" aria-label={t("period")}>
      {PERIODS.map((period) => (
        <Button
          key={period}
          variant={period === value ? "default" : "outline"}
          className={cn("h-tap min-w-tap px-4", busy && period === value && "opacity-70")}
          aria-pressed={period === value}
          onClick={() => onChange(period)}
          data-testid={`period-${period}`}
        >
          {period === "today" ? t("periodToday") : t("periodDays", { days: periodDays(period) })}
        </Button>
      ))}
    </div>
  );
}
