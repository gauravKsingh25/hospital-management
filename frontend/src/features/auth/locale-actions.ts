"use server";

import { cookies } from "next/headers";

import { isLocale, LOCALE_COOKIE, type Locale } from "@/i18n/config";

/**
 * Persist the staff member's language choice.
 *
 * Its own file rather than living beside sign-in, because `actions.ts`
 * carries the credential-handling code and this does not need to be anywhere
 * near it.
 *
 * A year is deliberate: language is a fact about a person, not a session. The
 * cookie is not httpOnly — there is nothing sensitive in "hi", and leaving it
 * readable lets a client component show the current choice without a round
 * trip.
 */
export async function setLocale(locale: Locale): Promise<void> {
  // Validated even though the caller is our own component: a server action is
  // a public HTTP endpoint, and anyone can post anything to it.
  if (!isLocale(locale)) return;

  (await cookies()).set({
    name: LOCALE_COOKIE,
    value: locale,
    httpOnly: false,
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
  });
}
