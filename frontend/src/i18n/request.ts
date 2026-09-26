import { cookies, headers } from "next/headers";
import { getRequestConfig } from "next-intl/server";

import { DEFAULT_LOCALE, isLocale, LOCALE_COOKIE, LOCALES, type Locale } from "@/i18n/config";

/**
 * Resolve the locale for this request and load its messages.
 *
 * Order of preference: the staff member's explicit choice (cookie), then
 * what their browser asks for, then English. The browser step matters more
 * than it looks — a hospital that images its counter machines with a Hindi
 * Windows install gets Hindi without anyone configuring anything.
 */
async function resolveLocale(): Promise<Locale> {
  const chosen = (await cookies()).get(LOCALE_COOKIE)?.value;
  if (isLocale(chosen)) return chosen;

  const header = (await headers()).get("accept-language");
  if (header) {
    for (const part of header.split(",")) {
      // "hi-IN;q=0.9" -> "hi". Regional variants share a translation file
      // until there is a reason for them not to.
      const tag = part.split(";")[0]?.trim().split("-")[0]?.toLowerCase();
      if (isLocale(tag)) return tag;
    }
  }

  return DEFAULT_LOCALE;
}

export default getRequestConfig(async () => {
  const locale = await resolveLocale();

  return {
    locale,
    // English is loaded alongside any other locale and used as the fallback,
    // so an untranslated key renders the English string rather than the key
    // itself. A screen reading `consultation.completeVisit` is worse than one
    // reading "Complete visit" in the wrong language.
    messages: {
      ...(await import("@/i18n/messages/en.json")).default,
      ...(locale === DEFAULT_LOCALE ? {} : (await import(`@/i18n/messages/${locale}.json`)).default),
    },
    // Indian formats throughout: dd/mm/yyyy dates and the lakh/crore digit
    // grouping that ₹ amounts are read in.
    timeZone: "Asia/Kolkata",
    formats: {
      dateTime: {
        short: { day: "2-digit", month: "short", year: "numeric" },
        time: { hour: "2-digit", minute: "2-digit", hour12: true },
      },
      number: {
        currency: { style: "currency", currency: "INR", maximumFractionDigits: 2 },
      },
    },
    onError(error) {
      // A missing translation is not worth failing a render over, but it must
      // be visible to whoever is filling in the file.
      if (process.env.NODE_ENV === "development") console.warn(error.message);
    },
  };
});

export { LOCALES };
