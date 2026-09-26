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
import { DEFAULT_TRAILING_DAYS, REPORT_WINDOWS } from "@/features/reports/windows";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type { Dashboard } from "@/types/api";

/**
 * The KPI dashboard (CLAUDE.md §13 step 10).
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
 * move one number. So the report body is fetched once per window, and the live
 * strip — today's queue, and the delay alerts on it — polls `/reports/queue`
 * on its own. That endpoint reads one day.
 *
 * ## The chart is loaded lazily
 *
 * Recharts is the heaviest dependency in the frontend and only this screen uses
 * it, so it is behind `dynamic()` (CLAUDE.md §4: lazy-load heavy widgets). The
 * rest of the dashboard is drawn with `BarList`, which is CSS.
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

export function ReportsDashboard({ initial }: { initial: Dashboard }) {
  const t = useTranslations("reports");
  const [trailingDays, setTrailingDays] = useState<number>(DEFAULT_TRAILING_DAYS);

  const dashboard = useQuery({
    queryKey: ["reports", "dashboard", trailingDays],
    queryFn: ({ signal }) =>
      api.get<Dashboard>(`/reports/dashboard?trailing_days=${trailingDays}`, { signal }),
    // Only the default window is server-rendered; the others are fetched.
    initialData: trailingDays === DEFAULT_TRAILING_DAYS ? initial : undefined,
    // Keep the previous window's numbers on screen while the new ones load.
    // Without this the whole page empties on every click of the picker —
    // headings and all — which reads as the dashboard breaking rather than
    // thinking. The picker dims instead.
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });

  const queue = useQueueSnapshot({
    initial: initial.queue,
    // Absent for a caller without `report:operational`; asking anyway would be
    // a guaranteed 403 on every poll.
    enabled: initial.queue !== null && initial.queue !== undefined,
  });

  const data = dashboard.data;
  const footfall = data?.footfall;

  return (
    <div className="space-y-8">
      <TodaySection queue={queue.data ?? data?.queue} />

      {footfall ? (
        <Section
          id="report-footfall"
          title={t("footfallTitle")}
          hint={t("windowHint", {
            from: formatLocalDate(footfall.window.date_from),
            to: formatLocalDate(footfall.window.date_to),
          })}
          action={
            <WindowPicker
              value={trailingDays}
              onChange={setTrailingDays}
              busy={dashboard.isFetching}
            />
          }
        >
          <FootfallStats report={footfall} />
          <AutoClosedNote report={footfall} />
          <FootfallChart days={footfall.by_day ?? []} />
          <FootfallBreakdown report={footfall} />
        </Section>
      ) : null}

      <OccupancySection report={data?.occupancy} />
      <InpatientSection report={data?.inpatient} />
      <FollowUpSection report={data?.follow_up} />
      <RevenueSection report={data?.revenue} />

      {data ? (
        <p className="text-muted-foreground text-xs">
          {t("generatedFor", { date: formatLocalDate(data.generated_for) })}
        </p>
      ) : null}
    </div>
  );
}

/**
 * How far back the report looks.
 *
 * Three fixed windows rather than a date-range picker. A manager asking "how
 * was last month" wants one click, and the arbitrary range — which the API
 * supports on the individual report endpoints — is a different, rarer job.
 */
function WindowPicker({
  value,
  onChange,
  busy,
}: {
  value: number;
  onChange: (days: number) => void;
  busy: boolean;
}) {
  const t = useTranslations("reports");

  return (
    <div className="flex gap-1" role="group" aria-label={t("window")}>
      {REPORT_WINDOWS.map((days) => (
        <Button
          key={days}
          size="sm"
          variant={days === value ? "default" : "outline"}
          className={cn("h-tap", busy && days === value && "opacity-70")}
          aria-pressed={days === value}
          onClick={() => onChange(days)}
          data-testid={`window-${days}`}
        >
          {t("lastDays", { days })}
        </Button>
      ))}
    </div>
  );
}
