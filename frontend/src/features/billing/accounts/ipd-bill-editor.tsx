"use client";

import { useQuery } from "@tanstack/react-query";
import { Pencil, Percent, Plus, Printer, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useReducer, useState, type FormEvent } from "react";
import { toast } from "sonner";

import { InvoiceStatusChip } from "@/components/status-chip";
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
import { LinkButton } from "@/components/ui/link-button";
import { Skeleton } from "@/components/ui/skeleton";
import { billHref } from "@/features/billing/accounts/accounts-tables";
import {
  useCancelFinalBill,
  useFinaliseIpdBill,
  useIpdBill,
  useSaveIpdBill,
  type BillOp,
} from "@/features/billing/accounts/use-ipd-bill";
import { formatMoney } from "@/features/billing/money";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Charge, ChargeCategory, IpdBill, Page, ServiceItem } from "@/types/api";

/**
 * The accountant's IPD bill editor (spec §5.5).
 *
 * Every change — add, edit, discount, remove — is staged here and visibly
 * unsaved. The total on screen is the last *saved* total, because a total
 * that moves while somebody is still deciding is a total nobody can trust.
 * Save sends everything at once with the version this editor loaded; the
 * server applies all or none, records each change with its reason, and
 * returns the recalculated bill. The version sent is the one the first
 * change was staged against, so a background reload cannot make a stale save
 * look fresh. If somebody else changed the stay first, the save is refused,
 * the editor reloads, and the staged changes are kept for the user to check
 * and save again (those whose line has gone are dropped).
 *
 * Read-only once the final bill exists, and for anyone without
 * `ipd_bill:edit`.
 */
export const CATEGORIES: ChargeCategory[] = [
  "PHARMACY",
  "OTHER",
  "PROCEDURE",
  "LAB",
  "RADIOLOGY",
  "ROOM",
  "CONSULTATION",
  "REGISTRATION",
];

type Staged = { key: number; op: BillOp; label: string };
type Action =
  | { type: "stage"; op: BillOp; label: string }
  | { type: "unstage"; key: number }
  | { type: "keep"; items: Staged[] }
  | { type: "clear" };

function staging(state: Staged[], action: Action): Staged[] {
  if (action.type === "clear") return [];
  if (action.type === "keep") return action.items;
  if (action.type === "unstage") return state.filter((item) => item.key !== action.key);
  return [...state, { key: Date.now() + state.length, op: action.op, label: action.label }];
}

type DialogState =
  | { mode: "add" }
  | { mode: "edit" | "discount" | "remove"; charge: Charge }
  | null;

