"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { PillBottle, Plus, Search } from "lucide-react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatMoney } from "@/features/billing/money";
import {
  FORM_DEFAULTS,
  MEDICINE_FORMS,
  MEDICINES_KEY,
  useMedicines,
} from "@/features/medicines/use-medicines";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { DrugSchedule, MedicationRoute, MedicineForm, MedicineItem } from "@/types/api";

const ROUTES: MedicationRoute[] = [
  "IV",
  "IM",
  "SC",
  "ORAL",
  "INHALED",
  "TOPICAL",
  "NASOGASTRIC",
  "RECTAL",
  "OPHTHALMIC",
  "OTHER",
];
const SCHEDULES: DrugSchedule[] = ["NONE", "H", "H1", "X", "NARCOTIC"];
// The Select needs a value for "no route" — a consumable is not given by any.
const NO_ROUTE = "NONE";

/**
 * The medicine list: the presets a ward charges for.
 *
 * IV fluids, injections, tablets and supplies, each with its usual dose and
 * its price. Everything else in the inpatient flow reads from here: a doctor
 * picks from it on the chart and every dose given is billed; the desk or a
 * nurse records an IV set used against a stay and it is billed.
 *
 * The price shown is the default rate card's — what a cash patient pays. It
 * lives on billing's price list, which is why an administrator sees these
 * items there too and can set a different price for an insurer.
 */
