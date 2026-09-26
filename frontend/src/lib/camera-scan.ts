/**
 * Reading a patient card with the device camera.
 *
 * The second of the two scan paths, and the optional one. A counter has a USB
 * scanner; a ward round has a phone or a tablet, and this is for those. It is
 * built on the browser's own **Barcode Detection API** — `BarcodeDetector` —
 * so it ships no decoder of its own.
 *
 * ## Why no library
 *
 * The usual choice here is a JavaScript QR decoder, and it costs 40–90 kB of
 * WebAssembly or minified JS on every page it is imported into. CLAUDE.md §14
 * says to question a dependency that bloats the bundle, and this one earns
 * nothing the platform does not already do: `BarcodeDetector` is a native
 * decoder, already present, already fast, and free.
 *
 * The price is that it is not everywhere — see `cameraScanSupport`. That is
 * an acceptable trade because this path is a convenience. The counter scanner
 * and typing the UHID both work regardless, so a browser without the API
 * loses a shortcut, not a capability.
 *
 * Everything decidable without a camera lives here as a pure function, so the
 * parts that can be tested are tested and the component is left holding only
 * the parts that cannot be.
 */

import { normaliseUhid } from "@/lib/scanner";

/** The one field of a detected barcode this feature reads. */
export type DetectedBarcode = { rawValue: string };

/** The subset of `BarcodeDetector` used here, so nothing depends on the full spec. */
export type BarcodeDetectorLike = {
  detect(source: CanvasImageSource): Promise<DetectedBarcode[]>;
};

export type BarcodeDetectorConstructor = new (options?: {
  formats?: string[];
}) => BarcodeDetectorLike;

/**
 * What this module needs from `window`.
 *
 * Declared as a parameter rather than reached for globally so the rules below
 * can be tested against a plain object, with no browser and no monkey-patching
 * of real globals. `BarcodeDetector` is also not in TypeScript's DOM library
 * yet, so it has to be described somewhere either way.
 */
export type ScanCapableWindow = {
  BarcodeDetector?: BarcodeDetectorConstructor;
  isSecureContext?: boolean;
  navigator?: { mediaDevices?: unknown };
};

/**
 * Whether this browser can scan with a camera at all.
 *
 * Three conditions, and all three genuinely occur:
 *
 * - **`BarcodeDetector` exists.** Chrome and Edge have it on Android, ChromeOS
 *   and macOS; on Windows desktop and in Firefox they do not. A counter PC is
 *   the most likely machine in this system and the least likely to support it,
 *   which is exactly why the button hides rather than fails.
 * - **The page is a secure context.** `getUserMedia` is null on plain HTTP, so
 *   without TLS this path cannot exist. `localhost` counts as secure, so
 *   development works.
 * - **`mediaDevices` exists.** The same condition from the other direction,
 *   and cheap insurance against an embedded webview that omits it.
 *
 * A false here renders nothing. A button that opens a dialog to explain it
 * cannot work is worse than no button, because staff learn to ignore it.
 */
export function cameraScanSupport(scope: ScanCapableWindow | undefined): boolean {
  if (scope === undefined) return false;
  if (typeof scope.BarcodeDetector !== "function") return false;
  if (scope.isSecureContext !== true) return false;
  return scope.navigator?.mediaDevices != null;
}

/**
 * A detector restricted to QR codes, or `null` if one cannot be made.
 *
 * The constructor throws on a format the platform cannot decode, so the
 * feature check above is necessary but not sufficient — a browser can have
 * the API and still not do QR. Narrowing to `qr_code` is also a speed choice:
 * asking for every format makes the detector try each one on every frame.
 */
export function newQrDetector(scope: ScanCapableWindow | undefined): BarcodeDetectorLike | null {
  if (!cameraScanSupport(scope)) return null;
  try {
    return new (scope as Required<ScanCapableWindow>).BarcodeDetector({ formats: ["qr_code"] });
  } catch {
    return null;
  }
}

/**
 * The first detected code that is one of our UHIDs, or `null`.
 *
 * A frame can hold several codes — a card lying on a desk next to a lab
 * requisition, a poster on the wall behind the patient — so this filters
 * rather than taking `[0]` and hoping. Anything that is not shaped like a
 * UHID is ignored in silence and the loop keeps looking, which is the right
 * behaviour when the camera is pointed at the wrong thing.
 */
export function firstUhid(barcodes: readonly DetectedBarcode[]): string | null {
  for (const barcode of barcodes) {
    const uhid = normaliseUhid(barcode.rawValue);
    if (uhid !== null) return uhid;
  }
  return null;
}

/** Why the camera did not start — each maps to a sentence staff can act on. */
export type CameraFailure = "denied" | "noCamera" | "failed";

/**
 * A `getUserMedia` rejection as one of three outcomes.
 *
 * The distinction is worth making because the remedies differ and only one of
 * them is the user's to apply: a denied permission is fixed in the address
 * bar, a missing camera is not fixable at all. Telling somebody to check
 * their permissions on a desktop with no webcam wastes their morning.
 *
 * Matched on `name` rather than `instanceof DOMException`: the message is
 * localised by the browser and the class is not available in every runtime,
 * while the names are fixed by the Media Capture spec.
 */
export function cameraFailureReason(error: unknown): CameraFailure {
  const name = (error as Partial<Error> | null)?.name;
  if (name === "NotAllowedError" || name === "SecurityError") return "denied";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "noCamera";
  return "failed";
}
