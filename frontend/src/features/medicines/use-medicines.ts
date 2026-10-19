"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "@/lib/api/client";
import type {
  CostBreakdown,
  MedicationRoute,
  MedicineForm,
  MedicineItem,
  MedicineIssue,
  Page,
} from "@/types/api";

/**
 * The medicine list and what a stay used from it.
 *
 * Every key sits under `["ipd"]`, so anything that invalidates the inpatient
 * prefix — an admission, a dose signed — also refreshes these.
 */
export const MEDICINES_KEY = ["ipd", "medicines"] as const;
export const issuesKey = (admissionId: string) => ["ipd", "medicine-issues", admissionId] as const;
export const costsKey = (encounterId: string) => ["ipd", "costs", encounterId] as const;

export const MEDICINE_FORMS: MedicineForm[] = [
  "INJECTION",
  "IV_FLUID",
  "TABLET",
  "CAPSULE",
  "SYRUP",
  "OINTMENT",
  "DROPS",
  "INHALER",
  "CONSUMABLE",
  "OTHER",
];

/**
 * What a new item of each form usually is, so choosing "IV fluid" fills in
 * "bottle" and "IV" rather than making somebody type them. Only a starting
 * point: every field stays editable.
 */
export const FORM_DEFAULTS: Record<MedicineForm, { unit: string; route: MedicationRoute | null }> = {
  INJECTION: { unit: "vial", route: "IV" },
  IV_FLUID: { unit: "bottle", route: "IV" },
  TABLET: { unit: "tablet", route: "ORAL" },
  CAPSULE: { unit: "capsule", route: "ORAL" },
  SYRUP: { unit: "bottle", route: "ORAL" },
  OINTMENT: { unit: "tube", route: "TOPICAL" },
  DROPS: { unit: "bottle", route: "OPHTHALMIC" },
  INHALER: { unit: "inhaler", route: "INHALED" },
  CONSUMABLE: { unit: "piece", route: null },
  OTHER: { unit: "unit", route: null },
};

/** Waits until typing pauses, so a search is one request rather than one per key. */
export function useDebounced<T>(value: T, delayMs = 200): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}

/**
 * The list, searched on the server.
 *
 * Server-side rather than filtering one downloaded page: a hospital's list
 * grows past any page size, and a search that silently misses item 201 is a
 * nurse concluding the hospital does not stock something it does.
 */
export function useMedicines({
  search = "",
  form = null,
  includeInactive = false,
  enabled = true,
  limit = 200,
}: {
  search?: string;
  form?: MedicineForm | null;
  includeInactive?: boolean;
  enabled?: boolean;
  limit?: number;
} = {}) {
  const term = useDebounced(search.trim());
  return useQuery({
    queryKey: [...MEDICINES_KEY, { term, form, includeInactive, limit }],
    queryFn: ({ signal }) =>
      api.get<Page<MedicineItem>>("/ipd/medicines", {
        query: {
          search: term || undefined,
          form: form ?? undefined,
          include_inactive: includeInactive,
          limit,
        },
        signal,
      }),
    enabled,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });
}

export function useMedicineIssues(admissionId: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: issuesKey(admissionId),
    queryFn: ({ signal }) =>
      api.get<Page<MedicineIssue>>(`/ipd/admissions/${admissionId}/medicine-issues`, {
        query: { limit: 200 },
        signal,
      }),
    enabled,
    staleTime: 0,
  });
}

export function useStayCosts(encounterId: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: costsKey(encounterId),
    queryFn: ({ signal }) =>
      api.get<CostBreakdown>(`/billing/accounts/${encounterId}/costs`, { signal }),
    enabled,
    staleTime: 0,
    // A dose signed on the chart, a bed-day accrued overnight: the cost moves
    // while this page is open.
    refetchInterval: 30_000,
  });
}
