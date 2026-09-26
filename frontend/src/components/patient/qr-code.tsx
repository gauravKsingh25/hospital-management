import qrcode from "qrcode-generator";

/**
 * A QR code, rendered to SVG on the server.
 *
 * Used only on printed artifacts — the token slip and the patient card — so
 * there is nothing interactive here and no reason for the browser to carry an
 * encoder. This is a server component; `qrcode-generator` never reaches the
 * client bundle.
 *
 * ## What is inside it
 *
 * The UHID, as plain text: `DEMO-26-000042`. Exactly the string printed in ink
 * beside it, which is what makes the choice safe — the QR adds no exposure a
 * lost card did not already have. It is not a credential: scanning yields a
 * search term, and every route behind it still demands a JWT and is scoped by
 * row-level security to the caller's own hospital.
 *
 * The payload being *only* the UHID is also what makes the common case free.
 * A counter barcode scanner is a keyboard: it types the payload into whatever
 * has focus and presses Enter. So a scan into the existing search box works
 * with no client code at all.
 *
 * ## The three things that decide whether it actually scans
 *
 * Getting these wrong produces a QR that reads perfectly on a monitor and
 * fails on a thermal slip, which is the usual way this feature disappoints.
 *
 * 1. **Quiet zone.** Four modules of blank margin, part of the spec and the
 *    most common omission. Without it a scanner cannot find the code's edge.
 * 2. **`shape-rendering="crispEdges"`.** Antialiasing blurs module boundaries
 *    at small sizes; on a 203 dpi thermal head that blur is the difference
 *    between a clean read and a retry.
 * 3. **Physical size.** Default 24 mm square, above the 20 mm floor a
 *    version-1 code needs at thermal resolution. Set in millimetres rather
 *    than pixels because the only size that matters here is the printed one.
 *
 * Error correction level M: ~15% recoverable, which covers a smudge or a fold
 * without pushing the code to a larger version that prints denser.
 */

/** `CODE-YY-NNNNNN`, the shape `allocate_uhid` produces. */
const UHID_PATTERN = /^[A-Z0-9]{2,12}-\d{2}-\d{6}$/;

export function PatientQr({
  uhid,
  size = "24mm",
  className,
}: {
  uhid: string;
  /** Any CSS length. Millimetres, because this is printed. */
  size?: string;
  className?: string;
}) {
  const value = uhid.trim().toUpperCase();

  // Refuse anything that is not one of our identifiers. The value comes from
  // our own database today, so this is not input validation so much as a
  // guarantee about what can ever be encoded: nobody can later point this at a
  // free-text field and print a QR containing something else.
  if (!UHID_PATTERN.test(value)) return null;

  const qr = qrcode(0, "M");
  qr.addData(value);
  qr.make();

  const count = qr.getModuleCount();
  const margin = 4; // the quiet zone, in modules
  const extent = count + margin * 2;

  // One path for every dark module rather than a rect element each: ~200
  // elements become one attribute, which keeps the printed document small and
  // renders identically.
  let path = "";
  for (let row = 0; row < count; row += 1) {
    for (let column = 0; column < count; column += 1) {
      if (qr.isDark(row, column)) {
        path += `M${column + margin} ${row + margin}h1v1h-1z`;
      }
    }
  }

  return (
    <svg
      viewBox={`0 0 ${extent} ${extent}`}
      width={size}
      height={size}
      shapeRendering="crispEdges"
      className={className}
      role="img"
      // Read aloud as the identifier it encodes, not as "image". A screen
      // reader user holding this slip should learn their own UHID from it.
      aria-label={value}
    >
      {/* White explicitly, not transparent: a scanner needs the contrast, and
          a transparent code on coloured paper is a code that does not read. */}
      <rect width={extent} height={extent} fill="#fff" />
      <path d={path} fill="#000" />
    </svg>
  );
}
