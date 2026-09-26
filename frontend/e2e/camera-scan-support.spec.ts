import { expect, test } from "@playwright/test";

import {
  cameraFailureReason,
  cameraScanSupport,
  firstUhid,
  newQrDetector,
  type ScanCapableWindow,
} from "@/lib/camera-scan";
import { normaliseUhid } from "@/lib/scanner";

/**
 * The camera scan path's decidable parts, in isolation.
 *
 * Unit tests — none of these requests the `page` fixture, so no browser
 * starts. Same reasoning as `scanner-heuristic.spec.ts`: a second test runner
 * and its config for one pure module is a dependency that has to earn itself
 * (CLAUDE.md §14), and this does not.
 *
 * What can be tested here is everything except the camera and the decoder:
 * whether this browser can scan at all, which detected code is a card, and
 * what to tell somebody when the camera will not start. Those three are the
 * whole of the logic; the rest is the platform's.
 */

/** A `window` that can scan. Each test breaks exactly one condition. */
const CAPABLE: ScanCapableWindow = {
  BarcodeDetector: class {
    async detect() {
      return [];
    }
  },
  isSecureContext: true,
  navigator: { mediaDevices: {} },
};

test.describe("whether this browser can scan", () => {
  test("a capable browser can", () => {
    expect(cameraScanSupport(CAPABLE)).toBe(true);
  });

  test("no window at all cannot", () => {
    // The server render. The button must not appear before hydration says so.
    expect(cameraScanSupport(undefined)).toBe(false);
  });

  test("without BarcodeDetector it cannot", () => {
    /**
     * The common case, not the exotic one. Windows desktop Chrome and every
     * version of Firefox lack the API — which is to say the machine most
     * likely to be at a reception counter. The button hides; the USB scanner
     * and typing are untouched.
     */
    expect(cameraScanSupport({ ...CAPABLE, BarcodeDetector: undefined })).toBe(false);
  });

  test("over plain HTTP it cannot", () => {
    // `getUserMedia` does not exist without TLS, so this is not a policy
    // choice — there is no camera to ask for. The pilot site needs HTTPS.
    expect(cameraScanSupport({ ...CAPABLE, isSecureContext: false })).toBe(false);
  });

  test("without mediaDevices it cannot", () => {
    expect(cameraScanSupport({ ...CAPABLE, navigator: {} })).toBe(false);
  });

  test("a detector is only built where scanning is supported", () => {
    expect(newQrDetector(CAPABLE)).not.toBeNull();
    expect(newQrDetector(undefined)).toBeNull();
    expect(newQrDetector({ ...CAPABLE, isSecureContext: false })).toBeNull();
  });

  test("a browser with the API but no QR support is caught", () => {
    /**
     * Feature detection is necessary and not sufficient: the constructor
     * throws on a format the platform cannot decode. Without this the dialog
     * would open onto a camera that never resolves anything.
     */
    const noQr: ScanCapableWindow = {
      ...CAPABLE,
      BarcodeDetector: class {
        constructor() {
          throw new TypeError("qr_code is not a supported format");
        }
        async detect() {
          return [];
        }
      },
    };
    expect(cameraScanSupport(noQr)).toBe(true);
    expect(newQrDetector(noQr)).toBeNull();
  });
});

test.describe("what counts as a card in a frame", () => {
  test("a UHID in view is read", () => {
    expect(firstUhid([{ rawValue: "DEMO-26-000042" }])).toBe("DEMO-26-000042");
  });

  test("an empty frame reads as nothing", () => {
    expect(firstUhid([])).toBeNull();
  });

  test("the card is found past other codes in the same frame", () => {
    /**
     * Why this filters rather than taking `[0]`. A card lying on a counter
     * next to a lab requisition, or a poster on the wall behind the patient,
     * puts more than one code in view — and the camera decides the order, not
     * us.
     */
    expect(
      firstUhid([
        { rawValue: "https://example.test/anything" },
        { rawValue: "LAB-REQ-99812" },
        { rawValue: "DEMO-26-000042" },
      ]),
    ).toBe("DEMO-26-000042");
  });

  test("a frame with no card keeps the camera looking", () => {
    // Silence, not an error. The camera is pointed at the wrong thing and the
    // remedy is to move it, which the person holding it is already doing.
    expect(firstUhid([{ rawValue: "https://example.test/" }])).toBeNull();
  });

  test("a card read in lower case still resolves", () => {
    expect(firstUhid([{ rawValue: " demo-26-000042 " }])).toBe("DEMO-26-000042");
  });

  test("both scan paths agree on what a card is", () => {
    /**
     * The reason `normaliseUhid` was pulled out of the keystroke reader. The
     * counter scanner sends keys and the camera sends a decoded string; what
     * counts as a card must not depend on which device read it.
     */
    for (const raw of ["DEMO-26-000042", "demo-26-000042", "NOPE", "DEMO-26-42"]) {
      expect(firstUhid([{ rawValue: raw }])).toBe(normaliseUhid(raw));
    }
  });
});

test.describe("explaining a camera that will not start", () => {
  test("a refused permission is the user's to fix", () => {
    expect(cameraFailureReason({ name: "NotAllowedError" })).toBe("denied");
    expect(cameraFailureReason({ name: "SecurityError" })).toBe("denied");
  });

  test("a missing camera is not", () => {
    /**
     * Worth separating from a denial: telling somebody to check their camera
     * permissions on a desktop that has no webcam wastes their morning.
     */
    expect(cameraFailureReason({ name: "NotFoundError" })).toBe("noCamera");
    expect(cameraFailureReason({ name: "OverconstrainedError" })).toBe("noCamera");
  });

  test("anything else still gets a sentence", () => {
    // Never a dead end, and never a raw browser message: all three outcomes
    // say the counter scanner and typing still work.
    expect(cameraFailureReason(new Error("camera in use"))).toBe("failed");
    expect(cameraFailureReason(null)).toBe("failed");
    expect(cameraFailureReason(undefined)).toBe("failed");
  });
});
