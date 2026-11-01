import { NextResponse, type NextRequest } from "next/server";

import { callApi } from "@/lib/api/fetch";
import { isProduction } from "@/lib/env";
import { sameOrigin } from "@/lib/same-origin";
import {
  LOCK_COOKIE_NAME,
  LOCK_MAX_AGE_SECONDS,
  clearSession,
  readSession,
} from "@/lib/session";

/**
 * Lock a screen nobody is using (SEC-04): end the session, remember whose.
 *
 * A route handler rather than a server action on purpose. A server action
 * that changes cookies re-renders the page on the server — which, with the
 * session gone, redirects to the login form and throws away the work the
 * lock exists to keep. `fetch` to a route handler changes the cookies and
 * leaves the page alone.
 *
 * The session really ends: the refresh token is revoked and the cookie
 * cleared. An overlay alone is bypassed by reloading the page; this way a
 * reload lands on the login form.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  if (!sameOrigin(request)) return new NextResponse(null, { status: 403 });

  const payload = (await request.json().catch(() => null)) as { email?: unknown } | null;
  const email = typeof payload?.email === "string" ? payload.email.trim() : "";
  if (!email.includes("@") || email.length > 255) {
    return new NextResponse(null, { status: 400 });
  }

  const session = await readSession();
  const response = new NextResponse(null, { status: 204 });
  clearSession(response.cookies);
  response.cookies.set({
    name: LOCK_COOKIE_NAME,
    value: email,
    httpOnly: true,
    secure: isProduction,
    sameSite: "strict",
    path: "/",
    maxAge: LOCK_MAX_AGE_SECONDS,
  });

  if (session) {
    try {
      await callApi("/auth/logout", {
        method: "POST",
        body: { refresh_token: session.refreshToken },
      });
    } catch {
      // Best effort, as in `signOut`: this browser no longer holds the token.
    }
  }
  return response;
}

