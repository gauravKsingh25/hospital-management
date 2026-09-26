import "server-only";

import { callApi } from "@/lib/api/fetch";
import { ApiError } from "@/lib/api/error";
import {
  type Session,
  type TokenPair,
  isExpiring,
  sessionFromTokens,
} from "@/lib/session";

/**
 * Refresh-token rotation, done exactly once per token.
 *
 * ## Why this needs care
 *
 * The backend rotates refresh tokens and detects reuse: presenting the same
 * refresh token twice is treated as theft, and the entire token family is
 * revoked (CLAUDE.md §12). That is the correct security posture, and it means
 * a benign race — two requests refreshing the same token at the same moment —
 * would log the user out mid-consultation.
 *
 * Which is exactly what happens without this module. A doctor's screen opens
 * six queries at once; if the access token expired a second earlier, all six
 * try to refresh, five of them look like an attack, and the session dies.
 *
 * The in-flight map collapses concurrent refreshes of the same token into one
 * call. Everyone waits on the same promise and receives the same new pair.
 *
 * ## The limit of this, stated plainly
 *
 * The map is per process. Behind a load balancer running several Next
 * instances, two requests can still land on different instances and race.
 * The window is the width of one refresh call, and only opens in the seconds
 * around expiry, so it is rare — but it is not zero. Two ways to close it
 * when this deploys to more than one instance:
 *
 *   * sticky sessions, so one browser's requests stay on one instance; or
 *   * move this lock into Redis, which the stack already runs for ARQ.
 *
 * Sticky sessions are the cheaper of the two and cost nothing here, since
 * nothing else in this app holds per-instance state.
 */
const inFlight = new Map<string, Promise<Session>>();

async function performRefresh(refreshToken: string): Promise<Session> {
  const tokens = await callApi<TokenPair>("/auth/refresh", {
    method: "POST",
    body: { refresh_token: refreshToken },
  });
  return sessionFromTokens(tokens);
}

/**
 * Exchange a refresh token for a new pair, deduplicating concurrent callers.
 *
 * Throws `ApiError` when the refresh token is expired, revoked or reused —
 * the caller's job is then to clear the cookie and send the user to log in.
 */
export function refreshSession(refreshToken: string): Promise<Session> {
  const existing = inFlight.get(refreshToken);
  if (existing) return existing;

  const attempt = performRefresh(refreshToken).finally(() => {
    // Cleared only after settling, so a caller arriving during the call joins
    // it, and one arriving after gets a fresh attempt with the new token.
    inFlight.delete(refreshToken);
  });

  inFlight.set(refreshToken, attempt);
  return attempt;
}

export type FreshSession =
  | { status: "valid"; session: Session; rotated: boolean }
  | { status: "expired" };

/**
 * Return a session with a usable access token, refreshing if it is close to
 * expiry.
 *
 * `rotated` tells the caller whether the cookie needs rewriting. Callers that
 * cannot write cookies (server components, during render) can still read the
 * returned token — they just cannot persist it, which is why `proxy.ts`
 * refreshes ahead of navigation instead.
 */
export async function ensureFreshSession(session: Session | null): Promise<FreshSession> {
  if (!session) return { status: "expired" };
  if (!isExpiring(session)) return { status: "valid", session, rotated: false };

  try {
    return { status: "valid", session: await refreshSession(session.refreshToken), rotated: true };
  } catch (error) {
    if (error instanceof ApiError && (error.isUnauthenticated || error.status === 403)) {
      return { status: "expired" };
    }
    // A network blip is not a logged-out user. Hand back the token we have
    // and let the request fail on its own terms; throwing "expired" here
    // would sign people out every time the backend hiccups.
    return { status: "valid", session, rotated: false };
  }
}
