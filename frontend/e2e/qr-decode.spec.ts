import { expect, test } from "@playwright/test";
import qrcode from "qrcode-generator";

import { decodeQrPixels, scaledFrame } from "@/lib/qr-decode";

/**
 * The downloaded QR decoder — what an iPad or iPhone uses in place of the
 * `BarcodeDetector` Safari does not have.
 *
 * Unit tests, no browser, like `camera-scan-support.spec.ts`. The camera is
 * the one part that cannot run here; the decoding can, and the test that
 * matters most is the round trip below: a QR made exactly the way `QrSvg`
 * prints one, read back by exactly the code the camera path runs.
 */

/**
 * A QR as the pixels a camera frame would hold: RGBA, white quiet zone,
 * black modules, `scale` pixels per module.
 *
 * Built with the same encoder and the same settings as `QrSvg` — level M and
 * a four-module quiet zone — so this is our card, not a QR in general.
 */
function renderCard(value: string, scale = 4): { data: Uint8ClampedArray; width: number } {
  const qr = qrcode(0, "M");
  qr.addData(value);
  qr.make();

  const margin = 4;
  const modules = qr.getModuleCount() + margin * 2;
  const width = modules * scale;
  const data = new Uint8ClampedArray(width * width * 4).fill(255);

  for (let y = 0; y < width; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const row = Math.floor(y / scale) - margin;
      const column = Math.floor(x / scale) - margin;
      const inCode = row >= 0 && column >= 0 && row < modules - margin * 2 && column < modules - margin * 2;
      if (inCode && qr.isDark(row, column)) {
        const offset = (y * width + x) * 4;
        data[offset] = 0;
        data[offset + 1] = 0;
        data[offset + 2] = 0;
      }
    }
  }
  return { data, width };
}

test.describe("reading a card's pixels", () => {
  test("a printed card's code reads back as its UHID", () => {
    const { data, width } = renderCard("DEMO-26-000042");
    expect(decodeQrPixels(data, width, width)).toBe("DEMO-26-000042");
  });

  test("a frame with no code in it reads as nothing", () => {
    // Most frames, while the card is still on its way into view.
    const blank = new Uint8ClampedArray(200 * 200 * 4).fill(255);
    expect(decodeQrPixels(blank, 200, 200)).toBeNull();
  });
});

test.describe("how much of a frame is decoded", () => {
  test("a large camera frame is scaled down, keeping its shape", () => {
    /**
     * A 1080p frame is two million pixels, and decoding that on the main
     * thread five times a second would stutter the preview on an older iPad.
     * A card held up to the camera fills a good part of the frame, so 640
     * pixels across is ample to read it.
     */
    expect(scaledFrame(1920, 1080)).toEqual({ width: 640, height: 360 });
    expect(scaledFrame(1080, 1920)).toEqual({ width: 360, height: 640 });
  });

  test("a small frame is left as it is", () => {
    // Scaling up adds pixels and no information.
    expect(scaledFrame(480, 360)).toEqual({ width: 480, height: 360 });
  });

  test("a video with no frame yet has nothing to decode", () => {
    // `videoWidth` is 0 until the first frame arrives.
    expect(scaledFrame(0, 0)).toBeNull();
    expect(scaledFrame(640, 0)).toBeNull();
  });
});
