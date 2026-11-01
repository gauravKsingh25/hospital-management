/**
 * Reading a patient card with the device camera.
 *
 * The second of the two scan paths, and the optional one. A counter has a USB
 * scanner; a ward round has a phone or a tablet, and this is for those. It is
 * built on the browser's own **Barcode Detection API** — `BarcodeDetector` —
 * where the browser has one, and on a small downloaded decoder where it does
 * not.
 *
 * ## Two decoders, one of them downloaded on demand
 *
 * `BarcodeDetector` is native, fast and free, and it is missing from exactly
 * the devices this is for: Safari has none, and every browser on an iPhone or
 * an iPad is Safari underneath. Firefox and Windows desktop Chrome lack it
 * too. Built on the native API alone, the scanner hid itself on every iPad on
 * the ward.
 *
 * So those devices fetch `jsqr` (see `qr-decode.ts`) the first time somebody
 * taps the scanner — never before, and never on a device with a native
 * decoder. No page carries it in its bundle (CLAUDE.md §14).
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

export type BarcodeDetectorConstructor = (new (options?: {
  formats?: string[];
}) => BarcodeDetectorLike) & {
  getSupportedFormats?: () => Promise<string[]>;
};

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
  navigator?: { mediaDevices?: { getUserMedia?: unknown } };
};

/**
 * Whether this browser can scan with a camera at all.
 *
 * Only the camera decides it — a decoder can always be downloaded. Two
 * conditions, and both genuinely occur:
 *
 * - **The page is a secure context.** `getUserMedia` does not exist on plain
 *   HTTP, so without TLS this path cannot exist. `localhost` counts as secure,
 *   so development on the same machine works; a phone on the office wifi
 *   needs `npm run dev:https`.
 * - **`getUserMedia` exists.** The same condition from the other direction,
 *   and cheap insurance against an embedded webview that omits it.
 *
 * Whether there is a camera to open is a separate, asynchronous question —
 * see `hasCamera`.
 */
export function cameraScanSupport(scope: ScanCapableWindow | undefined): boolean {
  if (scope === undefined) return false;
  if (scope.isSecureContext !== true) return false;
  return typeof scope.navigator?.mediaDevices?.getUserMedia === "function";
}

/**
 * The browser's own QR detector, or `null` if it has none that can be made.
 *
 * The constructor throws on a format the platform cannot decode, so a browser
 * can have the API and still not do QR. Narrowing to `qr_code` is also a speed
 * choice: asking for every format makes the detector try each one per frame.
 */
export function nativeQrDetector(scope: ScanCapableWindow | undefined): BarcodeDetectorLike | null {
  if (!cameraScanSupport(scope)) return null;
  const Detector = scope?.BarcodeDetector;
  if (typeof Detector !== "function") return null;
  try {
    return new Detector({ formats: ["qr_code"] });
  } catch {
    return null;
  }
}

/** Whether the platform says it can decode QR. Silence is taken as a yes. */
async function nativeReadsQr(scope: ScanCapableWindow | undefined): Promise<boolean> {
  const Detector = scope?.BarcodeDetector;
  if (typeof Detector?.getSupportedFormats !== "function") return true;
  try {
    return (await Detector.getSupportedFormats()).includes("qr_code");
  } catch {
    return false;
  }
}

/** Fetches the JavaScript decoder — its own chunk, loaded only from here. */
const downloadDecoder = (): Promise<BarcodeDetectorLike> =>
  import("@/lib/qr-decode").then((module) => module.createFrameDecoder());

/**
 * The QR detector this device should use, or `null` if it cannot scan.
 *
 * Native where it truly works; otherwise the downloaded one. "Truly" is the
 * point of `nativeReadsQr`: desktop Chrome on Linux has the class, accepts
 * `qr_code` without complaint and then finds nothing in any frame, and the
 * only honest signal is the list of supported formats.
 *
 * `loadFallback` is a parameter so tests can count the downloads; the app
 * never passes it.
 */
export async function loadQrDetector(
  scope: ScanCapableWindow | undefined,
  loadFallback: () => Promise<BarcodeDetectorLike> = downloadDecoder,
): Promise<BarcodeDetectorLike | null> {
  if (!cameraScanSupport(scope)) return null;

  const native = nativeQrDetector(scope);
  if (native !== null && (await nativeReadsQr(scope))) return native;

  return loadFallback();
}

/**
 * Whether `enumerateDevices` lists a camera.
 *
 * Before permission is granted, browsers list devices without names or ids —
 * but they do list them, so this answers without prompting anybody. It is
 * what keeps the scanner icon off a counter PC with no webcam: a button that
 * opens only to explain that it cannot work teaches staff to ignore buttons.
 */
export function hasCamera(devices: readonly { kind: string }[]): boolean {
  return devices.some((device) => device.kind === "videoinput");
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
