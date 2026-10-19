"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { CollectPayment, CollectPaymentResult } from "@/types/api";

import { formatMoney } from "@/features/billing/money";

/**
 * Take everything a visit owes — the reception row's "Paid" and the payment
 * dialog's "Received". One request: the server assembles, issues and pays.
 *
 * Settles by invalidating `["queue"]` and `["billing"]` whatever happened.
 * On success the row turns into "Paid"; on a `balance_changed` refusal the
 * dialog and the row must show the new amount, not the one that was refused.
 */
export function useCollect(encounterId: string) {
  const t = useTranslations("billing");
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: CollectPayment) =>
      api.post<CollectPaymentResult>(`/billing/accounts/${encounterId}/collect`, body),
    onSuccess: (result) =>
      toast.success(
        t("collected", {
          amount: formatMoney(result.amount),
          receipt: result.receipts.map((receipt) => receipt.receipt_number).join(", "),
        }),
      ),
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["queue"] });
      void queryClient.invalidateQueries({ queryKey: ["billing"] });
    },
  });
}
