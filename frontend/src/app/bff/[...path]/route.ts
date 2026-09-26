import { cookies } from "next/headers";
import { NextResponse, type NextRequest } from "next/server";

import { ApiError } from "@/lib/api/error";
import { callApiRaw } from "@/lib/api/fetch";
import { ensureFreshSession, refreshSession } from "@/lib/api/refresh";
import { clearSession, readSession, writeSession, type Session } from "@/lib/session";

/**
 * The backend-for-frontend proxy: every browser request to the API comes here.
 *
 * ## What this buys
 *
 * The access token is attached *here*, server-side, from an httpOnly cookie.
 * The browser therefore never holds a credential it could leak — no token in
 * `localStorage`, none in a JS variable, none in a header the page can read.
 * An XSS in any dependency still cannot exfiltrate a token that will keep
 * working after the tab closes.
 *
 * It also puts refresh-token rotation in exactly one place. Doing it in a
 * client-side interceptor means every tab races every other tab, and the
 * backend's reuse detection reads those races as theft (CLAUDE.md §12).
 *
 * ## What this costs, honestly
 *
 * One extra hop: browser → Next → FastAPI. In a deployment where both run in
 * the same region that is a couple of milliseconds, and it buys the property
 * above. The alternative — the browser calling FastAPI directly with a token
 * it holds — is one hop cheaper and one XSS away from the hospital's entire
 * patient list. For a system under the DPDP Act that is not a close call.
 *
 * ## Authorisation is not done here
 *
 * This proxy forwards; it does not decide. RBAC and tenant isolation are
 * enforced by FastAPI on every route (CLAUDE.md §8), and re-implementing
 * either here would create a second policy to keep in step with the first.
 * The only thing refused below is the set of endpoints that mint sessions,
 * because those must go through the deliberate paths that write the cookie.
 */

/** Endpoints that issue or destroy sessions. Not reachable through here. */
const SESSION_ENDPOINTS = new Set(["auth/login", "auth/refresh", "auth/logout"]);

/**
 * Response headers worth passing back. An allowlist rather than a copy: the
 * backend's `set-cookie` or auth headers must never reach the browser, and an
 * allowlist fails closed when the backend starts sending something new.
 */
const FORWARDED_RESPONSE_HEADERS = ["content-type", "content-disposition", "content-length"];

type RouteParams = { params: Promise<{ path: string[] }> };

function unauthenticated(): NextResponse {
  return NextResponse.json(
    {
      error: {
        code: "not_authenticated",
        message: "Your session has ended. Please sign in again.",
      },
    },
    { status: 401 },
  );
}

async function handle(request: NextRequest, { params }: RouteParams): Promise<NextResponse> {
  const { path } = await params;
  const target = path.join("/");

  if (SESSION_ENDPOINTS.has(target)) {
    return NextResponse.json(
      {
        error: {
          code: "route_not_proxied",
          message: "Sign-in is handled by the application, not through this endpoint.",
        },
      },
      { status: 404 },
    );
  }

  const stored = await readSession();
  const fresh = await ensureFreshSession(stored);

  if (fresh.status === "expired") {
    const store = await cookies();
    clearSession(store);
    return unauthenticated();
  }

  let session = fresh.session;
  let rotated = fresh.rotated;

  // The body is forwarded untouched rather than parsed and re-serialised:
  // this proxy has no opinion about payloads, and re-encoding one is a way to
  // corrupt it. GET and HEAD have no body by definition.
  const body =
    request.method === "GET" || request.method === "HEAD" ? undefined : await request.text();

  const forward = (accessToken: string) =>
    callApiRaw(`/${target}`, {
      method: request.method as "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
      rawBody: body || undefined,
      accessToken,
      query: Object.fromEntries(request.nextUrl.searchParams.entries()),
      headers: {
        ...(request.headers.get("content-type")
          ? { "Content-Type": request.headers.get("content-type") as string }
          : {}),
        // Preserve the caller's language choice so the backend can localise
        // anything it renders (CLAUDE.md §9).
        ...(request.headers.get("accept-language")
          ? { "Accept-Language": request.headers.get("accept-language") as string }
          : {}),
      },
      signal: request.signal,
    });

  let response: Response;
  try {
    response = await forward(session.accessToken);

    // A 401 despite a token we believed was current. Causes: the clock skewed,
    // the account was deactivated, or the signing key rotated. One refresh
    // attempt distinguishes "stale token" from "session genuinely over" —
    // and only one, because a loop here would hammer the backend.
    if (response.status === 401) {
      try {
        session = await refreshSession(session.refreshToken);
        rotated = true;
        response = await forward(session.accessToken);
      } catch {
        const store = await cookies();
        clearSession(store);
        return unauthenticated();
      }
    }
  } catch (error) {
    // `callApiRaw` converts an unreachable backend into a 503 ApiError.
    if (error instanceof ApiError) {
      return NextResponse.json(
        { error: { code: error.code, message: error.message } },
        { status: error.status },
      );
    }
    throw error;
  }

  const headers = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = response.headers.get(name);
    if (value) headers.set(name, value);
  }
  // Patient data must not sit in a shared cache or a browser's back-forward
  // store on a counter machine that several staff use in turn.
  headers.set("Cache-Control", "no-store, private");

  const proxied = new NextResponse(response.body, { status: response.status, headers });

  if (rotated) {
    writeRotatedSession(proxied, session);
  }

  return proxied;
}

/**
 * Persist a rotated token pair onto the outgoing response.
 *
 * Written on the response rather than through `cookies()` because a rotation
 * that happened during a request must reach the browser attached to *that*
 * request's response — the next one will already be using the new token.
 */
function writeRotatedSession(response: NextResponse, session: Session): void {
  writeSession(
    {
      set: (options) => response.cookies.set(options),
      delete: (name) => response.cookies.delete(name),
    },
    session,
  );
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const PUT = handle;
export const DELETE = handle;

/**
 * Never prerendered, never cached. Every response is tenant- and
 * user-specific, and a cached one would be another tenant's data.
 */
export const dynamic = "force-dynamic";
