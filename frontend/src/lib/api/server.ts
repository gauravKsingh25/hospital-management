import "server-only";

import { redirect } from "next/navigation";
import { cache } from "react";

import { ApiError } from "@/lib/api/error";
import { callApi, type ApiRequest } from "@/lib/api/fetch";
import { ensureFreshSession } from "@/lib/api/refresh";
import { readSession } from "@/lib/session";
import type { CurrentUser } from "@/types/api";

/**
 * The server-side data access layer.
 *
 * Server components and server actions call the API through here, so the
 * session is resolved in one place rather than at every call site. The
 * browser's path is different and separate — it goes to `/bff`, which attaches
 * the token itself (see `app/bff/[...path]/route.ts`).
 *
 * Note the asymmetry, which is deliberate: this layer *reads* a session but
 * never writes one. Cookies cannot be set during a server render, so token
 * rotation is `proxy.ts`'s job, ahead of the render. By the time anything
 * here runs, the token is already fresh.
 */

/** Fetch as the signed-in user, sending them to sign in if they are not. */
export async function serverFetch<T>(path: string, request: ApiRequest = {}): Promise<T> {
  const fresh = await ensureFreshSession(await readSession());

  if (fresh.status === "expired") {
    // `redirect` throws — nothing after this line runs.
    redirect("/login");
  }

  return callApi<T>(path, { ...request, accessToken: fresh.session.accessToken });
}

/**
 * Like `serverFetch`, but returns null on 404 and 403 instead of throwing.
 *
 * For screens that compose several optional panels: a doctor without
 * `report:revenue` should see a dashboard missing the revenue card, not an
 * error page (CLAUDE.md §7b, role-based dashboards).
 */
export async function serverFetchOptional<T>(
  path: string,
  request: ApiRequest = {},
): Promise<T | null> {
  try {
    return await serverFetch<T>(path, request);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.isForbidden)) {
      return null;
    }
    throw error;
  }
}

/**
 * The signed-in user, with their roles and permissions.
 *
 * Fetched rather than cached in the cookie, on purpose. Permissions in this
 * system are data (CLAUDE.md §8) — an administrator can change a role without
 * a deploy — and a permission set frozen into a cookie at login would keep a
 * revoked role alive until the user signed out. One call per navigation is a
 * small price for a revocation that takes effect immediately.
 *
 * `cache()` collapses it to a single call per render, no matter how many
 * components ask.
 */
export const getCurrentUser = cache(async (): Promise<CurrentUser> => {
  return serverFetch<CurrentUser>("/auth/me");
});

/**
 * The current user, or null when there is no session — without redirecting.
 *
 * For the few places that render differently rather than bouncing to login.
 */
export const getCurrentUserOrNull = cache(async (): Promise<CurrentUser | null> => {
  const session = await readSession();
  if (!session) return null;
  try {
    return await getCurrentUser();
  } catch (error) {
    if (error instanceof ApiError && error.isUnauthenticated) return null;
    throw error;
  }
});
