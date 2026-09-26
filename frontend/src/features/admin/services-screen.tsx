"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Plus } from "lucide-react";

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
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { AdminEmpty, AdminShell } from "@/features/admin/admin-shell";
import { useAdminList, useAdminMutation } from "@/features/admin/use-admin-resource";
import { formatMoney } from "@/features/billing/money";
import { api } from "@/lib/api/client";
import type { ChargeCategory, RateCard, ServiceItem, ServicePrice } from "@/types/api";

const SERVICES_KEY = ["admin", "services"] as const;
const PRICES_KEY = ["admin", "prices"] as const;

const CATEGORIES: ChargeCategory[] = [
  "CONSULTATION",
  "LAB",
  "RADIOLOGY",
  "PROCEDURE",
  "PHARMACY",
  "ROOM",
  "REGISTRATION",
  "OTHER",
];

/**
 * The price list.
 *
 * This is the screen whose absence was most consequential, and the reason is
 * worth stating because it is counter-intuitive: **billing never refuses a
 * charge for missing configuration.** An unpriced service is captured at zero
 * and flagged `needs_pricing`, deliberately, so that a half-configured
 * hospital can still order a blood test.
 *
 * The failure mode that creates is silent. A hospital with no rate card bills
 * nobody, nothing reaches the cash counter, and every screen looks like it is
 * working. So this screen leads with whether a default rate card exists at all
 * — that flag is what a walk-in cash patient falls back to — and marks every
 * service that has no price on it.
 */
