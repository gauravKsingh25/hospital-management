"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { BillChangesRequest, Invoice, IpdBill } from "@/types/api";

/**
 * The IPD bill editor's server state.
 *
 * Staged changes live in the editor (a reducer), never here: nothing reaches
 * the server until Save, and Save sends every staged change in one request
 * with the version the first change was staged against — not whatever a
 * background reload fetched since. The server applies all or none and
 * returns the recalculated bill, which replaces the cached one.
 */
export type BillOp = BillChangesRequest["ops"][number];

export const ipdBillKey = (encounterId: string) => ["billing", "ipd-bill", encounterId] as const;

export function useIpdBill(encounterId: string) {
  return useQuery({
    queryKey: ipdBillKey(encounterId),
    queryFn: ({ signal }) =>
      api.get<IpdBill>(`/billing/accounts/${encounterId}/ipd-bill`, { signal }),
  });
}

export function useSaveIpdBill(encounterId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (request: BillChangesRequest) =>
      api.post<IpdBill>(`/billing/accounts/${encounterId}/ipd-bill/changes`, request),
    onSuccess: (bill) => {
      client.setQueryData(ipdBillKey(encounterId), bill);
      void client.invalidateQueries({ queryKey: ["billing", "history"] });
    },
  });
}

/**
 * Edit on an issued bill (OPD or a final IPD bill): the server issues a new
 * bill with the changes, moves the receipts onto it and cancels this one.
 */
export function useCorrectBill() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ invoiceId, reason, ops }: { invoiceId: string; reason: string; ops: BillOp[] }) =>
      api.post<Invoice>(`/billing/invoices/${invoiceId}/correct`, { reason, ops }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["billing"] });
    },
  });
}

/** Void the final bill so the stay can be corrected and billed again. */
export function useCancelFinalBill(encounterId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (reason: string) =>
      api.post<Invoice>(`/billing/accounts/${encounterId}/final-bill/cancel`, { reason }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ipdBillKey(encounterId) });
      void client.invalidateQueries({ queryKey: ["billing", "history"] });
    },
  });
}

export function useFinaliseIpdBill(encounterId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<Invoice>(`/billing/accounts/${encounterId}/final-bill`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ipdBillKey(encounterId) });
      void client.invalidateQueries({ queryKey: ["billing", "history"] });
    },
  });
}
