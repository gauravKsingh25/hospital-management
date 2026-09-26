"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { IndianRupee } from "lucide-react";

import { Pager } from "@/components/pager";
import { EncounterStatusChip } from "@/components/status-chip";
import { LinkButton } from "@/components/ui/link-button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { useElapsed } from "@/lib/hooks/use-elapsed";
import { cn } from "@/lib/utils";
import type { AccountBoardEntry, EncounterStatus, Page } from "@/types/api";

import { formatMoney } from "@/features/billing/money";

/**
 * The cash counter's board: every visit that still owes something.
 *
 * Not the invoice list, and the difference matters. A visit whose charges
 * nobody has assembled into an invoice yet owes money just as surely as one
 * with an issued invoice nobody has paid — and it is the more common way a
 * patient leaves without paying, because there is no document whose absence
 * anybody notices. Listing invoices would show the counter only the debts
 * somebody had already done the paperwork for.
 *
 * Ordered by the oldest unsettled item, which is not the same as by arrival
 * time: the person who has been standing at the window longest belongs at the
 * top.
 */
export function AccountsBoard({ initial }: { initial: Page<AccountBoardEntry> }) {
  const t = useTranslations("billing");
  const [offset, setOffset] = useState(0);

  const board = useQuery({
    queryKey: ["billing", "accounts", offset],
    queryFn: ({ signal }) =>
      api.get<Page<AccountBoardEntry>>("/billing/accounts", {
        query: { limit: BOARD_PAGE_SIZE, offset },
        signal,
      }),
    // The server-rendered page is page one; later pages are fetched.
    initialData: offset === 0 ? initial : undefined,
    refetchInterval: 20_000,
    staleTime: 0,
  });

  const entries = board.data?.items ?? [];

  if (board.isPending) {
    return (
      <div className="space-y-2" aria-busy="true">
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-16 w-full rounded-lg" />
        ))}
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-lg border border-dashed py-16 text-center">
        <IndianRupee aria-hidden className="size-6" />
        <p className="text-foreground text-sm font-medium">{t("boardEmpty")}</p>
        <p className="text-sm">{t("boardEmptyHint")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-2xl text-sm">
        <caption className="sr-only">{t("boardTitle")}</caption>
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th scope="col" className="px-4 py-2.5 text-left font-medium">
              {t("patient")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-left font-medium">
              {t("visit")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">
              {t("balanceDue")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">
              {t("action")}
            </th>
          </tr>
        </thead>
        <tbody className="divide-border divide-y">
          {entries.map((entry) => (
            <AccountRow key={entry.encounter_id} entry={entry} />
          ))}
        </tbody>
      </table>
      </div>

      <Pager page={board.data} offset={offset} pageSize={BOARD_PAGE_SIZE} onOffsetChange={setOffset} />
    </div>
  );
}

/** Statuses that mean the visit is over and the bill was left behind. */
const SETTLEMENT_STATUSES: EncounterStatus[] = ["DECEASED", "REFERRED_OUT", "LAMA"];

function AccountRow({ entry }: { entry: AccountBoardEntry }) {
  const t = useTranslations("billing");
  const common = useTranslations("common");
  const waiting = useElapsed(entry.outstanding_since);

  const status = entry.encounter_status as EncounterStatus | null;
  // Same money, a completely different conversation: nobody is at the window
  // for these, and asking a bereaved family for payment at a counter screen
  // that says "collect from patient" is exactly the failure to avoid.
  const settlement = status !== null && SETTLEMENT_STATUSES.includes(status);

  return (
    <tr className={cn(settlement && "bg-muted/40")} data-testid="account-row">
      <td className="px-4 py-3">
        <div className="font-medium">{entry.patient_name ?? common("notRecorded")}</div>
        <div className="text-muted-foreground tabular text-xs">
          {entry.patient_uhid}
          {entry.patient_age_years !== null && entry.patient_age_years !== undefined
            ? ` · ${entry.patient_age_years}`
            : ""}
          {entry.patient_gender ? ` · ${entry.patient_gender[0]}` : ""}
        </div>
      </td>

      <td className="px-4 py-3">
        <div className="tabular text-xs">{entry.encounter_number}</div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          {status ? <EncounterStatusChip status={status} /> : null}
          {settlement ? (
            <span className="text-muted-foreground text-xs">{t("settlementNeeded")}</span>
          ) : null}
        </div>
        <div className="text-muted-foreground mt-1 text-xs">
          {t("waitingMinutes", { count: waiting })}
        </div>
      </td>

      <td className="px-4 py-3 text-right">
        <div className="tabular text-base font-semibold">{formatMoney(entry.balance_due)}</div>
        {entry.has_unpriced_items ? (
          <div className="text-caution-foreground text-xs font-medium">{t("unpriced")}</div>
        ) : null}
      </td>

      <td className="px-4 py-3 text-right">
        <LinkButton size="sm" className="h-tap" href={`/billing/${entry.encounter_id}`}>
          {t("openAccount")}
        </LinkButton>
      </td>
    </tr>
  );
}
