"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";
import { Minus, Plus } from "lucide-react";
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
import { Skeleton } from "@/components/ui/skeleton";
import { formatMoney } from "@/features/billing/money";
import { MedicinePicker } from "@/features/medicines/medicine-picker";
import {
  costsKey,
  issuesKey,
  useMedicineIssues,
  useStayCosts,
} from "@/features/medicines/use-medicines";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { ChargeCategory, CostLine, MedicineIssue, MedicineItem } from "@/types/api";

/**
 * Medicines and supplies used on this stay, outside the chart.
 *
 * The IV set, the cannula, the STAT injection given before a chart existed.
 * Each entry goes straight onto the patient's bill; a wrong one is reversed
 * with a reason — never deleted — so the list and the bill always agree.
 * Doses given from the chart are billed on their own and do not appear here.
 */
export function ItemsUsedPanel({
  admissionId,
  encounterId,
  canIssue,
  live,
}: {
  admissionId: string;
  encounterId: string;
  canIssue: boolean;
  live: boolean;
}) {
  const t = useTranslations("medicines");
  const format = useFormatter();
  const [adding, setAdding] = useState(false);
  const [reversing, setReversing] = useState<MedicineIssue | null>(null);

  const issues = useMedicineIssues(admissionId);
  const rows = issues.data?.items ?? [];

  return (
    <section className="space-y-3 rounded-lg border p-4" aria-labelledby="items-used-heading">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 id="items-used-heading" className="text-sm font-semibold">
            {t("itemsUsed")}
          </h2>
          <p className="text-muted-foreground text-xs">{t("itemsUsedHint")}</p>
        </div>
        {canIssue && live ? (
          <Button
            size="sm"
            className="h-tap"
            onClick={() => setAdding(true)}
            data-testid="record-item"
          >
            <Plus aria-hidden className="size-4" />
            {t("recordItem")}
          </Button>
        ) : null}
      </div>

      {issues.isPending ? (
        <Skeleton className="h-10 w-full" />
      ) : rows.length === 0 ? (
        <p className="text-muted-foreground text-sm">{t("nothingUsed")}</p>
      ) : (
        <ul className="divide-border divide-y" data-testid="items-used">
          {rows.map((issue) => {
            const reversed = issue.status === "REVERSED";
            return (
              <li
                key={issue.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2"
                data-testid="item-used"
              >
                <div className="min-w-[12rem] flex-1">
                  <div className={cn("font-medium", reversed && "text-muted-foreground line-through")}>
                    {issue.item_name}{" "}
                    <span className="tabular text-muted-foreground font-normal">
                      × {issue.quantity} {issue.unit}
                    </span>
                  </div>
                  <div className="text-muted-foreground text-xs">
                    {[
                      format.dateTime(new Date(issue.issued_at), {
                        day: "numeric",
                        month: "short",
                        hour: "2-digit",
                        minute: "2-digit",
                      }),
                      issue.issued_by_name,
                      issue.note,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                  {reversed ? (
                    <div className="text-muted-foreground text-xs">
                      {t("reversedBecause", { reason: issue.reversal_reason ?? "" })}
                    </div>
                  ) : null}
                </div>
                {canIssue && !reversed ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-tap"
                    onClick={() => setReversing(issue)}
                    aria-label={t("reverseAria", { name: issue.item_name })}
                  >
                    {t("reverse")}
                  </Button>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      {adding ? (
        <RecordItemDialog
          admissionId={admissionId}
          encounterId={encounterId}
          open
          onOpenChange={(next) => {
            if (!next) setAdding(false);
          }}
        />
      ) : null}
      {reversing ? (
        <ReverseDialog
          issue={reversing}
          admissionId={admissionId}
          encounterId={encounterId}
          open
          onOpenChange={(next) => {
            if (!next) setReversing(null);
          }}
        />
      ) : null}
    </section>
  );
}

function RecordItemDialog({
  admissionId,
  encounterId,
  open,
  onOpenChange,
}: {
  admissionId: string;
  encounterId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("medicines");
  const common = useTranslations("common");
  const queryClient = useQueryClient();
  const [item, setItem] = useState<MedicineItem | null>(null);
  const [quantity, setQuantity] = useState(1);
  const [note, setNote] = useState("");

  const record = useMutation({
    mutationFn: () =>
      api.post<MedicineIssue>(`/ipd/admissions/${admissionId}/medicine-issues`, {
        medicine_item_id: item?.id,
        quantity,
        note: note.trim() || null,
      }),
    onSuccess: (issue) => {
      toast.success(t("recorded", { name: issue.item_name, quantity: issue.quantity }));
      void queryClient.invalidateQueries({ queryKey: issuesKey(admissionId) });
      void queryClient.invalidateQueries({ queryKey: costsKey(encounterId) });
      onOpenChange(false);
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("recordItem")}</DialogTitle>
          <DialogDescription>{t("recordHint")}</DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (item) record.mutate();
          }}
          className="space-y-4 text-left"
          noValidate
        >
          <MedicinePicker label={t("item")} value={item} onChange={setItem} autoFocus />

          <div className="space-y-1.5">
            <Label htmlFor="item-quantity">
              {t("quantity")}
              {item ? ` (${item.unit})` : ""}
            </Label>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                className="h-tap w-tap"
                onClick={() => setQuantity((value) => Math.max(1, value - 1))}
                aria-label={t("less")}
              >
                <Minus aria-hidden className="size-4" />
              </Button>
              <Input
                id="item-quantity"
                value={quantity}
                onChange={(event) => {
                  const next = Number.parseInt(event.target.value, 10);
                  setQuantity(Number.isFinite(next) ? Math.min(999, Math.max(1, next)) : 1);
                }}
                inputMode="numeric"
                className="h-tap tabular w-20 text-center"
              />
              <Button
                type="button"
                variant="outline"
                className="h-tap w-tap"
                onClick={() => setQuantity((value) => Math.min(999, value + 1))}
                aria-label={t("more")}
              >
                <Plus aria-hidden className="size-4" />
              </Button>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="item-note">
              {t("note")} <span className="text-muted-foreground">({common("optional")})</span>
            </Label>
            <Input
              id="item-note"
              value={note}
              maxLength={255}
              onChange={(event) => setNote(event.target.value)}
              className="h-tap"
              placeholder={t("notePlaceholder")}
            />
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              className="h-tap"
              onClick={() => onOpenChange(false)}
            >
              {common("cancel")}
            </Button>
            <Button type="submit" className="h-tap" disabled={!item || record.isPending}>
              {t("addToBill")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ReverseDialog({
  issue,
  admissionId,
  encounterId,
  open,
  onOpenChange,
}: {
  issue: MedicineIssue;
  admissionId: string;
  encounterId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("medicines");
  const common = useTranslations("common");
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");

  const reverse = useMutation({
    mutationFn: () =>
      api.post<MedicineIssue>(`/ipd/medicine-issues/${issue.id}/reverse`, {
        reason: reason.trim(),
      }),
    onSuccess: () => {
      toast.success(t("reversed", { name: issue.item_name }));
      void queryClient.invalidateQueries({ queryKey: issuesKey(admissionId) });
      void queryClient.invalidateQueries({ queryKey: costsKey(encounterId) });
      onOpenChange(false);
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("reverseTitle")}</DialogTitle>
          <DialogDescription>
            {issue.item_name} × {issue.quantity} {issue.unit}
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (reason.trim()) reverse.mutate();
          }}
          className="space-y-3 text-left"
          noValidate
        >
          <div className="space-y-1.5">
            <Label htmlFor="reverse-reason">{t("reverseReason")}</Label>
            <Input
              id="reverse-reason"
              value={reason}
              maxLength={255}
              onChange={(event) => setReason(event.target.value)}
              className="h-tap"
              placeholder={t("reversePlaceholder")}
              autoFocus
            />
            <p className="text-muted-foreground text-xs">{t("reverseHint")}</p>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              className="h-tap"
              onClick={() => onOpenChange(false)}
            >
              {common("cancel")}
            </Button>
            <Button
              type="submit"
              variant="destructive"
              className="h-tap"
              disabled={!reason.trim() || reverse.isPending}
            >
              {t("reverse")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// The order a relative asks about a stay; the server sorts the same way.
const CATEGORY_ORDER: ChargeCategory[] = [
  "ROOM",
  "PHARMACY",
  "LAB",
  "RADIOLOGY",
  "PROCEDURE",
  "CONSULTATION",
  "REGISTRATION",
  "OTHER",
];

/**
 * What the stay has cost so far: bed-days, every dose given from the list,
 * every item used, tests — grouped by item, with the total.
 *
 * Read-only and from billing, so it is the same number the cash counter will
 * see. Totals come from the server; nothing here adds up money.
 */
export function StayCostPanel({ encounterId }: { encounterId: string }) {
  const t = useTranslations("medicines");
  const costs = useStayCosts(encounterId);
  const data = costs.data;

  const groups = CATEGORY_ORDER.map((category) => ({
    category,
    lines: (data?.lines ?? []).filter((line) => line.category === category),
  })).filter((group) => group.lines.length > 0);

  return (
    <section className="space-y-3 rounded-lg border p-4" aria-labelledby="stay-cost-heading">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="stay-cost-heading" className="text-sm font-semibold">
          {t("costSoFar")}
        </h2>
        {data ? (
          <span className="tabular text-xl font-semibold" data-testid="stay-total">
            {formatMoney(data.total)}
          </span>
        ) : null}
      </div>

      {costs.isPending ? (
        <Skeleton className="h-16 w-full" />
      ) : costs.isError ? (
        <p className="text-muted-foreground text-sm">{t("costUnavailable")}</p>
      ) : groups.length === 0 ? (
        <p className="text-muted-foreground text-sm">{t("noCostsYet")}</p>
      ) : (
        <div className="space-y-3" data-testid="stay-costs">
          {data?.has_unpriced_items ? (
            <p className="border-caution/50 bg-caution/10 rounded-md border px-3 py-2 text-sm">
              {t("unpricedWarning")}
            </p>
          ) : null}
          {groups.map((group) => (
            <div key={group.category}>
              <h3 className="text-muted-foreground mb-1 text-xs font-medium uppercase">
                {t("category", { category: group.category })}
              </h3>
              <ul className="space-y-1">
                {group.lines.map((line) => (
                  <CostRow key={`${line.description}-${line.unit_price}`} line={line} />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function CostRow({ line }: { line: CostLine }) {
  const t = useTranslations("medicines");
  // Quantities arrive as decimal strings ("2.00"); a whole number reads as one.
  const quantity = Number(line.quantity);
  const shownQuantity = Number.isInteger(quantity) ? String(quantity) : line.quantity;

  return (
    <li className="flex items-baseline gap-3 text-sm" data-testid="cost-line">
      <span className="min-w-0 flex-1">
        {line.description}
        <span className="text-muted-foreground tabular text-xs">
          {" "}
          × {shownQuantity}
          {line.needs_pricing ? "" : ` @ ${formatMoney(line.unit_price)}`}
        </span>
        {line.needs_pricing ? (
          <span className="text-caution-foreground ml-2 text-xs font-medium">
            {t("notPriced")}
          </span>
        ) : null}
      </span>
      <span className="tabular shrink-0">{formatMoney(line.total_amount)}</span>
    </li>
  );
}
