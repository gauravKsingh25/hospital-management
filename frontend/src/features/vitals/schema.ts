import { z } from "zod";

/**
 * The vitals form's validation, mirroring the backend's `VitalsCreate`
 * (CLAUDE.md §11: Zod schemas mirror backend Pydantic).
 *
 * Bounds are copied field-for-field from
 * `backend/app/modules/clinical/schemas.py`. They are wide on purpose — these
 * are the limits of what a human body can register, not the normal range. A
 * temperature of 41 °C is alarming but real, and the backend stores it and
 * flags `is_abnormal` rather than refusing it. **Nothing here may be narrower
 * than the backend**, or the form would block a reading a nurse genuinely took.
 *
 * Values stay as strings all the way through, because that is what an `<input>`
 * gives and because "" has to keep meaning *not measured* rather than becoming
 * zero. `toVitalsPayload` does the conversion at the boundary, the same way
 * `toApiPayload` does for registration.
 */

/** Whole numbers only — no sign, no decimal point, no exponent. */
const WHOLE = /^\d+$/;
/** At most one decimal place, matching the backend's `decimal_places=1`. */
const ONE_DECIMAL = /^\d+(\.\d)?$/;
/** Weight is `Numeric(5,2)` in the database. */
const TWO_DECIMALS = /^\d+(\.\d{1,2})?$/;

function numeric(options: {
  pattern: RegExp;
  patternKey: string;
  min: number;
  max: number;
  exclusiveMin?: boolean;
  rangeKey: string;
}) {
  return z
    .string()
    .trim()
    .superRefine((value, ctx) => {
      // Empty is valid: a nurse records what they measured and nothing else.
      if (value === "") return;

      if (!options.pattern.test(value)) {
        ctx.addIssue({ code: "custom", message: options.patternKey });
        // Return, so a typo does not also produce a confusing range error.
        return;
      }

      const parsed = Number(value);
      const belowMin = options.exclusiveMin ? parsed <= options.min : parsed < options.min;
      if (belowMin || parsed > options.max) {
        ctx.addIssue({ code: "custom", message: options.rangeKey });
      }
    });
}

export const vitalsSchema = z
  .object({
    temperature_c: numeric({
      pattern: ONE_DECIMAL,
      patternKey: "oneDecimalOnly",
      min: 25,
      max: 45,
      rangeKey: "temperatureRange",
    }),
    pulse_bpm: numeric({
      pattern: WHOLE,
      patternKey: "wholeNumberOnly",
      min: 0,
      max: 400,
      rangeKey: "pulseRange",
    }),
    systolic_bp: numeric({
      pattern: WHOLE,
      patternKey: "wholeNumberOnly",
      min: 0,
      max: 400,
      rangeKey: "systolicRange",
    }),
    diastolic_bp: numeric({
      pattern: WHOLE,
      patternKey: "wholeNumberOnly",
      min: 0,
      max: 300,
      rangeKey: "diastolicRange",
    }),
    spo2_percent: numeric({
      pattern: WHOLE,
      patternKey: "wholeNumberOnly",
      min: 0,
      max: 100,
      rangeKey: "spo2Range",
    }),
    respiratory_rate: numeric({
      pattern: WHOLE,
      patternKey: "wholeNumberOnly",
      min: 0,
      max: 150,
      rangeKey: "respiratoryRateRange",
    }),
    weight_kg: numeric({
      pattern: TWO_DECIMALS,
      patternKey: "numberInvalid",
      min: 0,
      max: 700,
      exclusiveMin: true,
      rangeKey: "weightRange",
    }),
    // Whole numbers, and that is not an oversight to be fixed. A glucometer
    // reports mg/dL as an integer; a decimal reading (14.5) is almost always
    // mmol/L typed into the wrong unit, which is why the input is labelled.
    blood_glucose_mgdl: numeric({
      pattern: WHOLE,
      patternKey: "wholeNumberOnly",
      min: 0,
      max: 2000,
      rangeKey: "glucoseRange",
    }),
  })
  // Both halves of the backend's `_check_bp`, so the nurse hears about a
  // transposed cuff reading before the round trip rather than after it.
  .superRefine((values, ctx) => {
    const systolic = values.systolic_bp.trim();
    const diastolic = values.diastolic_bp.trim();

    if (systolic === "" && diastolic === "") return;

    if (systolic === "" || diastolic === "") {
      ctx.addIssue({
        code: "custom",
        message: "bloodPressurePair",
        path: [systolic === "" ? "systolic_bp" : "diastolic_bp"],
      });
      return;
    }

    // Only meaningful once both parsed cleanly; the per-field rules above
    // already reported anything that did not.
    if (!WHOLE.test(systolic) || !WHOLE.test(diastolic)) return;

    if (Number(diastolic) >= Number(systolic)) {
      ctx.addIssue({
        code: "custom",
        message: "diastolicBelowSystolic",
        path: ["diastolic_bp"],
      });
    }
  });

export type VitalsForm = z.infer<typeof vitalsSchema>;

export const emptyVitals: VitalsForm = {
  temperature_c: "",
  pulse_bpm: "",
  systolic_bp: "",
  diastolic_bp: "",
  spo2_percent: "",
  respiratory_rate: "",
  weight_kg: "",
  blood_glucose_mgdl: "",
};

/** The field names this form renders, for binding backend errors back to it. */
export const vitalsFieldNames = Object.keys(emptyVitals) as (keyof VitalsForm)[];

/** True when the nurse has entered at least one reading. */
export function hasAnyVital(values: VitalsForm): boolean {
  return Object.values(values).some((value) => value.trim() !== "");
}

/** Drop the blanks and send numbers, so "" is omitted rather than sent as 0. */
export function toVitalsPayload(values: VitalsForm): Record<string, number> {
  const payload: Record<string, number> = {};
  for (const [key, value] of Object.entries(values)) {
    const trimmed = value.trim();
    if (trimmed !== "") payload[key] = Number(trimmed);
  }
  return payload;
}
