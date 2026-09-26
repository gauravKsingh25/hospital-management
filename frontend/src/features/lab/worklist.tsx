"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { FlaskConical } from "lucide-react";
import { toast } from "sonner";

import { Pager } from "@/components/pager";
import { WorklistStageChip } from "@/components/status-chip";
import { Button } from "@/components/ui/button";
import { LinkButton } from "@/components/ui/link-button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { CatalogueItem, Page, WorklistEntry } from "@/types/api";

import { AccessionDialog } from "@/features/lab/accession-dialog";

/**
 * The bench worklist.
 *
 * One list, not a set of tabs — lab and radiology, accessioned and not,
 * interleaved in the order the work should be done. Splitting them would put
 * a routine X-ray above a STAT troponin, because priority only means anything
 * across the whole set.
 *
 * Every row carries exactly one action, chosen by its `stage`, and that
 * choice is made on the server (`WorklistStage`). The technician does not read
 * three status enums and work out what is next; the row says what is next.
 * Three of the five stages are a single click — CLAUDE.md §7b's quick actions
 * — and only result entry opens a screen.
 */
export function LabWorklist({
  initial,
  canAccession,
  canCollect,
  canReceive,
}: {
  initial: Page<WorklistEntry>;
  canAccession: boolean;
  canCollect: boolean;
  canReceive: boolean;
}) {
  const t = useTranslations("lab");
  const queryClient = useQueryClient();
  const [offset, setOffset] = useState(0);

  const worklist = useQuery({
    queryKey: ["lab", "worklist", offset],
    queryFn: ({ signal }) =>
      api.get<Page<WorklistEntry>>("/diagnostics/worklist", {
        query: { limit: BOARD_PAGE_SIZE, offset },
        signal,
      }),
    initialData: offset === 0 ? initial : undefined,
    // Same reasoning as the OPD queue: several people work this list at once,
    // and a stale row means two technicians drawing the same tube.
    refetchInterval: 20_000,
    staleTime: 0,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["lab", "worklist"] });
  const entries = worklist.data?.items ?? [];

  if (worklist.isPending) {
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
        <FlaskConical aria-hidden className="size-6" />
        <p className="text-foreground text-sm font-medium">{t("empty")}</p>
        <p className="text-sm">{t("emptyHint")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-3xl text-sm">
        <caption className="sr-only">{t("title")}</caption>
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th scope="col" className="px-4 py-2.5 text-left font-medium">
              {t("patient")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-left font-medium">
              {t("investigation")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-left font-medium">
              {t("stage")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">
              {t("action")}
            </th>
          </tr>
        </thead>
        <tbody className="divide-border divide-y">
          {entries.map((entry) => (
            <WorklistRow
              key={entry.order_id}
              entry={entry}
              canAccession={canAccession}
              canCollect={canCollect}
              canReceive={canReceive}
              onDone={refresh}
            />
          ))}
        </tbody>
      </table>
      </div>

      <Pager page={worklist.data} offset={offset} pageSize={BOARD_PAGE_SIZE} onOffsetChange={setOffset} />
    </div>
  );
}

function WorklistRow({
  entry,
  canAccession,
  canCollect,
  canReceive,
  onDone,
}: {
  entry: WorklistEntry;
  canAccession: boolean;
  canCollect: boolean;
  canReceive: boolean;
  onDone: () => void;
}) {
  const t = useTranslations("lab");
  const common = useTranslations("common");
  const [accessioning, setAccessioning] = useState(false);

  // An empty object, not an absent body. `POST /specimens/{id}/collect` takes a
  // `CollectRequest` whose every field is optional — but the *body* is still
  // required, so sending nothing is a 422 rather than a default-filled request.
  const advance = useMutation({
    mutationFn: (path: string) => api.post<unknown>(path, {}),
    onSuccess: onDone,
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const urgent = entry.priority !== "ROUTINE";

  return (
    <tr className={cn(urgent && "bg-critical/5")} data-testid="worklist-row">
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
        <div className="font-medium">{entry.item_name}</div>
        <div className="text-muted-foreground text-xs">
          {t(`discipline${entry.order_type}` as "disciplineLAB")}
          {entry.accession_number ? ` · ${entry.accession_number}` : ""}
          {urgent ? (
            <span className="text-critical ml-1.5 font-medium">
              {entry.priority === "STAT" ? "STAT" : t("urgent")}
            </span>
          ) : null}
        </div>
        {entry.instructions ? (
          <div className="text-muted-foreground mt-0.5 text-xs italic">{entry.instructions}</div>
        ) : null}
      </td>

      <td className="px-4 py-3">
        <WorklistStageChip stage={entry.stage} />
        {entry.has_critical_result ? (
          <div className="text-critical mt-1 text-xs font-semibold">{t("criticalValue")}</div>
        ) : null}
      </td>

      <td className="px-4 py-3 text-right">
        {entry.stage === "AWAITING_ACCESSION" && canAccession ? (
          <>
            <Button size="sm" className="h-tap" onClick={() => setAccessioning(true)}>
              {t("accession")}
            </Button>
            <AccessionDialog
              open={accessioning}
              onOpenChange={setAccessioning}
              entry={entry}
              onAccessioned={() => {
                setAccessioning(false);
                onDone();
              }}
            />
          </>
        ) : null}

        {entry.stage === "AWAITING_COLLECTION" && canCollect && entry.specimen_id ? (
          <Button
            size="sm"
            className="h-tap"
            disabled={advance.isPending}
            onClick={() =>
              advance.mutate(`/diagnostics/specimens/${entry.specimen_id}/collect`)
            }
          >
            {t("markCollected")}
          </Button>
        ) : null}

        {entry.stage === "AWAITING_RECEIPT" && canReceive && entry.specimen_id ? (
          <Button
            size="sm"
            className="h-tap"
            disabled={advance.isPending}
            onClick={() => advance.mutate(`/diagnostics/specimens/${entry.specimen_id}/receive`)}
          >
            {t("markReceived")}
          </Button>
        ) : null}

        {(entry.stage === "AWAITING_RESULTS" || entry.stage === "AWAITING_VERIFICATION") &&
        entry.report_id ? (
          <LinkButton size="sm" className="h-tap" href={`/lab/reports/${entry.report_id}`}>
            {entry.stage === "AWAITING_RESULTS" ? t("enterResults") : t("openReport")}
          </LinkButton>
        ) : null}
      </td>
    </tr>
  );
}

/** Fetches the catalogue once, for the accession dialog's picker. */
export function useCatalogue(discipline: "LAB" | "RADIOLOGY") {
  return useQuery({
    queryKey: ["diagnostics", "catalogue", discipline],
    queryFn: ({ signal }) =>
      api.get<Page<CatalogueItem>>("/diagnostics/catalogue", {
        query: { discipline, limit: 200 },
        signal,
      }),
    // The test catalogue changes when an administrator edits it, which is
    // rarely and never mid-shift. Five minutes keeps the dialog instant.
    staleTime: 5 * 60_000,
  });
}
