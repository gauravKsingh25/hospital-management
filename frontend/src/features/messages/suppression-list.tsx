"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";

import { Pager } from "@/components/pager";
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
import { BOARD_PAGE_SIZE } from "@/lib/pagination";
import { cn } from "@/lib/utils";
import type { Page, Suppression } from "@/types/api";

/**
 * Who is blocked from being messaged, and why.
 *
 * This screen exists so that the system's hardest rule is *visible*. CLAUDE.md
 * §14 names it an invariant — never send a notification to a deceased patient
 * — and the backend enforces it in three places, but until now nobody in the
 * hospital could see it working. An invariant no one can inspect is one people
 * quietly stop believing in.
 *
 * ## A death is not liftable, and the screen says so before you try
 *
 * `service.lift_suppression` refuses a `DECEASED` reason outright, whatever
 * permissions the caller holds — deliberately in the service rather than in
 * RBAC, because permissions are editable rows and this is the one rule that
 * must not be grantable back. The row here has no Release button at all and
 * carries the sentence explaining why, rather than offering an action that
 * always fails.
 *
 * The same goes for adding one: `DECEASED` is not in the reason picker,
 * because a death is recorded by the clinical death entry inside that
 * transaction. Two ways to record a death would mean two answers to whether
 * somebody is dead.
 */
const REASONS = ["OPTED_OUT", "INVALID_CONTACT", "STAFF_BLOCK"] as const;

const KEY = ["notifications", "suppressions"] as const;

export function SuppressionList({
  initial,
  canManage,
}: {
  initial: Page<Suppression>;
  canManage: boolean;
}) {
  const t = useTranslations("messages");
  const queryClient = useQueryClient();

  const [offset, setOffset] = useState(0);
  const [includeLifted, setIncludeLifted] = useState(false);
  const [lifting, setLifting] = useState<Suppression | null>(null);

  const list = useQuery({
    queryKey: [...KEY, includeLifted, offset],
    queryFn: ({ signal }) =>
      api.get<Page<Suppression>>("/notifications/suppressions", {
        query: { limit: BOARD_PAGE_SIZE, offset, include_lifted: includeLifted },
        signal,
      }),
    initialData: !includeLifted && offset === 0 ? initial : undefined,
    staleTime: 10_000,
  });

  const refresh = () => void queryClient.invalidateQueries({ queryKey: KEY });
  const rows = list.data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant={includeLifted ? "default" : "outline"}
          className="h-tap"
          aria-pressed={includeLifted}
          onClick={() => {
            setIncludeLifted((current) => !current);
            setOffset(0);
          }}
          data-testid="toggle-lifted"
        >
          {t("showLifted")}
        </Button>
      </div>

      {rows.length === 0 ? (
        <p className="text-muted-foreground rounded-lg border border-dashed py-12 text-center text-sm">
          {t("noSuppressions")}
        </p>
      ) : (
        <ul className="space-y-2">
          {rows.map((row) => (
            <SuppressionRow
              key={row.id}
              suppression={row}
              canManage={canManage}
              onLift={() => setLifting(row)}
            />
          ))}
        </ul>
      )}

      <Pager page={list.data} offset={offset} pageSize={BOARD_PAGE_SIZE} onOffsetChange={setOffset} />

      {lifting ? (
        <LiftDialog
          suppression={lifting}
          onOpenChange={(next) => {
            if (!next) setLifting(null);
          }}
          onDone={() => {
            setLifting(null);
            refresh();
          }}
        />
      ) : null}
    </div>
  );
}

