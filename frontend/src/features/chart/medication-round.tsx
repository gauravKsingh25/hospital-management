"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Pill } from "lucide-react";

import { Pager } from "@/components/pager";
import { Button } from "@/components/ui/button";
import { DoseRow } from "@/features/chart/dose-row";
import { api } from "@/lib/api/client";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import type { Dose, Page } from "@/types/api";

/**
 * The ward's medication round.
 *
 * Every dose due across every bed, in time order, with the patient and the bed
 * on each row. This is the screen a nurse walks the ward with, and it is held
 * to §7b's fifteen seconds per action — which is why "Given" is one tap and
 * nothing about the common case opens a dialog.
 *
 * Defaults to what is **outstanding** rather than to everything. A round of
 * already-signed doses is a list a nurse has to read past to find their work,
 * and the whole point is that the next thing to do is at the top.
 */
export function MedicationRound({
  initial,
  canAdminister,
}: {
  initial: Page<Dose>;
  canAdminister: boolean;
}) {
  const t = useTranslations("chart");
  const queryClient = useQueryClient();

  const [offset, setOffset] = useState(0);
  const [showSigned, setShowSigned] = useState(false);

  const round = useQuery({
    queryKey: ["ipd", "round", showSigned, offset],
    queryFn: ({ signal }) =>
      api.get<Page<Dose>>("/ipd/doses", {
        query: {
          limit: BOARD_PAGE_SIZE,
          offset,
          status: showSigned ? undefined : "DUE",
        },
        signal,
      }),
    initialData: !showSigned && offset === 0 ? initial : undefined,
    // Several nurses work one ward. A dose signed by a colleague thirty
    // seconds ago must not still look outstanding — that is how a drug gets
    // given twice.
    refetchInterval: 15_000,
    staleTime: 0,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["ipd", "round"] });
  const doses = round.data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant={showSigned ? "outline" : "default"}
          className="h-tap"
          onClick={() => {
            setShowSigned(false);
            setOffset(0);
          }}
        >
          {t("outstanding")}
        </Button>
        <Button
          variant={showSigned ? "default" : "outline"}
          className="h-tap"
          onClick={() => {
            setShowSigned(true);
            setOffset(0);
          }}
        >
          {t("everything")}
        </Button>
      </div>

      {doses.length === 0 ? (
        <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-lg border border-dashed py-16 text-center">
          <Pill aria-hidden className="size-6" />
          <p className="text-foreground text-sm font-medium">
            {showSigned ? t("noDoses") : t("roundClear")}
          </p>
          <p className="text-sm">{showSigned ? t("noDosesHint") : t("roundClearHint")}</p>
        </div>
      ) : (
        <ul className="space-y-2">
          {doses.map((dose) => (
            <DoseRow
              key={dose.id}
              dose={dose}
              showPatient
              canAdminister={canAdminister}
              onSigned={refresh}
            />
          ))}
        </ul>
      )}

      <Pager
        page={round.data}
        offset={offset}
        pageSize={BOARD_PAGE_SIZE}
        onOffsetChange={setOffset}
      />
    </div>
  );
}
