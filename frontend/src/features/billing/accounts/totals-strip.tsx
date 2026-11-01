"use client";

import { useTranslations } from "next-intl";

import { Skeleton } from "@/components/ui/skeleton";
import { formatMoney, isPositive } from "@/features/billing/money";
import { cn } from "@/lib/utils";
import type { AccountsSummary } from "@/types/api";

/**
 * The five numbers an accountant opens the screen for. Collected, billed and
 * reversals are for the chosen period; outstanding and running in wards are
 * as of now, because a debt from last week is still owed today.
 */
export function TotalsStrip({ summary }: { summary: AccountsSummary | undefined }) {
  const t = useTranslations("accounts");
  const billing = useTranslations("billing");

  if (!summary) {
    return (
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5" aria-busy="true">
        {[0, 1, 2, 3, 4].map((card) => (
          <Skeleton key={card} className="h-24 rounded-lg" />
        ))}
      </div>
    );
  }

  const methods = Object.entries(summary.collected_by_method)
    .map(([method, amount]) => `${billing(`method${method}` as "methodUPI")} ${formatMoney(amount)}`)
    .join(" · ");

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-5" data-testid="totals-strip">
      <Card label={t("collected")} amount={summary.collected} testId="total-collected">
        {methods || t("nothingCollected")}
      </Card>
      <Card label={t("billsGenerated")} amount={summary.billed} testId="total-billed">
        {t("billsCount", { count: summary.bills_count })}
      </Card>
      <Card
        label={t("outstanding")}
        amount={summary.outstanding}
        testId="total-outstanding"
        tone={isPositive(summary.outstanding) ? "caution" : undefined}
      >
        {t("patientsCount", { count: summary.outstanding_patients })}
      </Card>
      <Card label={t("runningInWards")} amount={summary.running_in_wards} testId="total-running">
        {t("admittedCount", { count: summary.running_patients })}
      </Card>
      <Card label={t("reversals")} amount={summary.reversals} testId="total-reversals">
        {isPositive(summary.reversals) ? t("reversalsHint") : t("noReversals")}
      </Card>
    </div>
  );
}

function Card({
  label,
  amount,
  testId,
  tone,
  children,
}: {
  label: string;
  amount: string;
  testId: string;
  tone?: "caution";
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1 rounded-lg border p-3">
      <p className="text-muted-foreground text-xs font-medium uppercase tracking-wide">{label}</p>
      <p
        className={cn("tabular text-xl font-semibold", tone === "caution" && "text-caution-foreground")}
        data-testid={testId}
      >
        {formatMoney(amount)}
      </p>
      <p className="text-muted-foreground text-xs">{children}</p>
    </div>
  );
}
