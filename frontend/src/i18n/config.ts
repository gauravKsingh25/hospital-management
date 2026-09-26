/**
 * Locales, and how one is chosen.
 *
 * CLAUDE.md §9 requires multilingual UI from the start. The expensive part of
 * i18n is not the plumbing — it is going back through 4,000 lines of JSX
 * later to extract every hard-coded string. So every string goes through
 * `t()` from the first screen, even while Hindi is a partial translation.
 *
 * ## Why there is no `/en/…` or `/hi/…` in the URL
 *
 * Locale-prefixed routing is the usual next-intl setup and it is the wrong
 * shape here. It suits public sites where a URL must be shareable in a given
 * language and indexable by search engines. This is an internal system behind
 * a login: nothing is indexed, nothing is shared publicly, and language is a
 * property of the *member of staff*, not of the page. A nurse who reads Hindi
 * wants Hindi everywhere, not a different URL for every screen — and prefixed
 * routing would double every route and break every hard-coded link the moment
 * somebody switches language.
 *
 * So the locale comes from a cookie, defaulting to the browser's preference.
 * Once the `identity` module carries a staff language preference, that
 * becomes the source and this cookie becomes the override.
 */

export const LOCALES = ["en", "hi"] as const;

export type Locale = (typeof LOCALES)[number];

/**
 * English by default.
 *
 * Not a statement about users: clinical vocabulary, drug names and lab
 * analytes are written in English on the packaging, the request form and the
 * report throughout Indian private hospitals, and staff type them that way.
 * Hindi covers the interface around that vocabulary — labels, buttons,
 * instructions, anything shown to a patient.
 */
export const DEFAULT_LOCALE: Locale = "en";

export const LOCALE_COOKIE = "hms_locale";

export const LOCALE_LABELS: Record<Locale, string> = {
  en: "English",
  hi: "हिन्दी",
};

export function isLocale(value: string | undefined): value is Locale {
  return value !== undefined && (LOCALES as readonly string[]).includes(value);
}