export function ServicesScreen({ canManage }: { canManage: boolean }) {
  const t = useTranslations("admin");

  const [cardId, setCardId] = useState<string>("");
  const [creatingService, setCreatingService] = useState(false);
  const [creatingCard, setCreatingCard] = useState(false);
  const [pricing, setPricing] = useState<ServiceItem | null>(null);

  const cards = useAdminList<RateCard>(["admin", "rate-cards"], "/billing/rate-cards", {
    limit: 50,
  });
  const services = useAdminList<ServiceItem>(SERVICES_KEY, "/billing/services", { limit: 200 });

  const cardList = cards.data?.items ?? [];
  const defaultCard = cardList.find((card) => card.is_default);
  const selectedCard = cardId || defaultCard?.id || cardList[0]?.id || "";

  // Prices are fetched per service, so they are fetched once for the whole
  // page here rather than per row: `GET /billing/services/{id}/prices` would
  // otherwise be one request per service on a list of two hundred.
  const priceLookup = useQuery({
    queryKey: [...PRICES_KEY, selectedCard],
    enabled: Boolean(selectedCard) && (services.data?.items.length ?? 0) > 0,
    queryFn: async ({ signal }) => {
      const items = services.data?.items ?? [];
      const entries = await Promise.all(
        items.map(async (item) => {
          const prices = await api.get<ServicePrice[]>(
            `/billing/services/${item.id}/prices`,
            { signal },
          );
          const match = prices.find((price) => price.rate_card_id === selectedCard);
          return [item.id, match?.price ?? null] as const;
        }),
      );
      return Object.fromEntries(entries) as Record<string, string | null>;
    },
    staleTime: 30_000,
  });

  const rows = services.data?.items ?? [];
  const unpriced = rows.filter((row) => !priceLookup.data?.[row.id]).length;

  return (
    <AdminShell
      title={t("servicesTitle")}
      description={t("servicesDescription")}
      action={
        canManage ? (
          <div className="flex gap-2">
            <Button variant="outline" className="h-tap" onClick={() => setCreatingCard(true)}>
              {t("addRateCard")}
            </Button>
            <Button className="h-tap" onClick={() => setCreatingService(true)}>
              <Plus aria-hidden className="size-4" />
              {t("addService")}
            </Button>
          </div>
        ) : undefined
      }
    >
      {cardList.length === 0 ? (
        <p className="border-critical/40 bg-critical/10 rounded-md border px-3 py-2 text-sm">
          {t("noRateCards")}
        </p>
      ) : !defaultCard ? (
        <p className="border-caution/50 bg-caution/10 rounded-md border px-3 py-2 text-sm">
          {t("noDefaultRateCard")}
        </p>
      ) : null}

      {cardList.length > 0 ? (
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="rate-card">{t("rateCard")}</Label>
            <Select value={selectedCard} onValueChange={(value) => setCardId(value ?? "")}>
              <SelectTrigger id="rate-card" className="h-tap w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {cardList.map((card) => (
                  <SelectItem key={card.id} value={card.id}>
                    {card.name} ({card.payer_type})
                    {card.is_default ? ` · ${t("default")}` : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {unpriced > 0 ? (
            <p className="text-caution-foreground pb-2 text-sm font-medium">
              {t("unpricedServices", { count: unpriced })}
            </p>
          ) : null}
        </div>
      ) : null}

      {rows.length === 0 ? (
        <AdminEmpty message={t("servicesEmpty")} hint={t("servicesEmptyHint")} />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableCaption className="sr-only">{t("servicesTitle")}</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>{t("code")}</TableHead>
                <TableHead>{t("name")}</TableHead>
                <TableHead>{t("category")}</TableHead>
                <TableHead>{t("gst")}</TableHead>
                <TableHead className="text-right">{t("price")}</TableHead>
                <TableHead className="text-right">{t("actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((item) => {
                const price = priceLookup.data?.[item.id] ?? null;
                return (
                  <TableRow key={item.id} data-testid="service-row">
                    <TableCell className="tabular font-medium">{item.code}</TableCell>
                    <TableCell>{item.name}</TableCell>
                    <TableCell className="text-muted-foreground">{item.category}</TableCell>
                    <TableCell className="text-muted-foreground tabular">
                      {item.is_gst_exempt ? t("exempt") : `${Number(item.gst_rate)}%`}
                    </TableCell>
                    <TableCell className="tabular text-right">
                      {price ? (
                        formatMoney(price)
                      ) : (
                        <span className="text-caution-foreground font-medium">
                          {t("notPriced")}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {canManage && selectedCard ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-tap"
                          onClick={() => setPricing(item)}
                        >
                          {t("setPrice")}
                        </Button>
                      ) : null}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <ServiceDialog open={creatingService} onOpenChange={setCreatingService} />
      <RateCardDialog open={creatingCard} onOpenChange={setCreatingCard} />

      {pricing ? (
        <PriceDialog
          item={pricing}
          rateCardId={selectedCard}
          current={priceLookup.data?.[pricing.id] ?? ""}
          open
          onOpenChange={(next) => {
            if (!next) setPricing(null);
          }}
        />
      ) : null}
    </AdminShell>
  );
}

function ServiceDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");

  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState<ChargeCategory>("OTHER");
  const [exempt, setExempt] = useState(true);
  const [gstRate, setGstRate] = useState("");

  const create = useAdminMutation(
    SERVICES_KEY,
    () =>
      api.post<ServiceItem>("/billing/services", {
        code,
        name,
        category,
        is_gst_exempt: exempt,
        gst_rate: exempt ? "0" : gstRate,
      }),
    {
      successMessage: t("serviceCreated"),
      onDone: () => {
        setCode("");
        setName("");
        setGstRate("");
        onOpenChange(false);
      },
    },
  );

  // The backend refuses both halves of an incoherent tax setup; checking here
  // means the administrator is told before the round trip.
  const taxCoherent = exempt ? true : Number(gstRate) > 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("addService")}</DialogTitle>
          <DialogDescription>{t("addServiceHint")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="service-code">{t("code")}</Label>
              <Input
                id="service-code"
                value={code}
                onChange={(event) => setCode(event.target.value.toUpperCase())}
                className="h-tap tabular"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="service-category">{t("category")}</Label>
              <Select
                value={category}
                onValueChange={(value) => setCategory((value ?? "OTHER") as ChargeCategory)}
              >
                <SelectTrigger id="service-category" className="h-tap w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CATEGORIES.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="service-name">{t("name")}</Label>
            <Input
              id="service-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="h-tap"
            />
          </div>

          {/* CLAUDE.md §9: most clinical services are GST-exempt, but
              diagnostics, pharmacy and room rent may not be. */}
          <label className="flex min-h-tap cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={exempt}
              onChange={(event) => setExempt(event.target.checked)}
              className="accent-primary size-4"
            />
            {t("gstExempt")}
          </label>

          {!exempt ? (
            <div className="space-y-1.5">
              <Label htmlFor="service-gst">{t("gstRate")}</Label>
              <Input
                id="service-gst"
                value={gstRate}
                onChange={(event) => setGstRate(event.target.value)}
                inputMode="decimal"
                className="h-tap tabular w-32"
              />
              {!taxCoherent ? (
                <p role="alert" className="text-caution-foreground text-sm">
                  {t("gstRateRequired")}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!code.trim() || !name.trim() || !taxCoherent || create.isPending}
            onClick={() => create.mutate(undefined)}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RateCardDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");

  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [payerType, setPayerType] = useState("CASH");
  const [isDefault, setIsDefault] = useState(false);

  const create = useAdminMutation(
    ["admin", "rate-cards"],
    () =>
      api.post<RateCard>("/billing/rate-cards", {
        code,
        name,
        payer_type: payerType,
        is_default: isDefault,
      }),
    {
      successMessage: t("rateCardCreated"),
      onDone: () => {
        setCode("");
        setName("");
        onOpenChange(false);
      },
    },
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("addRateCard")}</DialogTitle>
          <DialogDescription>{t("addRateCardHint")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="card-code">{t("code")}</Label>
              <Input
                id="card-code"
                value={code}
                onChange={(event) => setCode(event.target.value.toUpperCase())}
                className="h-tap tabular"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="card-payer">{t("payerType")}</Label>
              <Select value={payerType} onValueChange={(value) => setPayerType(value ?? "CASH")}>
                <SelectTrigger id="card-payer" className="h-tap w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {["CASH", "INSURANCE", "SCHEME", "CORPORATE"].map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="card-name">{t("name")}</Label>
            <Input
              id="card-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="h-tap"
            />
          </div>

          <label className="flex min-h-tap cursor-pointer items-start gap-2.5 text-sm">
            <input
              type="checkbox"
              checked={isDefault}
              onChange={(event) => setIsDefault(event.target.checked)}
              className="accent-primary mt-3 size-4 shrink-0"
            />
            <span className="py-2.5">
              {t("makeDefault")}
              <span className="text-muted-foreground block text-xs">{t("makeDefaultHint")}</span>
            </span>
          </label>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!code.trim() || !name.trim() || create.isPending}
            onClick={() => create.mutate(undefined)}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PriceDialog({
  item,
  rateCardId,
  current,
  open,
  onOpenChange,
}: {
  item: ServiceItem;
  rateCardId: string;
  current: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");
  const [price, setPrice] = useState(current ?? "");

  const save = useAdminMutation(
    ["admin", "prices"],
    () =>
      api.put<ServicePrice>(`/billing/services/${item.id}/prices`, {
        rate_card_id: rateCardId,
        price,
      }),
    { successMessage: t("priceSaved"), onDone: () => onOpenChange(false) },
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("setPrice")}</DialogTitle>
          <DialogDescription>
            {item.code} — {item.name}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5 text-left">
          <Label htmlFor="service-price">{t("price")}</Label>
          <Input
            id="service-price"
            value={price}
            onChange={(event) => setPrice(event.target.value)}
            inputMode="decimal"
            className="h-tap tabular text-lg"
            autoFocus
          />
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!(Number(price) >= 0) || price.trim() === "" || save.isPending}
            onClick={() => save.mutate(undefined)}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
