"use client";

import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Plus } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Order, OrderType } from "@/types/api";

const ORDER_TYPES: OrderType[] = ["LAB", "RADIOLOGY", "PHARMACY", "PROCEDURE", "REFERRAL"];

/**
 * Orders — tests, imaging and medicines.
 *
 * The one field here that is genuinely a clinical decision is
 * `review_in_visit`, and it is worth its checkbox. It is the difference
 * between "wait, get this done and come back to me" and "get this done before
 * your next visit", and the backend turns it into `AWAITING_RESULTS` versus
 * `PENDING_CLEARANCE` — which is what reception reads off the queue to know
 * whether to send the patient back upstairs or to the counter.
 *
 * Without it somebody at the front desk has to guess, and CLAUDE.md §6 is
 * explicit that the state machine exists so nobody has to.
 */
export function OrdersPanel({
  encounterId,
  orders,
  disabled,
  onSaved,
}: {
  encounterId: string;
  orders: Order[];
  disabled: boolean;
  onSaved: () => void;
}) {
  const t = useTranslations("consultation");

  const [orderType, setOrderType] = useState<OrderType>("LAB");
  const [itemName, setItemName] = useState("");
  const [reviewInVisit, setReviewInVisit] = useState(false);

  const place = useMutation({
    mutationFn: () =>
      api.post<Order>(`/encounters/${encounterId}/orders`, {
        order_type: orderType,
        item_name: itemName.trim(),
        review_in_visit: reviewInVisit,
      }),
    onSuccess: () => {
      setItemName("");
      setReviewInVisit(false);
      onSaved();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const open = orders.filter((order) => order.status !== "CANCELLED");

  return (
    // `aria-labelledby` makes this a named landmark, so a screen-reader user
    // can jump straight to it. A bare <section> has no role at all, which
    // means the consultation screen would be one undifferentiated blob to
    // anyone navigating by region.
    <section className="space-y-3 rounded-lg border p-4" aria-labelledby="orders-heading">
      <h2 id="orders-heading" className="text-sm font-semibold">
        {t("orders")}
      </h2>

      {open.length > 0 ? (
        <ul className="space-y-1.5">
          {open.map((order) => (
            <li key={order.id} className="flex items-start gap-2 text-sm">
              <span
                className={cn(
                  "mt-1 size-1.5 shrink-0 rounded-full",
                  order.status === "COMPLETED" ? "bg-success" : "bg-caution",
                )}
                aria-hidden
              />
              <span className="flex-1">{order.item_name}</span>
              <span className="text-muted-foreground text-xs">
                {t(`orderType${order.order_type}` as "orderTypeLAB")}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-muted-foreground text-sm">{t("noOrders")}</p>
      )}

      <div className="space-y-2">
        <div className="flex gap-2">
          <Select value={orderType} onValueChange={(value) => setOrderType(value as OrderType)}>
            <SelectTrigger className="h-tap w-36 shrink-0" aria-label={t("orderType")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ORDER_TYPES.map((type) => (
                <SelectItem key={type} value={type}>
                  {t(`orderType${type}` as "orderTypeLAB")}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Input
            value={itemName}
            onChange={(event) => setItemName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && itemName.trim()) {
                event.preventDefault();
                place.mutate();
              }
            }}
            disabled={disabled}
            placeholder={t("orderItemPlaceholder")}
            className="h-tap"
            aria-label={t("orderItem")}
          />

          <Button
            onClick={() => place.mutate()}
            disabled={disabled || place.isPending || itemName.trim().length === 0}
            className="h-tap shrink-0"
            aria-label={t("placeOrder")}
          >
            <Plus aria-hidden className="size-4" />
          </Button>
        </div>

        <label className="flex min-h-tap cursor-pointer items-start gap-2.5 text-sm">
          <input
            type="checkbox"
            checked={reviewInVisit}
            onChange={(event) => setReviewInVisit(event.target.checked)}
            disabled={disabled}
            className="accent-primary mt-3 size-4 shrink-0"
          />
          <span className="py-2.5">
            {t("reviewInVisit")}
            <span className="text-muted-foreground block text-xs">{t("reviewInVisitHint")}</span>
          </span>
        </label>
      </div>
    </section>
  );
}
