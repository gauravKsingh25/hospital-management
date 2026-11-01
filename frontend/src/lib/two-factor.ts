import "server-only";

import { cookies } from "next/headers";

import { TWO_FACTOR_COOKIE_NAME } from "@/lib/session";

/** The sign-in waiting for its authenticator code (SEC-05). */
export type PendingChallenge = { token: string; enrol: boolean; next: string };

/** Only ever a path within this application — see `safeNext` in the auth actions. */
function safeNext(raw: unknown): string {
  if (typeof raw !== "string" || !raw) return "/";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  return raw;
}

/**
 * The challenge waiting in this browser, if it is still in date.
 *
 * Server-only rather than a server action: an exported action is callable
 * from any page, and the challenge token is not something a page needs.
 */
export async function readTwoFactorChallenge(): Promise<PendingChallenge | null> {
  const raw = (await cookies()).get(TWO_FACTOR_COOKIE_NAME)?.value;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<PendingChallenge>;
    if (typeof parsed.token !== "string" || typeof parsed.enrol !== "boolean") return null;
    return { token: parsed.token, enrol: parsed.enrol, next: safeNext(parsed.next) };
  } catch {
    return null;
  }
}
