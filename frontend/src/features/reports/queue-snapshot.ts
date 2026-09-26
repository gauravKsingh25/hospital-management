"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { QueueSnapshot } from "@/types/api";

/**
 * Today's queue analytics, shared by every screen that shows them.
 *
 * One hook and one query key, so the dashboard and reception's delay strip
 * read the same cache. Two screens quoting different waiting counts at the
 * same moment is exactly the kind of small inconsistency that teaches staff
 * the numbers are made up.
 *
 * Cheap by comparison with the rest of `reporting` — it reads a single day —
 * which is why this is the one report that polls.
 */
export const QUEUE_SNAPSHOT_KEY = ["reports", "queue"] as const;

export function useQueueSnapshot({
  initial,
  enabled = true,
}: {
  initial?: QueueSnapshot | null;
  enabled?: boolean;
} = {}) {
  return useQuery({
    queryKey: QUEUE_SNAPSHOT_KEY,
    queryFn: ({ signal }) => api.get<QueueSnapshot>("/reports/queue", { signal }),
    initialData: initial ?? undefined,
    // The same cadence as the queue board, because it is the same information.
    refetchInterval: 30_000,
    enabled,
  });
}
