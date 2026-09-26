/**
 * Binding the backend's field-level errors onto the inputs that caused them.
 *
 * The backend already does the hard half of this. `core/exceptions.py` flattens
 * Pydantic's `loc`/`msg` pairs into `details.fields`, and `apiErrorFromResponse`
 * parses them into `ApiError.fields`. What was missing was the last step: most
 * screens collapsed the whole thing to `toast.error(error.message)`, which for a
 * request-schema rejection reads "The submitted data is not valid." and leaves
 * the user to guess which of eight inputs is wrong.
 *
 * Both helpers deliberately report whether they actually placed anything, so the
 * caller can fall back to a toast. A 422 naming a field the form does not render
 * is not hypothetical — the backend validates fields this dialog never shows
 * (`height_cm`, `pain_score`, `notes`) — and silently dropping it would leave the
 * user staring at a form with no error at all, which is worse than the toast.
 */

import { isApiError, type FieldError } from "@/lib/api/error";

/** Pull the field errors out of an unknown thrown value, if it carries any. */
function fieldsOf(error: unknown): FieldError[] {
  return isApiError(error) ? error.fields : [];
}

/**
 * Attach server-side errors to react-hook-form inputs.
 *
 * `known` is the list of field names the form actually renders, and it is what
 * gives `Name` its type: only a name drawn from that list is ever passed to
 * `setError`, so the unchecked `as keyof FormValues` cast this replaces cannot
 * come back. Anything the backend names that is *not* in the list is left for
 * the caller to surface.
 *
 * Typed structurally rather than against `UseFormSetError` so this module stays
 * free of a react-hook-form import.
 *
 * @returns true when every reported field was placed on an input.
 */
export function applyFieldErrors<Name extends string>(
  error: unknown,
  setError: (field: Name, error: { message: string }) => void,
  known: readonly Name[],
): boolean {
  const fields = fieldsOf(error);
  if (fields.length === 0) return false;

  let placedAll = true;
  for (const field of fields) {
    const match = known.find((name) => name === field.field);
    if (match === undefined) {
      placedAll = false;
      continue;
    }
    setError(match, { message: field.message });
  }
  return placedAll;
}

/**
 * The same thing for dialogs that hold their draft in `useState` and have no
 * react-hook-form instance to call `setError` on.
 *
 * @returns a map of field name to message; empty when the error carried none.
 */
export function toFieldErrorMap(error: unknown): Record<string, string> {
  const map: Record<string, string> = {};
  for (const field of fieldsOf(error)) {
    // First one wins: Pydantic can report several problems for one field, and
    // the first is the one closest to what the user typed.
    map[field.field] ??= field.message;
  }
  return map;
}
