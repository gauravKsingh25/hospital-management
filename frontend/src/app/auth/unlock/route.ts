import { NextResponse, type NextRequest } from "next/server";
import { getTranslations } from "next-intl/server";

import { ApiError } from "@/lib/api/error";
import { callApi } from "@/lib/api/fetch";
import { sameOrigin } from "@/lib/same-origin";
import { LOCK_COOKIE_NAME, sessionFromTokens, writeSession, type TokenPair } from "@/lib/session";

/**
 * Sign the same person back in from the lock screen, keeping their page.
 *
 * The email comes from the lock cookie, never the request: the lock screen is
 * for the person who walked away, and "Switch user" is the way to be someone
 * else. A route handler for the same reason as `/auth/lock`.
 *
 * Answers `{ unlocked }`, `{ error }` or `{ signedOut }` (the lock outlived its
 * cookie — start again at the login form). Wrong passwords count towards the
 * account lockout exactly as on the login form.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  if (!sameOrigin(request)) return new NextResponse(null, { status: 403 });
  const t = await getTranslations("auth");

  const email = request.cookies.get(LOCK_COOKIE_NAME)?.value;
  if (!email) return NextResponse.json({ signedOut: true });

  const payload = (await request.json().catch(() => null)) as { password?: unknown } | null;
  const password = typeof payload?.password === "string" ? payload.password : "";
  if (!password) return NextResponse.json({ error: t("passwordRequired") });

  let tokens: TokenPair;
  try {
    tokens = await callApi<TokenPair>("/auth/login", {
      method: "POST",
      body: { email, password },
    });
  } catch (error) {
    if (error instanceof ApiError) {
      return NextResponse.json({
        error: error.status === 401 || error.status === 403 ? t("invalidCredentials") : error.message,
      });
    }
    throw error;
  }

  const response = NextResponse.json({ unlocked: true });
  writeSession(response.cookies, sessionFromTokens(tokens));
  response.cookies.delete(LOCK_COOKIE_NAME);
  return response;
}
