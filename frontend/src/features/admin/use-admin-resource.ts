"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { api, type QueryValue } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { Page } from "@/types/api";

/**
 * The list-and-mutate pair every administration screen repeats.
 *
 * Six screens here do the same three things: page a list, create a row, change
 * a row. What differs between them is the *form* — the fields, the validation,
 * the vocabulary — and that is deliberately left to each screen, because a
 * generic form generator is where this kind of shared code stops helping.
 *
 * What is shared is the plumbing worth getting right once: the query key, the
 * invalidation after a write, and turning a failed request into a message a
 * human can act on rather than a silent no-op.
 */
export function useAdminList<T>(
  key: readonly unknown[],
  path: string,
  query?: Record<string, QueryValue>,
) {
  return useQuery({
    queryKey: [...key, query ?? {}],
    queryFn: ({ signal }) => api.get<Page<T>>(path, { query, signal }),
    // Configuration changes when an administrator changes it, which is rarely
    // and never behind their back. Thirty seconds keeps navigation instant
    // without showing a stale price list for a whole shift.
    staleTime: 30_000,
  });
}

/**
 * A write that refreshes the list it belongs to.
 *
 * `onDone` runs only on success, so a dialog closing is never mistaken for a
 * save that worked.
 */
export function useAdminMutation<TResult, TInput>(
  key: readonly unknown[],
  send: (input: TInput) => Promise<TResult>,
  options: { successMessage?: string; onDone?: (result: TResult) => void } = {},
) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: send,
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: key });
      if (options.successMessage) toast.success(options.successMessage);
      options.onDone?.(result);
    },
    onError: (error) => {
      // Field-level errors are bound to inputs by the forms that render them
      // (`applyFieldErrors`); this is the fallback for everything else — a
      // conflict, a permission refusal, a dead backend.
      toast.error(isApiError(error) ? error.message : String(error));
    },
  });
}
