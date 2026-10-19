import { api, type QueryValue } from "@/lib/api/client";
import type { Page } from "@/types/api";

/**
 * CSV of a dashboard tab, built in the browser from the same endpoint the
 * table reads — so the export is exactly what the screen showed, with the
 * server's amounts as written (no arithmetic here).
 *
 * Capped at 5,000 rows: a year of bills for a mid-size hospital is far more,
 * and that is a report to run on the server, not a download from a tab.
 */
const PAGE = 200;
const CAP = 5_000;

export type Column<T> = { header: string; value: (row: T) => string | number | null | undefined };

function cell(value: string | number | null | undefined): string {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export async function exportCsv<T>(
  path: string,
  query: Record<string, QueryValue>,
  columns: Column<T>[],
  filename: string,
): Promise<number> {
  const rows: T[] = [];
  for (let offset = 0; offset < CAP; offset += PAGE) {
    const page = await api.get<Page<T>>(path, { query: { ...query, limit: PAGE, offset } });
    rows.push(...page.items);
    if (!page.has_more) break;
  }
  const lines = [
    columns.map((column) => cell(column.header)).join(","),
    ...rows.slice(0, CAP).map((row) => columns.map((column) => cell(column.value(row))).join(",")),
  ];
  // A byte-order mark so Excel reads the rupee sign and Hindi names correctly.
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
  return rows.length;
}
