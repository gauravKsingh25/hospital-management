/**
 * A QR decoder in JavaScript, for the browsers that do not have one built in.
 *
 * Safari has no `BarcodeDetector`, and every browser on an iPhone or an iPad
 * is Safari underneath — so without this, the devices most likely to be in a
 * nurse's hand could not scan at all. Firefox and Windows desktop Chrome are
 * in the same position.
 *
 * ## Why this file is only ever loaded on demand
 *
 * `jsqr` is ~45 kB gzipped. `camera-scan.ts` reaches it through a dynamic
 * `import()` on the first tap of the scanner, and only on a device without a
 * native decoder — so no page carries it, and an Android phone never fetches
 * it at all (CLAUDE.md §14). Nothing else may import this module statically,
 * or the decoder lands in every bundle that does.
 *
 * ## Why `jsqr`
 *
 * Plain JavaScript, no dependencies, no install scripts, and it makes no
 * network requests. The better-known `barcode-detector` polyfill fetches a
 * ~1 MB WebAssembly build from a public CDN at runtime; allowing that would
 * mean opening `connect-src` and allowing WebAssembly compilation in a CSP
 * whose `connect-src 'self'` is what stops a compromised dependency posting
 * patient data elsewhere. Pinned to an exact version in `package.json`.
 */

import jsQR from "jsqr";

import type { BarcodeDetectorLike } from "@/lib/camera-scan";

/**
 * The longest edge, in pixels, of the frame handed to the decoder.
 *
 * `jsqr` runs on the main thread, and a 1080p frame is two million pixels —
 * five of those a second stutters the preview on an older iPad. A card held up
 * to the camera fills much of the frame, so 640 pixels across reads it easily.
 */
const MAX_EDGE = 640;

/** The size to decode a `width` × `height` frame at, or `null` if it is empty. */
export function scaledFrame(
  width: number,
  height: number,
): { width: number; height: number } | null {
  if (width <= 0 || height <= 0) return null;

  // Never scaled up: that adds pixels and no information.
  const scale = Math.min(1, MAX_EDGE / Math.max(width, height));
  return { width: Math.round(width * scale), height: Math.round(height * scale) };
}

/**
 * The text in the QR code found in an RGBA pixel buffer, or `null`.
 *
 * `dontInvert`: our codes are printed dark on light, and also trying the
 * inverted image would double the work on every frame to find codes we never
 * print. An empty payload is reported as nothing found — it is not a card.
 */
export function decodeQrPixels(
  data: Uint8ClampedArray,
  width: number,
  height: number,
): string | null {
  const found = jsQR(data, width, height, { inversionAttempts: "dontInvert" });
  return found === null || found.data === "" ? null : found.data;
}

/**
 * A `BarcodeDetector` look-alike over a video element.
 *
 * Same shape as the native one, so the camera component runs a single frame
 * loop whichever decoder a device ended up with. One canvas is kept and
 * reused: allocating one per frame would hand the garbage collector a fresh
 * megabyte five times a second.
 */
export function createFrameDecoder(): BarcodeDetectorLike {
  let canvas: HTMLCanvasElement | null = null;
  let context: CanvasRenderingContext2D | null = null;

  return {
    async detect(source) {
      if (!(source instanceof HTMLVideoElement)) return [];

      const size = scaledFrame(source.videoWidth, source.videoHeight);
      if (size === null) return [];

      if (canvas === null) {
        canvas = document.createElement("canvas");
        // Read back every frame, so ask for a CPU-side canvas up front rather
        // than have the browser copy it off the GPU each time.
        context = canvas.getContext("2d", { willReadFrequently: true });
      }
      if (context === null) return [];

      if (canvas.width !== size.width || canvas.height !== size.height) {
        canvas.width = size.width;
        canvas.height = size.height;
      }

      context.drawImage(source, 0, 0, size.width, size.height);
      const { data } = context.getImageData(0, 0, size.width, size.height);
      const value = decodeQrPixels(data, size.width, size.height);
      return value === null ? [] : [{ rawValue: value }];
    },
  };
}
