"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { arrayMove } from "@dnd-kit/sortable";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { DoctorQueue, QueueEntry } from "@/types/api";

/**
 * Today's queue, one column per doctor — reception's view of the waiting room.
 *
 * Shared by three screens that must agree: the board itself, the doctor
 * picker on the registration form (which shows "4 waiting" next to each
 * name), and the move dialog (which shows the same numbers for the
 * destination). One query key, so a token issued on one screen shows up in
 * the counts on the others without each of them fetching separately.
 *
 * Under the `["queue", ...]` prefix on purpose: every action that changes the
 * queue already invalidates that prefix, and this board must not be the one
 * list that stays stale after a move.
 */
export const QUEUE_BOARD_KEY = ["queue", "board"] as const;

export function useQueueBoard({
  enabled = true,
  paused = false,
}: {
  enabled?: boolean;
  /**
   * Stop background refreshes without dropping the data. Set while a row is
   * being dragged: a refetch landing mid-drag would re-render the list under
   * the pointer and the row would drop somewhere the receptionist did not
   * put it.
   */
  paused?: boolean;
} = {}) {
  return useQuery({
    queryKey: QUEUE_BOARD_KEY,
    queryFn: ({ signal }) => api.get<DoctorQueue[]>("/queue/board", { signal }),
    // Same cadence as the flat queue, for the same reason: several people
    // watch it, and a stale board is a patient sent to a doctor who just
    // went to lunch.
    refetchInterval: paused ? false : 15_000,
    refetchOnWindowFocus: !paused,
    staleTime: 0,
    enabled,
  });
}

export type ReorderInput = {
  doctorId: string;
  entryId: string;
  /** Index the row was dragged from and to, within that doctor's column. */
  from: number;
  to: number;
  /** The row it lands after — `null` for the top. Resolved by the caller. */
  afterEntryId: string | null;
};

/**
 * Change a token's rank in its doctor's line — the drag on the board.
 *
 * Optimistic: the row moves the instant it is dropped, and the request goes
 * out behind it. A drag that snaps back a second later, or a list that jumps
 * on every drop, would teach staff the board is not to be trusted. If the
 * server refuses — the token was called in while it was mid-air, the anchor
 * has since left — the previous order is restored and the reason shown.
 *
 * Always settles by invalidating `["queue"]`: the server renumbers the whole
 * line, and every other screen reading it (the doctor's own list, the nurse's
 * flat queue) must see the same order this one does.
 */
export function useReorderEntry() {
  const t = useTranslations("queue");
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ entryId, afterEntryId }: ReorderInput) =>
      api.post<QueueEntry>(`/queue/${entryId}/reorder`, { after_entry_id: afterEntryId }),

    onMutate: async ({ doctorId, from, to }) => {
      // A refetch already in flight would overwrite the optimistic order
      // with the pre-drag one, then the invalidation would fix it again — a
      // visible flicker, and exactly the "did it take?" doubt to avoid.
      await queryClient.cancelQueries({ queryKey: QUEUE_BOARD_KEY });
      const previous = queryClient.getQueryData<DoctorQueue[]>(QUEUE_BOARD_KEY);
      if (previous) {
        queryClient.setQueryData<DoctorQueue[]>(
          QUEUE_BOARD_KEY,
          previous.map((column) =>
            column.doctor_id === doctorId
              ? { ...column, entries: arrayMove(column.entries ?? [], from, to) }
              : column,
          ),
        );
      }
      return { previous };
    },

    onError: (error, _input, context) => {
      if (context?.previous) queryClient.setQueryData(QUEUE_BOARD_KEY, context.previous);
      if (!isApiError(error)) throw error;
      toast.error(t("reorderFailed", { reason: error.message }));
    },

    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["queue"] });
    },
  });
}

/** How long a "Seen" click can be taken back before it is sent. */
export const SEEN_UNDO_MS = 5_000;

/**
 * Reception's "Seen" button, with a short undo window.
 *
 * Marking a patient seen is final on the server — the token and the booking
 * close, and a closed booking is never reopened. The button sits on every
 * row, next to Move and the drag handle, on a screen used at speed; a
 * misplaced tap is a certainty over a day. So the click strikes the name
 * through at once and the request waits {@link SEEN_UNDO_MS} behind an Undo,
 * the way a mail client holds a sent message.
 *
 * Leaving the screen inside the window sends immediately rather than
 * dropping the click. Closing the browser tab inside it does drop it — the
 * patient is then simply still waiting on the next load, which is the safe
 * way for that to fail.
 */
export function useMarkSeen() {
  const t = useTranslations("queue");
  const common = useTranslations("common");
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<ReadonlySet<string>>(() => new Set());
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const release = useCallback((entryId: string) => {
    setPending((current) => {
      const next = new Set(current);
      next.delete(entryId);
      return next;
    });
  }, []);

  const send = useCallback(
    async (entryId: string) => {
      timers.current.delete(entryId);
      try {
        await api.post<QueueEntry>(`/queue/${entryId}/seen`);
        // Awaited, so the row does not flash back into the line between the
        // strike-through ending and the refreshed board arriving.
        await queryClient.invalidateQueries({ queryKey: ["queue"] });
        void queryClient.invalidateQueries({ queryKey: ["reports", "queue"] });
      } catch (error) {
        toast.error(
          t("markSeenFailed", {
            reason: isApiError(error) ? error.message : common("somethingWentWrong"),
          }),
        );
      } finally {
        release(entryId);
      }
    },
    [common, queryClient, release, t],
  );

  const mark = useCallback(
    (entryId: string) => {
      if (timers.current.has(entryId)) return;
      setPending((current) => new Set(current).add(entryId));
      timers.current.set(
        entryId,
        setTimeout(() => void send(entryId), SEEN_UNDO_MS),
      );
    },
    [send],
  );

  const undo = useCallback(
    (entryId: string) => {
      const timer = timers.current.get(entryId);
      if (timer === undefined) return;
      clearTimeout(timer);
      timers.current.delete(entryId);
      release(entryId);
    },
    [release],
  );

  // Navigating away sends whatever is still waiting out its undo window.
  useEffect(() => {
    const waiting = timers.current;
    return () => {
      for (const [entryId, timer] of waiting) {
        clearTimeout(timer);
        void send(entryId);
      }
      waiting.clear();
    };
  }, [send]);

  return { pending, mark, undo };
}
