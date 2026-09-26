/**
 * The session cookie's name, shape and rules — with no runtime dependencies.
 *
 * Deliberately free of `server-only` and of `next/headers`, because `proxy.ts`
 * needs these and runs in the Edge runtime, where a `server-only` import can
 * resolve to the browser build and throw at request time. The parts that
 * actually touch cookies live in `session.ts`, which is server-only proper.
 *
 * Nothing here reads or writes anything. It is types and pure functions, so
 * importing it from the wrong place is harmless.
 */

export const SESSION_COOKIE_NAME = "hms_session";

/** Browsers drop cookies over ~4096 bytes silently — the worst failure mode. */
export const MAX_COOKIE_BYTES = 3800;

/**
 * Refresh this many seconds before the access token actually expires.
 *
 * Without a margin, a token that passes the check and then expires in flight
 * produces a 401 the user sees. Sixty seconds comfortably covers a slow
 * request plus clock skew between this server and the API's.
 */
export const REFRESH_MARGIN_SECONDS = 60;

export type Session = {
  accessToken: string;
  refreshToken: string;
  /** Epoch milliseconds. */
  expiresAt: number;
  /** The backend is telling us to route this user to a password change. */
  mustChangePassword: boolean;
};

/** The shape FastAPI's `/auth/login` and `/auth/refresh` return. */
export type TokenPair = {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  must_change_password: boolean;
};

export function sessionFromTokens(tokens: TokenPair): Session {
  return {
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    expiresAt: Date.now() + tokens.expires_in * 1000,
    mustChangePassword: tokens.must_change_password,
  };
}

/** True when the access token is expired, or close enough that it will be. */
export function isExpiring(session: Session): boolean {
  return session.expiresAt - REFRESH_MARGIN_SECONDS * 1000 <= Date.now();
}

export function serialiseSession(session: Session): string {
  const value = JSON.stringify(session);
  // `TextEncoder` rather than `Buffer`: this runs in the Edge runtime too.
  if (new TextEncoder().encode(value).length > MAX_COOKIE_BYTES) {
    // Loud, because the alternative is a browser silently discarding the
    // cookie and every user being bounced to the login screen forever with
    // no error anywhere to explain why.
    throw new Error(
      "Session cookie exceeds the browser size limit. The backend's JWT " +
        "claims have grown — move bulky claims out of the token.",
    );
  }
  return value;
}

export function parseSession(raw: string | undefined): Session | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof (parsed as Session).accessToken !== "string" ||
      typeof (parsed as Session).refreshToken !== "string" ||
      typeof (parsed as Session).expiresAt !== "number"
    ) {
      return null;
    }
    return parsed as Session;
  } catch {
    // A malformed cookie is treated as no session rather than an error: the
    // user gets the login screen, which is both recoverable and correct.
    return null;
  }
}
