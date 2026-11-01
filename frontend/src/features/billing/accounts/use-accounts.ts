"use client";

import { useQuery } from "@tanstack/react-query";

import { api, type QueryValue } from "@/lib/api/client";
import type { AccountsSummary, BillRow, Page, PatientRow, PaymentRow } from "@/types/api";

/**
 * The accounts dashboard's data, one hook per question.
 *
 * Every figure is the server's. The period is the only thing worked out
 * here, and it is dates, not money: the backend turns them into the
 * hospital's own midnights.
 */
export type Preset = "today" | "7d" | "month" | "custom";
export type Period = { from: string; to: string };
export type AccountsTab = "bills" | "payments" | "outstanding" | "patients";

export const ACCOUNTS_PAGE_SIZE = 25;

function isoDate(day: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}

export function presetPeriod(preset: Exclude<Preset, "custom">, now = new Date()): Period {
  const to = isoDate(now);
  if (preset === "today") return { from: to, to };
  if (preset === "7d") {
    const start = new Date(now);
    start.setDate(start.getDate() - 6);
    return { from: isoDate(start), to };
  }
  return { from: isoDate(new Date(now.getFullYear(), now.getMonth(), 1)), to };
}

export function useAccountsSummary(period: Period) {
  return useQuery({
    queryKey: ["billing", "accounts", "summary", period],
    queryFn: ({ signal }) =>
      api.get<AccountsSummary>("/billing/accounts/summary", { query: period, signal }),
    refetchInterval: 30_000,
  });
}

type Row = { bills: BillRow; payments: PaymentRow; outstanding: BillRow; patients: PatientRow };

/** The query a tab sends: outstanding and patients ignore the period by design. */
export function tabQuery(
  tab: AccountsTab,
  period: Period,
  q: string,
): Record<string, QueryValue> {
  const dated = tab === "bills" || tab === "payments";
  return { ...(dated ? period : {}), q: q.trim() || undefined };
}

export function useAccountsList<T extends AccountsTab>(
  tab: T,
  period: Period,
  q: string,
  offset: number,
) {
  return useQuery({
    queryKey: ["billing", "accounts", tab, period, q, offset],
    queryFn: ({ signal }) =>
      api.get<Page<Row[T]>>(`/billing/accounts/${tab}`, {
        query: { ...tabQuery(tab, period, q), limit: ACCOUNTS_PAGE_SIZE, offset },
        signal,
      }),
    // Keep the old page on screen while the next one loads — but only within
    // a tab: another tab's rows are a different shape.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[2] === tab ? previous : undefined,
  });
}
