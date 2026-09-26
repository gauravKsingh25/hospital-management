"use client";

import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import type { Page } from "@/types/api";

/**
 * Previous / next for a paginated board.
 *
 * Extracted because three screens got this wrong in the same way: the backend
 * paginates every list (CLAUDE.md §11) and the boards rendered page one and
 * offered no way to reach page two. That looks harmless on a demo tenant and
 * is not — the cash counter is ordered by oldest debt first, so by mid-morning
 * the patient standing at the window is on page three and the cashier cannot
 * reach them at all.
 *
 * `has_more` comes from the server rather than being inferred from
 * `offset + limit < total`: the two disagree the moment a row is soft-deleted
 * between pages.
 */
export function Pager<T>({
  page,
  offset,
  pageSize,
  onOffsetChange,
}: {
  page: Page<T> | undefined;
  offset: number;
  pageSize: number;
  onOffsetChange: (offset: number) => void;
}) {
  const t = useTranslations("common");
  const rows = page?.items.length ?? 0;

  // Nothing to page through: one short page needs no controls, and a pair of
  // permanently disabled buttons is clutter that teaches people to ignore them.
  if (offset === 0 && !page?.has_more) return null;

  return (
    <div className="flex items-center justify-between gap-3">
      <p className="text-muted-foreground text-sm">
        {t("showingRange", { from: offset + 1, to: offset + rows, total: page?.total ?? 0 })}
      </p>
      <div className="flex gap-2">
        <Button
          variant="outline"
          className="h-tap"
          disabled={offset === 0}
          onClick={() => onOffsetChange(Math.max(0, offset - pageSize))}
        >
          {t("back")}
        </Button>
        <Button
          variant="outline"
          className="h-tap"
          disabled={!page?.has_more}
          onClick={() => onOffsetChange(offset + pageSize)}
        >
          {t("next")}
        </Button>
      </div>
    </div>
  );
}
