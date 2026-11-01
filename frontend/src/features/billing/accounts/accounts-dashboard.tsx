"use client";

import { Download, Search } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Pager } from "@/components/pager";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { BillsTable, PatientsTable, PaymentsTable } from "@/features/billing/accounts/accounts-tables";
import { exportCsv, type Column } from "@/features/billing/accounts/csv";
import { TotalsStrip } from "@/features/billing/accounts/totals-strip";
import {
  ACCOUNTS_PAGE_SIZE,
  presetPeriod,
  tabQuery,
  useAccountsList,
  useAccountsSummary,
  type AccountsTab,
  type Period,
  type Preset,
} from "@/features/billing/accounts/use-accounts";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { BillRow, PatientRow, PaymentRow } from "@/types/api";

/**
 * Billing → Accounts: the accountant's home.
 *
 * Totals for the period across the top, then four read-only lists, all
 * newest first. Search reaches patient name, UHID, bill and receipt numbers.
 * Nothing on this screen changes money — IPD corrections happen on the
 * patient's bill page, and only for the accountant and the hospital admin.
 */
const PRESETS: Preset[] = ["today", "7d", "month", "custom"];
const TABS: AccountsTab[] = ["bills", "payments", "outstanding", "patients"];

export function AccountsDashboard() {
  const t = useTranslations("accounts");
  const [preset, setPreset] = useState<Preset>("today");
  const [custom, setCustom] = useState<Period>(() => presetPeriod("today"));
  const [tab, setTab] = useState<AccountsTab>("bills");
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [exporting, setExporting] = useState(false);

  // Typing settles before it searches: one request per pause, not per key.
  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(search);
      setOffset(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const period = preset === "custom" ? custom : presetPeriod(preset);
  const summary = useAccountsSummary(period);
  const list = useAccountsList(tab, period, q, offset);

  const choose = (next: AccountsTab) => {
    setTab(next);
    setOffset(0);
  };

  const download = async () => {
    setExporting(true);
    try {
      const count = await exportCsv(
        `/billing/accounts/${tab}`,
        tabQuery(tab, period, q),
        COLUMNS[tab] as Column<unknown>[],
        `${tab}-${period.from}-to-${period.to}.csv`,
      );
      toast.success(t("exported", { count }));
    } catch (failure) {
      toast.error(isApiError(failure) ? failure.message : String(failure));
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-2">
        <div role="group" aria-label={t("period")} className="flex flex-wrap gap-1">
          {PRESETS.map((option) => (
            <Button
              key={option}
              type="button"
              variant={preset === option ? "default" : "outline"}
              className="h-tap"
              aria-pressed={preset === option}
              onClick={() => {
                setPreset(option);
                setOffset(0);
              }}
            >
              {t(`preset_${option}` as "preset_today")}
            </Button>
          ))}
        </div>
        {preset === "custom" ? (
          <div className="flex items-end gap-2">
            <label className="space-y-1 text-xs">
              <span className="text-muted-foreground">{t("from")}</span>
              <Input
                type="date"
                value={custom.from}
                max={custom.to}
                onChange={(event) => setCustom((c) => ({ ...c, from: event.target.value || c.from }))}
                className="h-tap"
              />
            </label>
            <label className="space-y-1 text-xs">
              <span className="text-muted-foreground">{t("to")}</span>
              <Input
                type="date"
                value={custom.to}
                min={custom.from}
                onChange={(event) => setCustom((c) => ({ ...c, to: event.target.value || c.to }))}
                className="h-tap"
              />
            </label>
          </div>
        ) : null}
        <div className="relative ml-auto min-w-64 flex-1 sm:max-w-sm">
          <Search
            aria-hidden
            className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
          />
          <Input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t("searchPlaceholder")}
            aria-label={t("search")}
            className="h-tap pl-9"
          />
        </div>
        <Button type="button" variant="outline" className="h-tap" onClick={download} disabled={exporting}>
          <Download aria-hidden />
          {t("export")}
        </Button>
      </div>

      <TotalsStrip summary={summary.data} />

      <div role="tablist" aria-label={t("lists")} className="flex flex-wrap gap-1 border-b">
        {TABS.map((option) => (
          <button
            key={option}
            type="button"
            role="tab"
            aria-selected={tab === option}
            onClick={() => choose(option)}
            className={cn(
              "h-tap -mb-px border-b-2 px-4 text-sm font-medium",
              tab === option
                ? "border-primary text-foreground"
                : "text-muted-foreground hover:text-foreground border-transparent",
            )}
          >
            {t(`tab_${option}` as "tab_bills")}
          </button>
        ))}
      </div>

      <div role="tabpanel" aria-label={t(`tab_${tab}` as "tab_bills")} className="space-y-3">
        {list.isPending ? (
          <div className="space-y-2" aria-busy="true">
            {[0, 1, 2].map((row) => (
              <Skeleton key={row} className="h-12 w-full rounded-lg" />
            ))}
          </div>
        ) : list.data && list.data.items.length === 0 ? (
          <p className="text-muted-foreground rounded-lg border border-dashed py-12 text-center text-sm">
            {t(`empty_${tab}` as "empty_bills")}
          </p>
        ) : list.data ? (
          <>
            {tab === "payments" ? (
              <PaymentsTable rows={list.data.items as PaymentRow[]} caption={t("tab_payments")} />
            ) : tab === "patients" ? (
              <PatientsTable rows={list.data.items as PatientRow[]} caption={t("tab_patients")} />
            ) : (
              <BillsTable rows={list.data.items as BillRow[]} caption={t(`tab_${tab}`)} />
            )}
            <Pager
              page={list.data}
              offset={offset}
              pageSize={ACCOUNTS_PAGE_SIZE}
              onOffsetChange={setOffset}
            />
          </>
        ) : null}
      </div>
    </div>
  );
}

const BILL_COLUMNS: Column<BillRow>[] = [
  { header: "Date", value: (row) => row.issued_at ?? row.created_at },
  { header: "Bill no.", value: (row) => row.invoice_number },
  { header: "Patient", value: (row) => row.patient_name },
  { header: "UHID", value: (row) => row.uhid },
  { header: "Visit", value: (row) => row.scope },
  { header: "Admission no.", value: (row) => row.admission_number },
  { header: "Stay days", value: (row) => row.stay_days },
  { header: "Status", value: (row) => row.status },
  { header: "Amount", value: (row) => row.grand_total },
  { header: "Paid", value: (row) => row.amount_paid },
  { header: "Balance", value: (row) => row.balance_due },
];

const COLUMNS: Record<AccountsTab, Column<never>[]> = {
  bills: BILL_COLUMNS,
  outstanding: BILL_COLUMNS,
  payments: [
    { header: "Date", value: (row: PaymentRow) => row.received_at },
    { header: "Receipt no.", value: (row: PaymentRow) => row.receipt_number },
    { header: "Patient", value: (row: PaymentRow) => row.patient_name },
    { header: "UHID", value: (row: PaymentRow) => row.uhid },
    { header: "Bill no.", value: (row: PaymentRow) => row.invoice_number },
    { header: "Method", value: (row: PaymentRow) => row.method },
    { header: "Reference", value: (row: PaymentRow) => row.reference },
    { header: "Status", value: (row: PaymentRow) => row.status },
    { header: "Amount", value: (row: PaymentRow) => row.amount },
    { header: "Received by", value: (row: PaymentRow) => row.received_by_name },
  ] as Column<never>[],
  patients: [
    { header: "Patient", value: (row: PatientRow) => row.name },
    { header: "UHID", value: (row: PatientRow) => row.uhid },
    { header: "Last activity", value: (row: PatientRow) => row.last_activity_at },
    { header: "Visits", value: (row: PatientRow) => row.visits },
    { header: "Billed", value: (row: PatientRow) => row.billed },
    { header: "Paid", value: (row: PatientRow) => row.paid },
    { header: "Due", value: (row: PatientRow) => row.due },
  ] as Column<never>[],
};