export function IpdBillEditor({
  encounterId,
  canEdit,
  canFinalise,
}: {
  encounterId: string;
  canEdit: boolean;
  canFinalise: boolean;
}) {
  const t = useTranslations("ipdBill");
  const accounts = useTranslations("accounts");
  const bill = useIpdBill(encounterId);
  const save = useSaveIpdBill(encounterId);
  const finalise = useFinaliseIpdBill(encounterId);
  const cancelFinal = useCancelFinalBill(encounterId);
  const [staged, dispatch] = useReducer(staging, []);
  // The bill version the staged changes were made against.
  const [base, setBase] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [confirming, setConfirming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelReason, setCancelReason] = useState("");

  if (bill.isPending) return <Skeleton className="h-64 w-full rounded-lg" />;
  if (!bill.data) {
    return <p className="text-destructive text-sm">{t("loadFailed")}</p>;
  }
  const data = bill.data;
  const editable = canEdit && !data.final_invoice;

  const clearStaged = () => {
    dispatch({ type: "clear" });
    setBase(null);
  };

  const submit = () =>
    save.mutate(
      { expected_version: base ?? data.version, ops: staged.map((item) => item.op) },
      {
        onSuccess: () => {
          clearStaged();
          toast.success(t("saved"));
        },
        onError: async (failure) => {
          if (isApiError(failure) && failure.code === "bill_changed") {
            const fresh = (await bill.refetch()).data;
            if (!fresh) return clearStaged();
            const live = new Set(
              fresh.charges.filter((charge) => charge.status !== "WAIVED").map((charge) => charge.id),
            );
            const kept = staged.filter((item) => !("charge_id" in item.op) || live.has(item.op.charge_id));
            dispatch({ type: "keep", items: kept });
            setBase(kept.length ? fresh.version : null);
            toast.warning(kept.length < staged.length ? t("changedElsewhereDropped") : t("changedElsewhere"));
          } else {
            toast.error(isApiError(failure) ? failure.message : String(failure));
          }
        },
      },
    );

  const voidFinal = () =>
    cancelFinal.mutate(cancelReason.trim(), {
      onSuccess: (invoice) => {
        setCancelling(false);
        setCancelReason("");
        toast.success(t("finalCancelled", { number: invoice.invoice_number }));
      },
      onError: (failure) => toast.error(isApiError(failure) ? failure.message : String(failure)),
    });

  const generate = () =>
    finalise.mutate(undefined, {
      onSuccess: (invoice) => {
        setConfirming(false);
        toast.success(t("finalGenerated", { number: invoice.invoice_number }));
      },
      onError: (failure) => toast.error(isApiError(failure) ? failure.message : String(failure)),
    });

  const stagedFor = (chargeId: string) =>
    staged.filter((item) => "charge_id" in item.op && item.op.charge_id === chargeId);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-muted-foreground text-xs font-medium uppercase tracking-wide">
            {t("savedTotal")}
          </p>
          <p className="tabular text-2xl font-semibold" data-testid="ipd-total">
            {formatMoney(data.total)}
          </p>
          {staged.length ? (
            <p className="text-caution-foreground text-xs">{t("totalOnSave")}</p>
          ) : null}
        </div>
        <div className="flex flex-wrap gap-2">
          {editable ? (
            <Button variant="outline" className="h-tap" onClick={() => setDialog({ mode: "add" })}>
              <Plus aria-hidden />
              {t("addCharge")}
            </Button>
          ) : null}
          <LinkButton
            href={`/bills/running/${encounterId}?print=1`}
            variant="outline"
            className="h-tap"
          >
            <Printer aria-hidden />
            {t("printRunning")}
          </LinkButton>
          {canFinalise && data.can_finalise && !staged.length ? (
            <Button className="h-tap" onClick={() => setConfirming(true)}>
              {t("generateFinal")}
            </Button>
          ) : null}
        </div>
      </div>

      {data.final_invoice ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3">
          <div className="space-y-1">
            <p className="font-medium">
              {t("finalBill")} · <span className="tabular">{data.final_invoice.invoice_number}</span>
            </p>
            <div className="flex items-center gap-2 text-sm">
              <InvoiceStatusChip status={data.final_invoice.status} />
              <span className="tabular">{formatMoney(data.final_invoice.grand_total)}</span>
              <span className="text-muted-foreground">
                {t("balance")} {formatMoney(data.final_invoice.balance_due)}
              </span>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            {canFinalise && data.final_invoice.status === "ISSUED" ? (
              <Button variant="outline" className="h-tap" onClick={() => setCancelling(true)}>
                {t("cancelFinal")}
              </Button>
            ) : null}
            <LinkButton href={billHref(data.final_invoice.id)} variant="outline" className="h-tap">
              {accounts("open")}
            </LinkButton>
            <LinkButton
              href={billHref(data.final_invoice.id, true)}
              className="h-tap"
              aria-label={accounts("printBill", { number: data.final_invoice.invoice_number })}
            >
              <Printer aria-hidden />
              {accounts("print")}
            </LinkButton>
          </div>
        </div>
      ) : data.stay.status === "DISCHARGED" || data.can_finalise ? (
        <p className="text-muted-foreground text-sm">{t("awaitingFinal")}</p>
      ) : (
        <p className="text-muted-foreground text-sm">{t("runningHint")}</p>
      )}

      {data.has_unpriced_items ? (
        <p className="text-caution-foreground text-sm font-medium">{t("unpriced")}</p>
      ) : null}

      <Lines
        bill={data}
        editable={editable}
        stagedFor={stagedFor}
        onAction={(mode, charge) => setDialog({ mode, charge })}
      />

      {staged.length ? (
        <div className="bg-caution/10 border-caution/40 space-y-2 rounded-lg border p-3" role="status">
          <p className="text-sm font-medium" data-testid="unsaved-count">
            {t("unsavedCount", { count: staged.length })}
          </p>
          <ul className="space-y-1 text-sm">
            {staged.map((item) => (
              <li key={item.key} className="flex items-center justify-between gap-2">
                <span>{item.label}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => dispatch({ type: "unstage", key: item.key })}
                  aria-label={t("undoStaged", { change: item.label })}
                >
                  {t("undo")}
                </Button>
              </li>
            ))}
          </ul>
          <div className="flex gap-2">
            <Button className="h-tap" onClick={submit} disabled={save.isPending}>
              {t("saveChanges")}
            </Button>
            <Button
              variant="outline"
              className="h-tap"
              onClick={clearStaged}
              disabled={save.isPending}
            >
              {t("discard")}
            </Button>
          </div>
        </div>
      ) : null}

      {dialog ? (
        <ChargeDialog
          state={dialog}
          onClose={() => setDialog(null)}
          onStage={(op, label) => {
            if (!staged.length) setBase(data.version);
            dispatch({ type: "stage", op, label });
            setDialog(null);
          }}
        />
      ) : null}

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("generateFinal")}</DialogTitle>
            <DialogDescription>{t("generateFinalHint")}</DialogDescription>
          </DialogHeader>
          <p className="tabular text-lg font-semibold">{formatMoney(data.total)}</p>
          <DialogFooter>
            <Button variant="outline" className="h-tap" onClick={() => setConfirming(false)}>
              {t("cancel")}
            </Button>
            <Button className="h-tap" onClick={generate} disabled={finalise.isPending}>
              {t("generateFinal")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {data.final_invoice ? (
        <Dialog open={cancelling} onOpenChange={setCancelling}>
          <DialogContent className="sm:max-w-md">
            <form
              className="space-y-4"
              onSubmit={(event) => {
                event.preventDefault();
                if (cancelReason.trim().length < 3) return toast.error(t("reasonRequired"));
                voidFinal();
              }}
            >
              <DialogHeader>
                <DialogTitle>{t("cancelFinal")}</DialogTitle>
                <DialogDescription>{t("cancelFinalHint")}</DialogDescription>
              </DialogHeader>
              <div className="space-y-1.5">
                <Label htmlFor="cancel-final-reason">{t("cancelReason")}</Label>
                <Input
                  id="cancel-final-reason"
                  value={cancelReason}
                  onChange={(event) => setCancelReason(event.target.value)}
                  className="h-tap"
                />
              </div>
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  className="h-tap"
                  onClick={() => setCancelling(false)}
                >
                  {t("keepBill")}
                </Button>
                <Button type="submit" className="h-tap" disabled={cancelFinal.isPending}>
                  {t("cancelFinal")}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      ) : null}
    </div>
  );
}

