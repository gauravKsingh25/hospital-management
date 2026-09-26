"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { AdmissionRequest, Page } from "@/types/api";

/**
 * The OPD → admission desk hand-off, as the two counters see it.
 *
 * Every key sits under `ADMISSION_REQUESTS_KEY`, so sending a patient, admitting
 * one or turning one away refreshes both sides with one invalidation — the
 * reception board's "At admission desk" chip and the desk's own list must not
 * disagree about where a patient is.
 */
export const ADMISSION_REQUESTS_KEY = ["ipd", "admission-requests"] as const;

// Several people watch both screens; the queue board polls at the same rate.
const POLL_MS = 15_000;

/** Local midnight, as an instant — "today" for a counter in the hospital. */
function startOfToday(): string {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  return start.toISOString();
}

/**
 * Everything sent today, newest first. Reception reads it to label the
 * patients it has sent; the desk reads it for "handled today".
 */
export function useTodaysAdmissionRequests({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    // The date is in the key so the list rolls over at midnight rather than
    // carrying yesterday's evening into the morning.
    queryKey: [...ADMISSION_REQUESTS_KEY, "today", new Date().toDateString()],
    queryFn: ({ signal }) =>
      api.get<Page<AdmissionRequest>>("/ipd/admission-requests", {
        query: { since: startOfToday(), newest_first: true, limit: 200 },
        signal,
      }),
    refetchInterval: POLL_MS,
    staleTime: 0,
    enabled,
  });
}

/** The desk's worklist: everyone still waiting, oldest first, whatever the day. */
export function usePendingAdmissionRequests() {
  return useQuery({
    queryKey: [...ADMISSION_REQUESTS_KEY, "pending"],
    queryFn: ({ signal }) =>
      api.get<Page<AdmissionRequest>>("/ipd/admission-requests", {
        query: { status: "PENDING", limit: 200 },
        signal,
      }),
    refetchInterval: POLL_MS,
    staleTime: 0,
  });
}
