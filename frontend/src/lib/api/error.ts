/**
 * The backend's error envelope, as a typed exception.
 *
 * FastAPI returns every failure in one shape (backend `core/exceptions.py`):
 *
 * ```json
 * { "error": { "code": "validation_error",
 *              "message": "The submitted data is not valid.",
 *              "details": { "fields": [{ "field": "phone", "message": "..." }] } } }
 * ```
 *
 * Because that shape is guaranteed, the frontend never has to guess at an
 * error. `code` is stable and machine-readable, so screens branch on it;
 * `message` is written for staff and is safe to show verbatim; `fields` maps
 * straight onto form inputs.
 *
 * This module is deliberately isomorphic — no `server-only` — because both
 * the server fetch layer and the browser query layer throw it, and screens
 * catch the same type either side of the boundary.
 */

export type FieldError = {
  field: string;
  message: string;
  type?: string;
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields: FieldError[];
  readonly details: Record<string, unknown> | undefined;

  constructor(options: {
    status: number;
    code: string;
    message: string;
    fields?: FieldError[];
    details?: Record<string, unknown>;
  }) {
    super(options.message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.fields = options.fields ?? [];
    this.details = options.details;
  }

  /** The session is gone or was never there. */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  /** Authenticated, but this role may not do this (CLAUDE.md §8). */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /** Worth another attempt: the backend is briefly unavailable. */
  get isTransient(): boolean {
    return this.status === 503 || this.status === 504 || this.status === 0;
  }
}

type ErrorEnvelope = {
  error?: {
    code?: string;
    message?: string;
    details?: Record<string, unknown>;
  };
};

/** Build an `ApiError` from a non-2xx response, tolerating a non-JSON body. */
export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  let envelope: ErrorEnvelope = {};
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    // A proxy, a load balancer or a crash can produce HTML or nothing at all.
    // Falling through leaves the generic message below, which is honest.
  }

  const details = envelope.error?.details;
  const rawFields = details?.fields;

  return new ApiError({
    status: response.status,
    code: envelope.error?.code ?? `http_${response.status}`,
    message: envelope.error?.message ?? defaultMessage(response.status),
    fields: Array.isArray(rawFields) ? (rawFields as FieldError[]) : [],
    details,
  });
}

/**
 * What to say when the backend did not say anything.
 *
 * Written for a receptionist, not a developer: no status codes, and every
 * message names the next action rather than the failure.
 */
function defaultMessage(status: number): string {
  if (status === 401) return "Your session has ended. Please sign in again.";
  if (status === 403) return "You do not have permission to do this.";
  if (status === 404) return "That record no longer exists.";
  if (status === 409) return "Someone else changed this record. Reload and try again.";
  if (status === 422) return "Some of the details are not valid.";
  if (status === 429) return "Too many attempts. Wait a moment and try again.";
  if (status >= 500) return "The server is not responding. Please try again.";
  return "The request could not be completed.";
}

/** Narrowing helper — `catch` gives `unknown`. */
export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}