function SuppressionRow({
  suppression,
  canManage,
  onLift,
}: {
  suppression: Suppression;
  canManage: boolean;
  onLift: () => void;
}) {
  const t = useTranslations("messages");
  const common = useTranslations("common");

  const isDeath = suppression.reason === "DECEASED";
  const lifted = suppression.lifted_at !== null && suppression.lifted_at !== undefined;

  return (
    <li
      className={cn(
        "flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border p-3",
        isDeath && "border-foreground/40 bg-foreground/5",
        lifted && "opacity-60",
      )}
      data-testid="suppression-row"
      data-reason={suppression.reason}
      data-lifted={lifted}
    >
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium">
          {suppression.patient_name ?? common("notRecorded")}
        </div>
        <div className="text-muted-foreground tabular truncate text-xs">
          {suppression.uhid ? `${suppression.uhid} · ` : ""}
          {t(`reason${suppression.reason}` as "reasonOPTED_OUT")}
          {suppression.channel ? ` · ${suppression.channel}` : ""}
          {suppression.category ? ` · ${suppression.category}` : ""}
        </div>
        {suppression.note ? (
          <p className="text-muted-foreground mt-0.5 text-xs">{suppression.note}</p>
        ) : null}
        {lifted ? (
          <p className="text-muted-foreground mt-0.5 text-xs">
            {t("liftedOn", {
              date: new Date(suppression.lifted_at as string).toLocaleDateString(),
              reason: suppression.lifted_reason ?? common("notRecorded"),
            })}
          </p>
        ) : null}
      </div>

      {isDeath ? (
        // No button. Not a disabled one either — a control that can never be
        // used is still a control somebody will ask why they cannot use.
        <p className="text-muted-foreground max-w-xs text-xs">{t("deathNotLiftable")}</p>
      ) : canManage && !lifted ? (
        <Button variant="outline" size="sm" className="h-tap" onClick={onLift}>
          {t("lift")}
        </Button>
      ) : null}
    </li>
  );
}

/** Record a block a patient asked for. `DECEASED` is deliberately not offered. */
export function AddSuppressionDialog({
  patientId,
  patientName,
  onOpenChange,
  onDone,
}: {
  patientId: string;
  patientName: string;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const t = useTranslations("messages");
  const common = useTranslations("common");

  const [reason, setReason] = useState<(typeof REASONS)[number]>("OPTED_OUT");
  const [note, setNote] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.post<Suppression>("/notifications/suppressions", {
        patient_id: patientId,
        reason,
        note: note.trim() || null,
      }),
    onSuccess: () => {
      toast.success(t("suppressionAdded"));
      onDone();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("addSuppression")}</DialogTitle>
          <DialogDescription>{patientName}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="space-y-1.5">
            <Label htmlFor="suppression-reason">{t("reason")}</Label>
            <div
              className="flex flex-wrap gap-1"
              role="group"
              aria-labelledby="suppression-reason"
              id="suppression-reason"
            >
              {REASONS.map((option) => (
                <Button
                  key={option}
                  size="sm"
                  variant={option === reason ? "default" : "outline"}
                  className="h-tap"
                  aria-pressed={option === reason}
                  onClick={() => setReason(option)}
                >
                  {t(`reason${option}` as "reasonOPTED_OUT")}
                </Button>
              ))}
            </div>
            {/* Says what is missing and why, rather than leaving somebody to
                hunt for it. */}
            <p className="text-muted-foreground text-xs">{t("deathNotTypedIn")}</p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="suppression-note">{t("note")}</Label>
            <Input
              id="suppression-note"
              value={note}
              onChange={(event) => setNote(event.target.value)}
              className="h-tap"
              placeholder={t("notePlaceholder")}
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button className="h-tap" disabled={create.isPending} onClick={() => create.mutate()}>
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function LiftDialog({
  suppression,
  onOpenChange,
  onDone,
}: {
  suppression: Suppression;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const t = useTranslations("messages");
  const common = useTranslations("common");
  const [reason, setReason] = useState("");

  const lift = useMutation({
    mutationFn: () =>
      api.post<Suppression>(`/notifications/suppressions/${suppression.id}/lift`, {
        reason: reason.trim(),
      }),
    onSuccess: () => {
      toast.success(t("lifted"));
      onDone();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("liftTitle")}</DialogTitle>
          <DialogDescription>{suppression.patient_name}</DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5 text-left">
          <Label htmlFor="lift-reason">{t("reason")}</Label>
          <Input
            id="lift-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            className="h-tap"
            autoFocus
          />
          {/* Required by the backend, and rightly: re-enabling messaging to
              somebody who opted out is a consent decision under the DPDP Act,
              and it has to say who decided and why. */}
          <p className="text-muted-foreground text-xs">{t("liftReasonHint")}</p>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={reason.trim().length === 0 || lift.isPending}
            onClick={() => lift.mutate()}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
