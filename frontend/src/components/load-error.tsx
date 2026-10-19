"use client";

import { useTranslations } from "next-intl";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Shown in place of a list whose query failed.
 *
 * Exists because the easy alternative — `query.data?.items ?? []` straight
 * into an empty state — renders a failed request as "nothing here". On a
 * medication round that reads as "no doses due", which is the one thing a
 * nurse must never be told by mistake. A list screen should therefore always
 * render three distinct states: loading (skeleton), failed (this), and
 * genuinely empty.
 *
 * Usage:
 *   if (query.isPending) return <Skeleton … />;
 *   if (query.isError && !query.data) return <LoadError onRetry={() => query.refetch()} />;
 *
 * The `!query.data` guard matters: when a *background* refetch fails, TanStack
 * keeps the last good data, and showing that is better than hiding it.
 */
export function LoadError({ onRetry }: { onRetry: () => void }) {
  const t = useTranslations("common");

  return (
    <div
      role="alert"
      className="text-muted-foreground flex flex-col items-center gap-3 rounded-lg border border-dashed py-14 text-center"
    >
      <AlertTriangle aria-hidden className="text-critical size-6" />
      <p className="text-foreground text-sm font-medium">{t("somethingWentWrong")}</p>
      <Button variant="outline" className="h-tap" onClick={onRetry}>
        {t("retry")}
      </Button>
    </div>
  );
}
