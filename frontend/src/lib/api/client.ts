import { ApiError, apiErrorFromResponse } from "@/lib/api/error";

/**
 * The browser's API client. Talks to `/bff`, never to FastAPI.
 *
 * There is no token here, and no way to obtain one — that is the whole point.
 * `/bff` attaches it server-side from an httpOnly cookie, so this module has
 * nothing worth stealing and needs no interceptor, no token refresh and no
 * retry-after-401 dance. It is a `fetch` wrapper, and that is all it should
 * ever grow into.
 */

const BFF_PREFIX = "/bff";

type Scalar = string | number | boolean | null | undefined;
/** An array repeats the key — `?patient_id=a&patient_id=b` — which FastAPI reads as a list. */
export type QueryValue = Scalar | readonly Scalar[];

function buildPath(path: string, query?: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== undefined && item !== null && item !== "") {
        search.append(key, String(item));
      }
    }
  }
  const suffix = search.toString();
  const base = `${BFF_PREFIX}${path.startsWith("/") ? path : `/${path}`}`;
  return suffix ? `${base}?${suffix}` : base;
}

type RequestOptions = {
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
};

async function request<T>(
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  let response: Response;

  try {
    // A file goes as itself — its bytes, labelled with its own type — not
    // wrapped in JSON or a multipart form. Everything else is JSON.
    const file = body instanceof Blob ? body : null;
    response = await fetch(buildPath(path, options.query), {
      method,
      headers: file
        ? { "Content-Type": file.type || "application/octet-stream" }
        : body === undefined
          ? {}
          : { "Content-Type": "application/json" },
      body: file ?? (body === undefined ? undefined : JSON.stringify(body)),
      signal: options.signal,
      // Same-origin by definition, but stated so a future change to the
      // fetch defaults cannot quietly stop sending the session cookie.
      credentials: "same-origin",
    });
  } catch (cause) {
    // An aborted request is the caller's own doing — usually React unmounting
    // a component or TanStack Query cancelling a stale fetch. Surfacing it as
    // a network error would light up the screen with a false alarm every time
    // a user navigates.
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;

    throw new ApiError({
      status: 0,
      code: "network_error",
      message: "Lost connection to the hospital server.",
    });
  }

  if (!response.ok) {
    const error = await apiErrorFromResponse(response);

    // The session ended underneath us — the refresh token expired, was
    // revoked, or an administrator deactivated the account mid-shift. A full
    // reload is deliberate: it re-runs `proxy.ts`, which sends the user to
    // the login screen with `next` set, so signing back in returns them to
    // the patient they had open rather than to a blank home screen.
    if (error.isUnauthenticated && typeof window !== "undefined" && !sessionLocked) {
      // An absolute URL, and a hard navigation rather than `router.push`.
      // Both are deliberate: the full load re-runs `proxy.ts` (which owns the
      // decision about where an unauthenticated user goes) and discards the
      // in-memory query cache, which on a shared counter machine may hold the
      // previous user's patient list.
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.assign(new URL(`/login?next=${next}`, window.location.origin));
    }

    throw error;
  }

  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as T;
  }

  return (await response.json()) as T;
}

/**
 * Set while the idle lock is up (SEC-04). The lock ends the server session on
 * purpose, so every background poll now gets a 401 — and the usual response,
 * a hard navigation to the login page, would throw away the work under the
 * lock that unlocking exists to keep.
 */
let sessionLocked = false;

export function setSessionLocked(locked: boolean): void {
  sessionLocked = locked;
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => request<T>("GET", path, undefined, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("POST", path, body, options),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("PATCH", path, body, options),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("PUT", path, body, options),
  delete: <T>(path: string, options?: RequestOptions) =>
    request<T>("DELETE", path, undefined, options),
};
