import { NextResponse, type NextRequest } from "next/server";

import { refreshSession } from "@/lib/api/refresh";
import { clearSession, readSession, writeSession } from "@/lib/session";

/**
 * Rotate the token pair, then continue to wherever the user was going.
 *
 * `proxy.ts` sends navigations here when the access token is close to
 * expiring. It exists because server components cannot set cookies during a
 * render: a page that discovers an expired token mid-render can obtain a new
 * one but has nowhere to put it. A route handler can, so the refresh happens
 * here — one redirect, roughly every fifteen minutes per user — and every
 * page then renders with a token that is certain to be valid.
 */

/**
 * Only ever redirect within this application.
 *
 * `next` arrives in a query string, which means a user can be handed a link
 * with any value in it. Without this check that is an open redirect: a
 * convincing "your hospital session expired" phishing page, reached through a
 * genuine hospital URL. Note `//evil.example` — protocol-relative, and a path
 * that a naive "starts with /" test waves straight through.
 */
function safeNext(raw: string | null): string {
  if (!raw) return "/";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  return raw;
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const destination = safeNext(request.nextUrl.searchParams.get("next"));
  const session = await readSession();

  const loginUrl = request.nextUrl.clone();
  loginUrl.pathname = "/login";
  loginUrl.search = "";
  if (destination !== "/") loginUrl.searchParams.set("next", destination);

  if (!session) {
    return NextResponse.redirect(loginUrl);
  }

  try {
    const refreshed = await refreshSession(session.refreshToken);
    const target = request.nextUrl.clone();
    target.pathname = destination.split("?")[0];
    target.search = destination.includes("?") ? `?${destination.split("?").slice(1).join("?")}` : "";

    const response = NextResponse.redirect(target);
    writeSession(
      {
        set: (options) => response.cookies.set(options),
        delete: (name) => response.cookies.delete(name),
      },
      refreshed,
    );
    return response;
  } catch {
    // Expired, revoked, or flagged as reuse. Either way there is no session
    // left to save — clear the cookie so the next request does not loop back
    // through here, and ask for credentials.
    const response = NextResponse.redirect(loginUrl);
    clearSession({
      set: (options) => response.cookies.set(options),
      delete: (name) => response.cookies.delete(name),
    });
    return response;
  }
}

export const dynamic = "force-dynamic";
