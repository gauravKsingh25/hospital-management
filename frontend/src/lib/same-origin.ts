import "server-only";

import type { NextRequest } from "next/server";

/**
 * True when a state-changing request came from this application's own page.
 *
 * Compared against the `Host` the browser addressed, not `nextUrl`: behind a
 * proxy or in the dev container, `nextUrl` is the server's own bind address
 * (`0.0.0.0:3000`), which no browser ever sends as its origin. Browsers mark
 * every fetch with `Sec-Fetch-Site`, which settles it where `Origin` is
 * missing. A page on another site can drive neither.
 */
export function sameOrigin(request: NextRequest): boolean {
  const site = request.headers.get("sec-fetch-site");
  if (site !== null) return site === "same-origin";

  const origin = request.headers.get("origin");
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  if (!origin || !host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
