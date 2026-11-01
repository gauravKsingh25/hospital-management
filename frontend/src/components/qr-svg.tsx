import qrcode from "qrcode-generator";

/**
 * Any string as a QR code, drawn as one SVG path.
 *
 * The encoding shared by the printed patient QR (`PatientQr`, a server
 * component) and the UPI QR on reception's payment dialog (lazy-loaded, so
 * the encoder reaches the browser only when someone opens it). Callers decide
 * what may be encoded; this only draws it.
 *
 * Quiet zone, crisp edges and an explicit white ground are what make a code
 * that reads on a monitor also read on a thermal slip or a phone camera — see
 * `PatientQr` for the longer story.
 */
export function QrSvg({
  value,
  size,
  label,
  className,
}: {
  value: string;
  /** Any CSS length. */
  size: string;
  /** Read aloud in place of "image". */
  label: string;
  className?: string;
}) {
  // Level M: ~15% recoverable, enough for a smudge, a fold or a glare spot.
  const qr = qrcode(0, "M");
  qr.addData(value);
  qr.make();

  const count = qr.getModuleCount();
  const margin = 4; // the quiet zone, in modules
  const extent = count + margin * 2;

  // One path for every dark module rather than a rect element each.
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
      aria-label={label}
    >
      <rect width={extent} height={extent} fill="#fff" />
      <path d={path} fill="#000" />
    </svg>
  );
}
