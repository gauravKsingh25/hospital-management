import { z } from "zod";

/**
 * The registration form's validation, mirroring the backend's
 * `PatientRegister` (CLAUDE.md §11: Zod schemas mirror backend Pydantic).
 *
 * Client-side validation exists to give an instant answer, not to be the
 * authority — FastAPI validates the same rules again and its verdict wins.
 * What this buys is a receptionist finding out about a nine-digit mobile
 * number while the patient is still standing there, rather than after a round
 * trip.
 *
 * Exactly four required fields (§7b). Everything else is optional and can be
 * filled in later by whoever has time; nothing else may gate the queue.
 */

/**
 * Indian mobile numbers: ten digits starting 6–9, optionally with a +91 or 0
 * prefix that people type out of habit.
 *
 * Deliberately permissive about spaces, hyphens and brackets — a number
 * copied off a referral slip arrives as "+91 98765-43210", and rejecting that
 * teaches staff to fight the form instead of using it. The backend
 * normalises; this only checks there is a plausible number in there.
 */
const MOBILE = /^(?:\+?91[-\s]?|0)?[6-9]\d{9}$/;

export function normalisePhone(raw: string): string {
  return raw.replace(/[^\d+]/g, "");
}

export const genderValues = ["MALE", "FEMALE", "OTHER"] as const;

export const registrationSchema = z.object({
  full_name: z
    .string()
    .trim()
    .min(1, "nameRequired")
    // Two names is not a rule anyone can enforce here: plenty of patients
    // genuinely have one, and a form that refuses them is a form that gets a
    // fake surname typed into it.
    .max(200),

  phone: z
    .string()
    .trim()
    .min(1, "phoneRequired")
    .transform(normalisePhone)
    .refine((value) => MOBILE.test(value), "phoneInvalid"),

  gender: z.enum(genderValues, "genderRequired"),

  // Age rather than date of birth, because most patients know roughly how old
  // they are and not the date. Demanding a date stalls the counter, and what
  // gets typed instead is 01/01/1980.
  age_years: z.coerce
    .number("ageRequired")
    .int("ageInvalid")
    .min(0, "ageInvalid")
    .max(130, "ageInvalid"),

  // --- Everything below is optional and revealed on demand ---------------
  guardian_name: z.string().trim().max(200).optional().or(z.literal("")),
  guardian_relation: z.string().trim().max(50).optional().or(z.literal("")),
  guardian_phone: z.string().trim().max(20).optional().or(z.literal("")),
  address_line1: z.string().trim().max(200).optional().or(z.literal("")),
  city: z.string().trim().max(100).optional().or(z.literal("")),
  state: z.string().trim().max(100).optional().or(z.literal("")),
  pincode: z.string().trim().max(10).optional().or(z.literal("")),
  blood_group: z.string().optional(),

  // Set only after reception has read the duplicate warnings and decided this
  // really is a different person — a father and son sharing a name and a
  // phone. Never a default (see the backend's `PatientRegister`).
  confirm_not_duplicate: z.boolean().default(false),
});

export type RegistrationForm = z.input<typeof registrationSchema>;
export type RegistrationPayload = z.output<typeof registrationSchema>;

/** The field names this form renders, for binding backend errors back to it. */
export const registrationFieldNames = Object.keys(
  registrationSchema.shape,
) as (keyof RegistrationForm)[];

/** Drop empty optional strings so they are omitted rather than sent as "". */
export function toApiPayload(values: RegistrationPayload): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(values)) {
    if (value === "" || value === undefined || value === null) continue;
    payload[key] = value;
  }
  return payload;
}
