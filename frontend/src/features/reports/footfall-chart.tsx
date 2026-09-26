"use client";

import { useTranslations } from "next-intl";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatAxisDate, formatLocalDate } from "@/features/reports/format";
import type { DailyCount } from "@/types/api";

/**
 * Footfall by day — the one genuine time series on the dashboard.
 *
 * ## Why this file is loaded lazily
 *
 * Recharts is by a wide margin the heaviest thing the frontend depends on. It
 * is the stack CLAUDE.md §4 fixes, and §4 also says to lazy-load charts — so
 * this module is imported through `next/dynamic` from `dashboard.tsx` and
 * never reaches the initial bundle. That matters more than it sounds: the
 * screens staff live in are the queue, the chart and the counter, and none of
 * them should pay for a chart library that only management opens.
 *
 * Everything else on the dashboard is a proportion and is drawn with `BarList`
 * at no cost. This is the exception, because a month of daily counts genuinely
 * needs an axis to be read.
 *
 * ## Stacked, not grouped
 *
 * `seen` and `lost` stack, so the height of a bar is the day's total footfall
 * and the shaded part is the share that never reached a doctor. Side-by-side
 * bars would show the same two numbers and hide the relationship between them,
 * which is the entire point of putting them on one chart.
 */
export function FootfallChart({ days }: { days: DailyCount[] }) {
  const t = useTranslations("reports");

  // Short dates: thirty full dates on an axis overlap into a grey smear. The
  // full date is still in the tooltip, where somebody reading one bar wants it.
  const data = days.map((day) => ({
    day: day.day,
    short: formatAxisDate(day.day),
    seen: day.seen,
    lost: day.lost,
    // Visits that were neither seen nor lost — still open, or closed without a
    // consultation. Shown so the bar totals to the day's footfall rather than
    // quietly under-reporting it.
    other: Math.max(0, day.total - day.seen - day.lost),
  }));

  return (
    <div className="rounded-lg border p-4">
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -20 }} accessibilityLayer>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
          <XAxis
            dataKey="short"
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            minTickGap={16}
          />
          <YAxis
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={false}
            allowDecimals={false}
          />
          <Tooltip
            cursor={{ fill: "var(--muted)" }}
            contentStyle={{
              background: "var(--popover)",
              border: "1px solid var(--border)",
              borderRadius: "var(--radius-md)",
              fontSize: 12,
            }}
            labelFormatter={(_label, payload) => {
              const day = payload?.[0]?.payload as { day?: string } | undefined;
              return day?.day ? formatLocalDate(day.day) : "";
            }}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Bar dataKey="seen" name={t("seen")} stackId="a" fill="var(--chart-1)" />
          <Bar dataKey="other" name={t("openOrClosed")} stackId="a" fill="var(--chart-3)" />
          <Bar dataKey="lost" name={t("lost")} stackId="a" fill="var(--chart-5)" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
