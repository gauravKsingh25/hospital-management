import { QueryClient, isServer } from "@tanstack/react-query";

import { isApiError } from "@/lib/api/error";

/**
 * TanStack Query defaults, tuned for a hospital floor rather than a website.
 *
 * The governing fact: several people look at the same queue at once — the
 * receptionist who checks a patient in, the nurse who takes vitals, the
 * doctor waiting for the next name. Data that is thirty seconds stale is a
 * doctor calling a patient who has already left.
 */
function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Ten seconds. Long enough that moving between two screens does not
        // refetch everything; short enough that a queue is never meaningfully
        // wrong. Screens that need tighter (the live queue) override it.
        staleTime: 10_000,

        // Coming back to a tab is exactly when a stale queue misleads
        // somebody, so this stays on.
        refetchOnWindowFocus: true,

        // Refetch when the network returns. A ward's wifi drops; the screen
        // should heal itself rather than wait for a human to press reload.
        refetchOnReconnect: true,

        retry: (failureCount, error) => {
          // Never retry a refusal. A 403 will be a 403 the third time too,
          // and retrying a 422 just resubmits bad input.
          if (isApiError(error) && !error.isTransient) return false;
          return failureCount < 2;
        },

        // 250ms, 500ms, 1s. Capped low because someone is standing at a
        // counter watching a spinner, not a background job.
        retryDelay: (attempt) => Math.min(250 * 2 ** attempt, 2_000),
      },
      mutations: {
        // Mutations are never retried automatically. Registering a patient or
        // recording a payment twice is far worse than telling staff it failed
        // and letting them decide — the backend's idempotency guarantees
        // cover its own retries, not ours.
        retry: false,
      },
    },
  });
}

let browserQueryClient: QueryClient | undefined;

/**
 * One client per browser, a new one per server render.
 *
 * Sharing a client across server renders would let one user's cached patient
 * list be served to the next request — a cross-tenant leak created entirely
 * on the frontend, and one that no amount of Row-Level Security downstream
 * would catch.
 */
export function getQueryClient(): QueryClient {
  if (isServer) return makeQueryClient();
  browserQueryClient ??= makeQueryClient();
  return browserQueryClient;
}