export function MedicinesScreen({ canManage }: { canManage: boolean }) {
  const t = useTranslations("medicines");
  const common = useTranslations("common");

  const [search, setSearch] = useState("");
  const [form, setForm] = useState<MedicineForm | null>(null);
  const [showInactive, setShowInactive] = useState(false);
  const [editing, setEditing] = useState<MedicineItem | "new" | null>(null);

  const list = useMedicines({ search, form, includeInactive: showInactive });
  const rows = list.data?.items ?? [];
  const total = list.data?.total ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="relative w-full max-w-sm">
          <Search
            aria-hidden
            className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
          />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t("searchPlaceholder")}
            aria-label={common("search")}
            className="h-tap pl-9"
          />
        </div>
        {canManage ? (
          <Button className="h-tap" onClick={() => setEditing("new")} data-testid="add-medicine">
            <Plus aria-hidden className="size-4" />
            {t("add")}
          </Button>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-2" role="group" aria-label={t("filterBy")}>
        <FilterChip selected={form === null} onClick={() => setForm(null)}>
          {t("all")}
        </FilterChip>
        {MEDICINE_FORMS.map((option) => (
          <FilterChip key={option} selected={form === option} onClick={() => setForm(option)}>
            {t("formLabel", { form: option })}
          </FilterChip>
        ))}
        {canManage ? (
          <label className="text-muted-foreground ml-auto flex min-h-tap cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={(event) => setShowInactive(event.target.checked)}
              className="accent-primary size-4"
            />
            {t("showInactive")}
          </label>
        ) : null}
      </div>

      {list.isPending ? (
        <div className="space-y-2" aria-busy="true">
          {[0, 1, 2].map((row) => (
            <Skeleton key={row} className="h-12 w-full rounded-md" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-lg border border-dashed py-14 text-center">
          <PillBottle aria-hidden className="size-6" />
          <p className="text-foreground text-sm font-medium">
            {search.trim()
              ? t("noMatch", { term: search.trim() })
              : form
                ? t("noneOfType")
                : t("emptyList")}
          </p>
          {canManage && !search.trim() && !form ? (
            <p className="text-sm">{t("emptyHint")}</p>
          ) : null}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableCaption className="sr-only">{t("title")}</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>{t("name")}</TableHead>
                <TableHead>{t("form")}</TableHead>
                <TableHead>{t("doseAndRoute")}</TableHead>
                <TableHead className="text-right">{t("price")}</TableHead>
                {canManage ? <TableHead className="w-0" /> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((item) => (
                <TableRow
                  key={item.id}
                  data-testid="medicine-row"
                  className={cn(!item.is_active && "text-muted-foreground")}
                >
                  <TableCell>
                    <div className="font-medium">{item.name}</div>
                    <div className="text-muted-foreground tabular text-xs">
                      {item.code}
                      {item.drug_schedule !== "NONE"
                        ? ` · ${t("schedule", { schedule: item.drug_schedule })}`
                        : ""}
                      {!item.is_active ? ` · ${t("switchedOff")}` : ""}
                    </div>
                  </TableCell>
                  <TableCell>{t("formLabel", { form: item.form })}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {[item.dose, item.route].filter(Boolean).join(" · ") || "—"}
                  </TableCell>
                  <TableCell className="tabular text-right whitespace-nowrap">
                    {item.price !== null && item.price !== undefined ? (
                      <>
                        {formatMoney(item.price)}
                        <span className="text-muted-foreground text-xs"> / {item.unit}</span>
                      </>
                    ) : (
                      <span className="text-caution-foreground font-medium">
                        {t("notPriced")}
                      </span>
                    )}
                  </TableCell>
                  {canManage ? (
                    <TableCell className="text-right">
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-tap"
                        onClick={() => setEditing(item)}
                        aria-label={t("editAria", { name: item.name })}
                      >
                        {t("edit")}
                      </Button>
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {total > rows.length ? (
        <p className="text-muted-foreground text-xs">
          {t("showingSome", { shown: rows.length, total })}
        </p>
      ) : null}

      {editing ? (
        <MedicineDialog
          item={editing === "new" ? null : editing}
          open
          onOpenChange={(next) => {
            if (!next) setEditing(null);
          }}
        />
      ) : null}
    </div>
  );
}

function FilterChip({
  selected,
  onClick,
  children,
}: {
  selected: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={cn(
        "min-h-tap rounded-full border px-3 text-sm",
        selected ? "border-primary bg-primary/10 font-medium" : "hover:bg-accent",
      )}
    >
      {children}
    </button>
  );
}

/**
 * Add an item, or edit one. Required: name, type, unit, price. The type fills
 * in the usual unit and route; the dose is optional because a cannula has none.
 */
function MedicineDialog({
  item,
  open,
  onOpenChange,
}: {
  item: MedicineItem | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("medicines");
  const common = useTranslations("common");
  const queryClient = useQueryClient();

  const [name, setName] = useState(item?.name ?? "");
  const [form, setForm] = useState<MedicineForm>(item?.form ?? "INJECTION");
  const [dose, setDose] = useState(item?.dose ?? "");
  const [route, setRoute] = useState<string>(
    item ? (item.route ?? NO_ROUTE) : (FORM_DEFAULTS.INJECTION.route ?? NO_ROUTE),
  );
  const [unit, setUnit] = useState(item?.unit ?? FORM_DEFAULTS.INJECTION.unit);
  const [price, setPrice] = useState(item?.price ?? "");
  const [schedule, setSchedule] = useState<DrugSchedule>(item?.drug_schedule ?? "NONE");
  const [active, setActive] = useState(item?.is_active ?? true);
  // Once somebody types a unit or picks a route, changing the type stops
  // overwriting it.
  const [touched, setTouched] = useState({ unit: Boolean(item), route: Boolean(item) });

  const chooseForm = (next: MedicineForm) => {
    setForm(next);
    if (!touched.unit) setUnit(FORM_DEFAULTS[next].unit);
    if (!touched.route) setRoute(FORM_DEFAULTS[next].route ?? NO_ROUTE);
  };

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name: name.trim(),
        form,
        dose: dose.trim() || null,
        route: route === NO_ROUTE ? null : route,
        unit: unit.trim(),
        drug_schedule: schedule,
        price: price.trim(),
      };
      return item
        ? api.patch<MedicineItem>(`/ipd/medicines/${item.id}`, { ...body, is_active: active })
        : api.post<MedicineItem>("/ipd/medicines", body);
    },
    onSuccess: (saved) => {
      toast.success(item ? t("saved", { name: saved.name }) : t("added", { name: saved.name }));
      void queryClient.invalidateQueries({ queryKey: MEDICINES_KEY });
      onOpenChange(false);
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const priceValid = price.trim() !== "" && Number(price) >= 0 && Number.isFinite(Number(price));
  const valid = name.trim() && unit.trim() && priceValid;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{item ? t("editTitle") : t("addTitle")}</DialogTitle>
          <DialogDescription>{item ? item.code : t("addHint")}</DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (valid) save.mutate();
          }}
          className="space-y-3 text-left"
          noValidate
        >
          <div className="space-y-1.5">
            <Label htmlFor="medicine-name">{t("name")}</Label>
            <Input
              id="medicine-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="h-tap"
              placeholder={t("namePlaceholder")}
              autoFocus
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="medicine-form">{t("form")}</Label>
              <Select
                value={form}
                onValueChange={(value) => chooseForm((value ?? "OTHER") as MedicineForm)}
              >
                <SelectTrigger id="medicine-form" className="h-tap w-full">
                  <SelectValue>
                    {(value: string | null) => (value ? t("formLabel", { form: value }) : "")}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {MEDICINE_FORMS.map((option) => (
                    <SelectItem key={option} value={option}>
                      {t("formLabel", { form: option })}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="medicine-dose">
                {t("dose")} <span className="text-muted-foreground">({common("optional")})</span>
              </Label>
              <Input
                id="medicine-dose"
                value={dose}
                onChange={(event) => setDose(event.target.value)}
                className="h-tap"
                placeholder={t("dosePlaceholder")}
              />
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="medicine-route">{t("route")}</Label>
              <Select
                value={route}
                onValueChange={(value) => {
                  setRoute(value ?? NO_ROUTE);
                  setTouched((current) => ({ ...current, route: true }));
                }}
              >
                <SelectTrigger id="medicine-route" className="h-tap w-full">
                  <SelectValue>
                    {(value: string | null) => (!value || value === NO_ROUTE ? t("noRoute") : value)}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_ROUTE}>{t("noRoute")}</SelectItem>
                  {ROUTES.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="medicine-unit">{t("unit")}</Label>
              <Input
                id="medicine-unit"
                value={unit}
                onChange={(event) => {
                  setUnit(event.target.value);
                  setTouched((current) => ({ ...current, unit: true }));
                }}
                className="h-tap"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="medicine-price">{t("pricePerUnit", { unit: unit || "…" })}</Label>
              <Input
                id="medicine-price"
                value={price}
                onChange={(event) => setPrice(event.target.value)}
                inputMode="decimal"
                className="h-tap tabular"
                placeholder="0.00"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="medicine-schedule">{t("scheduleLabel")}</Label>
            <Select
              value={schedule}
              onValueChange={(value) => setSchedule((value ?? "NONE") as DrugSchedule)}
            >
              <SelectTrigger id="medicine-schedule" className="h-tap w-full sm:w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SCHEDULES.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-muted-foreground text-xs">{t("scheduleHint")}</p>
          </div>

          {item ? (
            <label className="flex min-h-tap cursor-pointer items-start gap-2.5 text-sm">
              <input
                type="checkbox"
                checked={active}
                onChange={(event) => setActive(event.target.checked)}
                className="accent-primary mt-3 size-4 shrink-0"
              />
              <span className="py-2.5">
                {t("inUse")}
                <span className="text-muted-foreground block text-xs">{t("inUseHint")}</span>
              </span>
            </label>
          ) : null}

          <p className="text-muted-foreground text-xs">{t("priceHint")}</p>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              className="h-tap"
              onClick={() => onOpenChange(false)}
            >
              {common("cancel")}
            </Button>
            <Button type="submit" className="h-tap" disabled={!valid || save.isPending}>
              {common("save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
