import { expect, test } from "@playwright/test";

import {
  cameraFailureReason,
  cameraScanSupport,
  firstUhid,
  hasCamera,
  loadQrDetector,
  nativeQrDetector,
  type BarcodeDetectorLike,
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
 * whether this browser can scan at all, which decoder it gets, which detected
 * code is a card, and what to tell somebody when the camera will not start.
 * Those are the whole of the logic; the rest is the platform's.
 */

/** A native decoder, as Chrome on Android provides one. */
class NativeDetector {
  async detect() {
    return [];
  }
}

/** A `window` that can scan natively. Each test breaks exactly one condition. */
const CAPABLE: ScanCapableWindow = {
  BarcodeDetector: NativeDetector,
  isSecureContext: true,
  navigator: { mediaDevices: { getUserMedia: async () => undefined } },
};

/** Safari on an iPad: a camera, TLS, and no `BarcodeDetector`. */
const IPAD: ScanCapableWindow = { ...CAPABLE, BarcodeDetector: undefined };

/** Stands in for the downloaded decoder, and counts how often it was fetched. */
function fallbackLoader() {
  const fallback: BarcodeDetectorLike = { detect: async () => [] };
  const loader = Object.assign(
    async () => {
      loader.calls += 1;
      return fallback;
    },
    { calls: 0 },
  );
  return { fallback, loader };
}

test.describe("whether this browser can scan", () => {
  test("a capable browser can", () => {
    expect(cameraScanSupport(CAPABLE)).toBe(true);
  });

  test("no window at all cannot", () => {
    // The server render. The button must not appear before hydration says so.
    expect(cameraScanSupport(undefined)).toBe(false);
  });

  test("without BarcodeDetector it still can", () => {
    /**
     * The case this whole path was rebuilt for. Every browser on an iPhone or
     * an iPad is Safari underneath, and Safari has no `BarcodeDetector` — nor
     * do Firefox and Windows desktop Chrome. Those devices get a downloaded
     * decoder instead, so a missing native one no longer hides the button.
     */
    expect(cameraScanSupport(IPAD)).toBe(true);
  });

  test("over plain HTTP it cannot", () => {
    // `getUserMedia` does not exist without TLS, so this is not a policy
    // choice — there is no camera to ask for. The pilot site needs HTTPS.
    expect(cameraScanSupport({ ...CAPABLE, isSecureContext: false })).toBe(false);
  });

  test("without mediaDevices it cannot", () => {
    expect(cameraScanSupport({ ...CAPABLE, navigator: {} })).toBe(false);
  });

  test("without getUserMedia it cannot", () => {
    // An embedded webview can expose `mediaDevices` with nothing on it.
    expect(cameraScanSupport({ ...CAPABLE, navigator: { mediaDevices: {} } })).toBe(false);
  });

  test("a native detector is only built where scanning is supported", () => {
    expect(nativeQrDetector(CAPABLE)).not.toBeNull();
    expect(nativeQrDetector(undefined)).toBeNull();
    expect(nativeQrDetector(IPAD)).toBeNull();
    expect(nativeQrDetector({ ...CAPABLE, isSecureContext: false })).toBeNull();
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
    expect(nativeQrDetector(noQr)).toBeNull();
  });
});

test.describe("which decoder a device gets", () => {
  test("a native decoder is used where there is one, and nothing is downloaded", async () => {
    // Android Chrome pays nothing for the iPad's sake.
    const { loader } = fallbackLoader();
    const detector = await loadQrDetector(CAPABLE, loader);
    expect(detector).toBeInstanceOf(NativeDetector);
    expect(loader.calls).toBe(0);
  });

  test("an iPad downloads the decoder", async () => {
    const { fallback, loader } = fallbackLoader();
    expect(await loadQrDetector(IPAD, loader)).toBe(fallback);
    expect(loader.calls).toBe(1);
  });

  test("a native API that cannot read QR falls back too", async () => {
    const { fallback, loader } = fallbackLoader();
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
    expect(await loadQrDetector(noQr, loader)).toBe(fallback);
  });

  test("a native API that lists no QR support falls back too", async () => {
    /**
     * Desktop Chrome on Linux: the class exists, the constructor accepts
     * `qr_code` without complaint, and every frame comes back empty. The only
     * honest signal is `getSupportedFormats`, so it is asked.
     */
    const { fallback, loader } = fallbackLoader();
    const silent: ScanCapableWindow = {
      ...CAPABLE,
      BarcodeDetector: Object.assign(class extends NativeDetector {}, {
        getSupportedFormats: async () => [],
      }),
    };
    expect(await loadQrDetector(silent, loader)).toBe(fallback);
  });

  test("nothing is downloaded where the camera cannot work anyway", async () => {
    // Plain HTTP: no camera to point it at, so no reason to fetch a decoder.
    const { loader } = fallbackLoader();
    expect(await loadQrDetector({ ...IPAD, isSecureContext: false }, loader)).toBeNull();
    expect(loader.calls).toBe(0);
  });
});

test.describe("whether the device has a camera", () => {
  test("a listed video input is a camera", () => {
    expect(hasCamera([{ kind: "audioinput" }, { kind: "videoinput" }])).toBe(true);
  });

  test("a desktop with only a microphone has none", () => {
    /**
     * The counter PC. Without this the scanner icon would sit in its header
     * all day and, when tapped, explain that it cannot work — the button
     * staff learn to ignore.
     */
    expect(hasCamera([{ kind: "audioinput" }, { kind: "audiooutput" }])).toBe(false);
    expect(hasCamera([])).toBe(false);
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
