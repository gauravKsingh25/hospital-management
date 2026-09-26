import "server-only";

import { cookies } from "next/headers";
import { cache } from "react";

import { isProduction } from "@/lib/env";
import {
  SESSION_COOKIE_NAME,
  parseSession,
  serialiseSession,
  type Session,
} from "@/lib/session-cookie";

/**
 * Reading and writing the session cookie.
 *
 * ## Why we store the backend's tokens, and not a session of our own
 *
 * The usual Next.js pattern is to mint an encrypted session cookie holding a
 * user id. That is right when Next owns the session. Here FastAPI owns it —
 * it issues the pair, rotates refresh tokens, detects reuse and revokes whole
 * token families (CLAUDE.md §12). Wrapping its tokens inside a second session
 * of our own would create two expiries to keep in step and, worse, a logout
 * that clears our cookie while the backend's refresh token stays valid.
 * Storing the backend's tokens directly keeps revocation authoritative where
 * it belongs.
 *
 * ## Why one cookie and not three
 *
 * The three values must change together. Cookies are set individually, so
 * three cookies have interleavings where the access token is new and the
 * expiry is old — which reads as "valid" and is not. One cookie makes the
 * write atomic.
 *
 * ## Why httpOnly is not optional here
 *
 * `localStorage` is readable by any script on the page. One XSS in one
 * dependency, and an attacker holds a token that can read the hospital's
 * entire patient list. httpOnly means the page's JavaScript cannot read the
 * token even in that case; the attacker is limited to riding the session in
 * the browser, which is bounded and auditable. Under the DPDP Act that
 * difference is the difference between an incident and a reportable breach.
 */

export type { Session, TokenPair } from "@/lib/session-cookie";
export {
  SESSION_COOKIE_NAME,
  REFRESH_MARGIN_SECONDS,
  isExpiring,
  sessionFromTokens,
} from "@/lib/session-cookie";

/**
 * The current session, or null.
 *
 * `cache()` memoises this for the duration of one server render, so a layout
 * and three components asking for the session parse one cookie once.
 */
export const readSession = cache(async (): Promise<Session | null> => {
  const store = await cookies();
  return parseSession(store.get(SESSION_COOKIE_NAME)?.value);
});

/**
 * The subset of the cookie APIs this module needs.
 *
 * Both `cookies()` and `NextResponse.cookies` satisfy it, which is what lets
 * the same writer serve server actions (which mutate the request's store) and
 * the BFF proxy (which must attach the cookie to a response it is returning).
 */
export type MutableCookies = {
  set: (options: {
    name: string;
    value: string;
    httpOnly: boolean;
    secure: boolean;
    sameSite: "lax" | "strict" | "none";
    path: string;
    maxAge?: number;
  }) => unknown;
  delete: (name: string) => unknown;
};

export function writeSession(store: MutableCookies, session: Session): void {
  store.set({
    name: SESSION_COOKIE_NAME,
    value: serialiseSession(session),
    httpOnly: true,
    secure: isProduction,
    // `lax` rather than `strict`: strict would drop the cookie on any
    // cross-site navigation into the app, so following a link from an email
    // or a hospital intranet page would land staff on the login screen with a
    // valid session sitting unused in their browser.
    sameSite: "lax",
    path: "/",
    // No maxAge: a session cookie dies with the browser. A shared counter
    // machine at a hospital should not stay logged in overnight because
    // somebody closed the lid (CLAUDE.md §12, session timeouts).
  });
}

export function clearSession(store: MutableCookies): void {
  store.delete(SESSION_COOKIE_NAME);
}
