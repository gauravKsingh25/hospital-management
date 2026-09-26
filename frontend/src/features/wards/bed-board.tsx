"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Bed as BedIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Bed, BoardBed, BoardWard, Occupancy } from "@/types/api";

const BOARD_KEY = ["ipd", "board"] as const;

/**
 * The nursing station's bed board.
 *
 * The one screen in the system that is deliberately **not paginated**, and the
 * backend says so at the endpoint: a bed board you have to page through is a
 * bed board nobody can read at a glance, which is its only purpose. The
 * response is bounded by the hospital's physical bed count.
 *
 * The interesting state is `CLEANING`, and it is the rung most systems leave
 * out. Without it a bed looks free the instant a patient is discharged,
 * admissions sends somebody to it, and they arrive to find it unmade — so the
 * board learns to lie and staff go back to a whiteboard. Modelling
 * housekeeping as a state the bed passes through is what makes "available"
 * mean available, and it is why this screen gives housekeeping a one-click
 * action of their own.
 */
export function BedBoard({
  initialBoard,
  initialOccupancy,
  canClean,
  canBlock,
}: {
  initialBoard: BoardWard[];
  initialOccupancy: Occupancy | null;
  canClean: boolean;
  canBlock: boolean;
}) {
  const t = useTranslations("wards");
  const queryClient = useQueryClient();
  const [blocking, setBlocking] = useState<BoardBed | null>(null);

  const board = useQuery({
    queryKey: BOARD_KEY,
    queryFn: ({ signal }) => api.get<BoardWard[]>("/ipd/wards/board", { signal }),
    initialData: initialBoard,
    // Several people watch this at once — the charge nurse, admissions looking
    // for a bed, housekeeping working through the turnarounds. Fifteen seconds
    // is the same cadence as the OPD queue and for the same reason.
    refetchInterval: 15_000,
    staleTime: 0,
  });

  const occupancy = useQuery({
    queryKey: ["ipd", "occupancy"],
    queryFn: ({ signal }) => api.get<Occupancy>("/ipd/wards/occupancy", { signal }),
    initialData: initialOccupancy ?? undefined,
    refetchInterval: 60_000,
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: BOARD_KEY });
    void queryClient.invalidateQueries({ queryKey: ["ipd", "occupancy"] });
  };

  const wards = board.data ?? [];

  if (wards.length === 0) {
    return (
      <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-lg border border-dashed py-16 text-center">
        <BedIcon aria-hidden className="size-6" />
        <p className="text-foreground text-sm font-medium">{t("noWards")}</p>
        <p className="text-sm">{t("noWardsHint")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {occupancy.data ? <OccupancyStrip stats={occupancy.data} /> : null}

      {wards.map((ward) => (
        <section key={ward.id} aria-labelledby={`ward-${ward.id}`} className="space-y-3">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h2 id={`ward-${ward.id}`} className="font-semibold">
              {ward.name}
              <span className="text-muted-foreground ml-2 text-sm font-normal">
                {ward.code} · {ward.bed_class}
                {ward.gender_policy ? ` · ${ward.gender_policy}` : ""}
              </span>
            </h2>
            <p className="text-muted-foreground tabular text-sm">
              {t("wardCounts", {
                occupied: ward.occupied,
                available: ward.available,
                cleaning: ward.cleaning,
              })}
            </p>
          </div>

          <ul className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">
            {(ward.beds ?? []).map((bed) => (
              <BedTile
                key={bed.id}
                bed={bed}
                canClean={canClean}
                canBlock={canBlock}
                onDone={refresh}
                onBlock={() => setBlocking(bed)}
              />
            ))}
          </ul>
        </section>
      ))}

      {blocking ? (
        <BlockBedDialog
          bed={blocking}
          open
          onOpenChange={(next) => {
            if (!next) setBlocking(null);
          }}
          onDone={() => {
            setBlocking(null);
            refresh();
          }}
        />
      ) : null}
    </div>
  );
}

/** The number management asks for at nine in the morning. */
function OccupancyStrip({ stats }: { stats: Occupancy }) {
  const t = useTranslations("wards");

  const cells: { key: string; value: number | string; tone?: string }[] = [
    { key: "occupancyRate", value: `${Math.round(stats.occupancy_rate * 100)}%` },
    { key: "occupied", value: stats.occupied },
    { key: "available", value: stats.available, tone: "text-success" },
    { key: "cleaning", value: stats.cleaning, tone: "text-caution-foreground" },
    { key: "reserved", value: stats.reserved },
    { key: "outOfService", value: stats.out_of_service, tone: "text-muted-foreground" },
  ];

  return (
    <dl className="grid grid-cols-3 gap-3 rounded-lg border p-4 sm:grid-cols-6">
      {cells.map((cell) => (
        <div key={cell.key}>
          <dt className="text-muted-foreground text-xs">{t(cell.key as "occupied")}</dt>
          <dd className={cn("tabular text-xl font-semibold", cell.tone)}>{cell.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * One bed.
 *
 * Colour carries the status and so does the label — the same rule as the
 * status chips. A charge nurse reads this from across the room, and roughly
 * one in twelve male staff would otherwise be reading a grid of identical grey
 * squares.
 */
function BedTile({
  bed,
  canClean,
  canBlock,
  onDone,
  onBlock,
}: {
  bed: BoardBed;
  canClean: boolean;
  canBlock: boolean;
  onDone: () => void;
  onBlock: () => void;
}) {
  const t = useTranslations("wards");

  const act = useMutation({
    mutationFn: (path: string) => api.post<Bed>(path, {}),
    onSuccess: onDone,
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const tone =
    bed.status === "AVAILABLE"
      ? "border-success/40 bg-success/10"
      : bed.status === "OCCUPIED"
        ? "border-info/40 bg-info/10"
        : bed.status === "CLEANING"
          ? "border-caution/50 bg-caution/15"
          : bed.status === "RESERVED"
            ? "border-primary/40 bg-primary/10"
            : "bg-muted opacity-70";

  return (
    <li
      className={cn("rounded-lg border p-2.5", tone)}
      data-testid="bed-tile"
      data-status={bed.status}
      data-code={bed.code}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="tabular text-sm font-semibold">{bed.code}</span>
        <span className="text-[10px] font-medium uppercase">
          {t(`status${bed.status}` as "statusAVAILABLE")}
        </span>
      </div>

      {bed.patient_name ? (
        // The whole reason `BoardBed` carries identity: a board of bed numbers
        // answers "is there room", and a ward round needs "who is in it".
        <Link
          href={bed.admission_id ? `/admissions/${bed.admission_id}` : "#"}
          className="mt-1.5 block hover:underline"
        >
          <span className="block truncate text-sm font-medium">{bed.patient_name}</span>
          <span className="text-muted-foreground tabular block truncate text-[11px]">
            {bed.uhid}
          </span>
        </Link>
      ) : (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {bed.status === "CLEANING" && canClean ? (
            <Button
              size="sm"
              variant="outline"
              className="h-tap w-full"
              disabled={act.isPending}
              onClick={() => act.mutate(`/ipd/beds/${bed.id}/cleaned`)}
            >
              {t("markCleaned")}
            </Button>
          ) : null}

          {bed.status === "AVAILABLE" && canBlock ? (
            <Button
              size="sm"
              variant="ghost"
              className="h-tap text-muted-foreground w-full"
              onClick={onBlock}
            >
              {t("takeOutOfService")}
            </Button>
          ) : null}

          {bed.status === "OUT_OF_SERVICE" && canBlock ? (
            <Button
              size="sm"
              variant="outline"
              className="h-tap w-full"
              disabled={act.isPending}
              onClick={() => act.mutate(`/ipd/beds/${bed.id}/restore`)}
            >
              {t("restore")}
            </Button>
          ) : null}
        </div>
      )}
    </li>
  );
}

function BlockBedDialog({
  bed,
  open,
  onOpenChange,
  onDone,
}: {
  bed: BoardBed;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const t = useTranslations("wards");
  const common = useTranslations("common");
  const [reason, setReason] = useState("");

  const block = useMutation({
    mutationFn: () =>
      api.post<Bed>(`/ipd/beds/${bed.id}/out-of-service`, { reason: reason.trim() }),
    onSuccess: () => {
      toast.success(t("bedBlocked"));
      setReason("");
      onDone();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("takeOutOfService")}</DialogTitle>
          <DialogDescription>{bed.code}</DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5 text-left">
          <Label htmlFor="block-reason">{t("reason")}</Label>
          <Input
            id="block-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            className="h-tap"
            placeholder={t("reasonPlaceholder")}
            autoFocus
          />
          {/* Required by the backend, and rightly: a bed that disappears from
              the board with no reason is a bed nobody dares bring back. */}
          <p className="text-muted-foreground text-xs">{t("reasonHint")}</p>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={reason.trim().length < 3 || block.isPending}
            onClick={() => block.mutate()}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