function Lines({
  bill,
  editable,
  stagedFor,
  onAction,
}: {
  bill: IpdBill;
  editable: boolean;
  stagedFor: (chargeId: string) => Staged[];
  onAction: (mode: "edit" | "discount" | "remove", charge: Charge) => void;
}) {
  const t = useTranslations("ipdBill");
  const categories = useTranslations("categories");

  if (bill.charges.length === 0) {
    return (
      <p className="text-muted-foreground rounded-lg border border-dashed py-8 text-center text-sm">
        {t("noLines")}
      </p>
    );
  }

  const groups = new Map<string, Charge[]>();
  for (const charge of bill.charges) {
    groups.set(charge.category, [...(groups.get(charge.category) ?? []), charge]);
  }

  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-2xl text-sm">
        <caption className="sr-only">{t("lines")}</caption>
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 text-left font-medium">{t("item")}</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">{t("qty")}</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">{t("rate")}</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">{t("discount")}</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">{t("gst")}</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">{t("lineTotal")}</th>
            {editable ? <th scope="col" className="sr-only">{t("actions")}</th> : null}
          </tr>
        </thead>
        {[...groups.entries()].map(([category, charges]) => (
          <tbody key={category} className="divide-border divide-y border-t">
            <tr className="bg-muted/20">
              <th
                scope="rowgroup"
                colSpan={editable ? 7 : 6}
                className="px-3 py-1.5 text-left text-xs font-semibold uppercase tracking-wide"
              >
                {categories(category as "OTHER")}
              </th>
            </tr>
            {charges.map((charge) => {
              const waived = charge.status === "WAIVED";
              const pending = stagedFor(charge.id);
              return (
                <tr key={charge.id} data-testid="ipd-line" className={cn(waived && "text-muted-foreground")}>
                  <td className="px-3 py-2">
                    <div className={cn(waived && "line-through")}>{charge.description}</div>
                    <div className="text-muted-foreground text-xs">
                      {charge.hsn_sac_code ? `SAC/HSN ${charge.hsn_sac_code} · ` : ""}
                      {waived ? t("waived") : null}
                      {charge.needs_pricing ? t("noPrice") : null}
                    </div>
                    {pending.length ? (
                      <div className="text-caution-foreground text-xs font-medium">
                        {t("unsavedOnLine", { count: pending.length })}
                      </div>
                    ) : null}
                  </td>
                  <td className="tabular px-3 py-2 text-right">{Number(charge.quantity)}</td>
                  <td className="tabular px-3 py-2 text-right">{formatMoney(charge.unit_price)}</td>
                  <td className="tabular px-3 py-2 text-right">{formatMoney(charge.discount_amount)}</td>
                  <td className="tabular px-3 py-2 text-right">{Number(charge.gst_rate)}%</td>
                  <td className={cn("tabular px-3 py-2 text-right font-medium", waived && "line-through")}>
                    {formatMoney(charge.total_amount)}
                  </td>
                  {editable ? (
                    <td className="px-2 py-1 text-right whitespace-nowrap">
                      {waived ? null : (
                        <>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-tap"
                            aria-label={t("editLine", { item: charge.description })}
                            onClick={() => onAction("edit", charge)}
                          >
                            <Pencil aria-hidden />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-tap"
                            aria-label={t("discountLine", { item: charge.description })}
                            onClick={() => onAction("discount", charge)}
                          >
                            <Percent aria-hidden />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-tap"
                            aria-label={t("removeLine", { item: charge.description })}
                            onClick={() => onAction("remove", charge)}
                          >
                            <Trash2 aria-hidden />
                          </Button>
                        </>
                      )}
                    </td>
                  ) : null}
                </tr>
              );
            })}
          </tbody>
        ))}
      </table>
    </div>
  );
}

