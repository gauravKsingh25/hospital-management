import "server-only";

import { env } from "@/lib/env";
import { ApiError, apiErrorFromResponse } from "@/lib/api/error";

/**
 * The one place this app talks to FastAPI.
 *
 * Everything above it — server components, the `/bff` proxy, server actions —
 * goes through here, which is what makes "the browser never holds a token"
 * enforceable rather than aspirational.
 */

export type ApiRequest = {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  /** Serialised as JSON. Use `rawBody` to forward an untouched payload. */
  body?: unknown;
  rawBody?: BodyInit;
  /** `undefined` and `null` entries are dropped rather than sent as "null". */
  query?: Record<string, string | number | boolean | null | undefined>;
  accessToken?: string | null;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /**
   * Next's fetch cache. Defaults to `no-store`: every response here is
   * hospital data that is either patient-specific or changes minute to
   * minute. A cached queue is a wrong queue.
   */
  cache?: RequestCache;
};

export function buildUrl(path: string, query?: ApiRequest["query"]): string {
  const url = new URL(
    path.startsWith("/") ? `${env.API_BASE_URL}${path}` : `${env.API_BASE_URL}/${path}`,
  );
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/** Perform the request and return the raw `Response`, for the proxy to stream. */
export async function callApiRaw(path: string, request: ApiRequest = {}): Promise<Response> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...request.headers,
  };

  if (request.accessToken) {
    headers.Authorization = `Bearer ${request.accessToken}`;
  }

  let body: BodyInit | undefined = request.rawBody;
  if (request.body !== undefined) {
    body = JSON.stringify(request.body);
    headers["Content-Type"] = "application/json";
  }

  // A timeout of our own, because `fetch` has none. Without this a hung
  // backend holds a Next server worker until the platform kills it, and the
  // symptom staff report is "the whole system is slow" rather than "billing
  // is down".
  const timeout = AbortSignal.timeout(env.API_TIMEOUT_MS);
  const signal = request.signal
    ? AbortSignal.any([request.signal, timeout])
    : timeout;

  try {
    return await fetch(buildUrl(path, request.query), {
      method: request.method ?? "GET",
      headers,
      body,
      signal,
      cache: request.cache ?? "no-store",
    });
  } catch (cause) {
    // The backend is unreachable, or we timed out. Presented as a 503 so
    // callers have one branch for "the API is not answering" rather than two.
    throw new ApiError({
      status: 503,
      code: cause instanceof Error && cause.name === "TimeoutError" ? "api_timeout" : "api_unreachable",
      message:
        "Cannot reach the hospital server. Check the connection and try again.",
      details: { cause: cause instanceof Error ? cause.message : String(cause) },
    });
  }
}

/** Perform the request and decode the JSON body, throwing `ApiError` on failure. */
export async function callApi<T>(path: string, request: ApiRequest = {}): Promise<T> {
  const response = await callApiRaw(path, request);

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }

  // 204 is a real answer from several endpoints (logout, change-password).
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as T;
  }

  return (await response.json()) as T;
}
