import { NextResponse, type NextRequest } from "next/server";

import { isExpiring, parseSession, SESSION_COOKIE_NAME } from "@/lib/session-cookie";

/**
 * Runs before every page request.
 *
 * > Next.js 16 renamed Middleware to Proxy. Same mechanism, same file
 * > position (`src/proxy.ts`, beside `app/`), different name.
 *
 * Two jobs, and deliberately only two:
 *
 * **1. An optimistic session check.** No cookie means no session, so send the
 * user to sign in without paying for a render they cannot see. This is
 * explicitly *not* the authorisation decision — the Next.js docs warn against
 * treating proxy as a security boundary, and rightly: it sees a cookie, not a
 * verified session. Every actual decision is made by FastAPI, which validates
 * the token, resolves the tenant and checks the permission on every route
 * (CLAUDE.md §8). What happens here only decides which screen renders.
 *
 * **2. Refreshing ahead of a navigation.** Server components cannot set
 * cookies during render, so a component that discovers an expired token has
 * no way to persist a new one. Catching it here — before the render — and
 * bouncing through `/auth/refresh` means every page renders with a valid
 * token. Cost: one redirect roughly every fifteen minutes, per user.
 *
 * Note there is no `fetch` in here. Proxy runs on every request, so anything
 * slow in this file is slow for the whole application; the expiry check is a
 * timestamp comparison against a cookie already in the request.
 */

/** Reachable without a session. Everything else requires one. */
const PUBLIC_PATHS = ["/login", "/auth/refresh", "/health"];

/** Where a user with `must_change_password` is allowed to go (CLAUDE.md §12). */
const PASSWORD_CHANGE_PATH = "/change-password";

function isPublic(pathname: string): boolean {
  return PUBLIC_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`));
}

function buildCsp(nonce: string, isDev: boolean): string {
  // `strict-dynamic` means scripts loaded by a nonced script are trusted too,
  // which is what lets Next's chunk loader work without allowlisting every
  // bundle path. `unsafe-eval` is a development-only concession: React uses
  // eval to rebuild server stack traces in the browser.
  //
  // `connect-src 'self'` is worth pausing on. It is the reason a compromised
  // dependency cannot post patient data to an attacker's host — and it costs
  // nothing here precisely because the browser only ever talks to this app's
  // own `/bff` proxy, never directly to the API.
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
    // No `'unsafe-inline'` here, deliberately. It used to be listed as a
    // fallback and did nothing at all: the CSP spec says a nonce in a
    // directive makes the browser *ignore* `'unsafe-inline'` in that same
    // directive, which is the whole point of nonces. Chrome says so in the
    // violation itself — "'unsafe-inline' is ignored if either a hash or
    // nonce value is present". Leaving it in described a permission the
    // browser was never granting, which is worse than not having it: it hid
    // the fact that Sonner's runtime-injected stylesheet was being blocked
    // and every toast in the app was rendering unstyled. Next's own guidance
    // omits it too.
    `style-src 'self' 'nonce-${nonce}'`,
    "img-src 'self' blob: data:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    // Clickjacking a "confirm death record" button is not a hypothetical risk
    // worth carrying for a system with no reason to be framed.
    "frame-ancestors 'none'",
    ...(isDev ? [] : ["upgrade-insecure-requests"]),
  ].join("; ");
}

function withSecurityHeaders(response: NextResponse, csp: string): NextResponse {
  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("X-Content-Type-Options", "nosniff");
  // Patient identifiers appear in URLs. Never leak one in a Referer header.
  response.headers.set("Referrer-Policy", "no-referrer");
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set(
    "Permissions-Policy",
    "camera=(), microphone=(), geolocation=(), payment=()",
  );
  return response;
}

export function proxy(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;

  const nonce = btoa(crypto.randomUUID());
  const csp = buildCsp(nonce, process.env.NODE_ENV === "development");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const proceed = () =>
    withSecurityHeaders(NextResponse.next({ request: { headers: requestHeaders } }), csp);

  const redirectTo = (path: string, params?: Record<string, string>) => {
    const url = request.nextUrl.clone();
    url.pathname = path;
    url.search = "";
    for (const [key, value] of Object.entries(params ?? {})) {
      url.searchParams.set(key, value);
    }
    return withSecurityHeaders(NextResponse.redirect(url), csp);
  };

  // The BFF answers with JSON, including its own 401. Redirecting it would
  // hand `fetch` an HTML login page to parse as JSON — a confusing failure in
  // place of a clear one.
  if (pathname.startsWith("/bff/")) {
    return proceed();
  }

  const session = parseSession(request.cookies.get(SESSION_COOKIE_NAME)?.value);

  if (isPublic(pathname)) {
    // Already signed in and heading for the login screen: skip it.
    if (pathname === "/login" && session && !isExpiring(session)) {
      return redirectTo("/");
    }
    return proceed();
  }

  if (!session) {
    // `next` brings the user back where they were going once they sign in —
    // the difference between "log in again" and "log in again and re-find
    // the patient you had open".
    return redirectTo("/login", { next: `${pathname}${search}` });
  }

  if (session.mustChangePassword && pathname !== PASSWORD_CHANGE_PATH) {
    return redirectTo(PASSWORD_CHANGE_PATH);
  }

  if (isExpiring(session)) {
    return redirectTo("/auth/refresh", { next: `${pathname}${search}` });
  }

  return proceed();
}

export const config = {
  /**
   * Everything except static assets and the favicon. Static files carry no
   * session and gain nothing from a check, and running this on each of them
   * would add a hop to every image on the page.
   */
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)"],
};