/** One dialog for the four kinds of change; every one needs a reason. */
function ChargeDialog({
  state,
  onClose,
  onStage,
}: {
  state: NonNullable<DialogState>;
  onClose: () => void;
  onStage: (op: BillOp, label: string) => void;
}) {
  const t = useTranslations("ipdBill");
  const categories = useTranslations("categories");
  const charge = state.mode === "add" ? null : state.charge;
  const [code, setCode] = useState("");
  const [description, setDescription] = useState(charge?.description ?? "");
  const [category, setCategory] = useState<ChargeCategory>("OTHER");
  const [quantity, setQuantity] = useState(charge ? String(Number(charge.quantity)) : "1");
  const [unitPrice, setUnitPrice] = useState(charge ? String(charge.unit_price) : "");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const services = useQuery({
    queryKey: ["billing", "services", "active"],
    queryFn: ({ signal }) =>
      api.get<Page<ServiceItem>>("/billing/services", { query: { limit: 200 }, signal }),
    enabled: state.mode === "add",
    staleTime: 5 * 60_000,
  });

  const positive = (value: string) => value.trim() !== "" && Number(value) > 0;
  const title = {
    add: t("addCharge"),
    edit: t("editCharge"),
    discount: t("discountCharge"),
    remove: t("removeCharge"),
  }[state.mode];

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (reason.trim().length < 3) return setError(t("reasonRequired"));
    if (state.mode === "add") {
      if (!code && !description.trim()) return setError(t("describeIt"));
      if (!code && !positive(unitPrice)) return setError(t("priceRequired"));
      if (!positive(quantity)) return setError(t("quantityPositive"));
      const name = code
        ? (services.data?.items.find((item) => item.code === code)?.name ?? code)
        : description.trim();
      return onStage(
        {
          op: "add",
          service_item_code: code || null,
          description: code ? null : description.trim(),
          category,
          quantity,
          unit_price: unitPrice.trim() ? unitPrice : null,
          reason: reason.trim(),
        },
        t("stagedAdd", { item: name }),
      );
    }
    const target = charge as Charge;
    if (state.mode === "edit") {
      const changes = {
        quantity: quantity !== String(Number(target.quantity)) ? quantity : null,
        unit_price: unitPrice !== String(target.unit_price) ? unitPrice : null,
        description: description.trim() !== target.description ? description.trim() : null,
      };
      if (!changes.quantity && !changes.unit_price && !changes.description) {
        return setError(t("nothingChanged"));
      }
      if (changes.quantity && !positive(changes.quantity)) return setError(t("quantityPositive"));
      return onStage(
        { op: "edit", charge_id: target.id, ...changes, reason: reason.trim() },
        t("stagedEdit", { item: target.description }),
      );
    }
    if (state.mode === "discount") {
      if (!positive(amount)) return setError(t("amountPositive"));
      return onStage(
        { op: "discount", charge_id: target.id, amount, reason: reason.trim() },
        t("stagedDiscount", { item: target.description }),
      );
    }
    return onStage(
      { op: "remove", charge_id: target.id, reason: reason.trim() },
      t("stagedRemove", { item: target.description }),
    );
  };

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-h-[92dvh] overflow-y-auto sm:max-w-md">
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{charge ? charge.description : t("addHint")}</DialogDescription>
          </DialogHeader>

          {state.mode === "add" ? (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="charge-service">{t("fromCatalogue")}</Label>
                <select
                  id="charge-service"
                  value={code}
                  onChange={(event) => setCode(event.target.value)}
                  className="border-input bg-background h-tap w-full rounded-md border px-3 text-base"
                >
                  <option value="">{t("freeText")}</option>
                  {services.data?.items
                    .filter((item) => item.is_active)
                    .map((item) => (
                      <option key={item.id} value={item.code}>
                        {item.name}
                      </option>
                    ))}
                </select>
              </div>
              {code ? null : (
                <>
                  <Field id="charge-description" label={t("description")} value={description} onChange={setDescription} />
                  <div className="space-y-1.5">
                    <Label htmlFor="charge-category">{t("category")}</Label>
                    <select
                      id="charge-category"
                      value={category}
                      onChange={(event) => setCategory(event.target.value as ChargeCategory)}
                      className="border-input bg-background h-tap w-full rounded-md border px-3 text-base"
                    >
                      {CATEGORIES.map((option) => (
                        <option key={option} value={option}>
                          {categories(option)}
                        </option>
                      ))}
                    </select>
                  </div>
                </>
              )}
              <Field id="charge-quantity" label={t("quantity")} value={quantity} onChange={setQuantity} numeric />
              <Field
                id="charge-price"
                label={t("unitPrice")}
                value={unitPrice}
                onChange={setUnitPrice}
                numeric
                hint={code ? t("priceFromRateCard") : undefined}
              />
            </>
          ) : null}

          {state.mode === "edit" ? (
            <>
              <Field id="charge-description" label={t("description")} value={description} onChange={setDescription} />
              <Field id="charge-quantity" label={t("quantity")} value={quantity} onChange={setQuantity} numeric />
              <Field id="charge-price" label={t("unitPrice")} value={unitPrice} onChange={setUnitPrice} numeric />
            </>
          ) : null}

          {state.mode === "discount" ? (
            <Field id="charge-amount" label={t("discountAmount")} value={amount} onChange={setAmount} numeric />
          ) : null}

          {state.mode === "remove" ? (
            <p className="text-sm">{t("removeHint")}</p>
          ) : null}

          <Field id="charge-reason" label={t("reason")} value={reason} onChange={setReason} hint={t("reasonHint")} />

          {error ? (
            <p className="text-destructive text-sm" role="alert">
              {error}
            </p>
          ) : null}

          <DialogFooter>
            <Button type="button" variant="outline" className="h-tap" onClick={onClose}>
              {t("cancel")}
            </Button>
            <Button type="submit" className="h-tap">
              {state.mode === "add" ? t("addToBill") : t("stageChange")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  numeric,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  numeric?: boolean;
  hint?: string;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        inputMode={numeric ? "decimal" : undefined}
        autoComplete="off"
        className="h-tap text-base"
        aria-describedby={hint ? `${id}-hint` : undefined}
      />
      {hint ? (
        <p id={`${id}-hint`} className="text-muted-foreground text-xs">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
