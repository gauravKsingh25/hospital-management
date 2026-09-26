/**
 * The inline error shown under a single input.
 *
 * Field errors are stored as message *keys* by the Zod schemas, so the same
 * schema can be validated on a server that has no React context. Anything that
 * is not a known key is a message the backend sent, and is shown as-is.
 *
 * Shared rather than per-form because the heuristic below is the interesting
 * part, and two copies of it would drift.
 */
export function FieldError({
  message,
  translate,
  id,
}: {
  message?: string;
  translate: (key: string) => string;
  id?: string;
}) {
  if (!message) return null;
  const known = /^[a-z][A-Za-z0-9]+$/.test(message);
  return (
    <p id={id} role="alert" className="text-destructive text-sm">
      {known ? translate(message) : message}
    </p>
  );
}
