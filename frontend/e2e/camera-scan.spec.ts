import { expect, test, type Page } from "@playwright/test";
import qrcode from "qrcode-generator";

import { ACCOUNTS, registerAndReadSlip, signIn } from "./support";

/**
 * Scanning a patient card with the device camera, from the top bar.
 *
 * For the people with no counter scanner within reach: a nurse with a tablet,
 * a doctor with a phone. Where the browser has a native `BarcodeDetector` it
 * is used; where it does not — every iPhone and iPad, Firefox, Windows desktop
 * Chrome — a JavaScript decoder is downloaded on the first tap.
 *
 * ## What these tests can and cannot prove
 *
 * Headless Chromium has no camera, so `getUserMedia` is replaced with a canvas
 * capture stream. Two kinds of device are then built on top of it:
 *
 * - **Native**: a stub `BarcodeDetector` that reports whatever UHID the test
 *   sets. Exercises the dialog, the frame loop, resolving and releasing.
 * - **iPad**: no `BarcodeDetector` at all, and the canvas paints a *real* QR
 *   code made the way `QrSvg` prints one. The downloaded decoder has to read
 *   it out of live video frames, so that path is exercised from the pixels up.
 *
 * What is not covered is a real lens reading a real printed code under ward
 * lighting. That needs paper and a camera (QR plan §7).
 */

/** Where the stubs read their answers from, so a test can set them mid-session. */
const UHID_KEY = "e2e-camera-uhid";
const QR_KEY = "e2e-camera-qr";
const DENY_KEY = "e2e-camera-deny";

type CameraOptions = {
  /** Whether the browser has its own `BarcodeDetector`. */
  native?: boolean;
  /** Whether `enumerateDevices` lists a camera. */
  camera?: boolean;
};

/**
 * Give the page a camera, and optionally a native decoder.
 *
 * Installed as an init script so it is in place before any app code runs —
 * the support check happens on hydration, and a stub applied afterwards would
 * be too late to make the button appear.
 *
 * The fake camera paints the QR matrix stored under `QR_KEY`, if any, onto a
 * white canvas and streams it. It repaints continually, because a canvas only
 * produces frames when it changes and a video with no frames never becomes
 * ready to read.
 */
async function stubCamera(page: Page, { native = true, camera = true }: CameraOptions = {}) {
  await page.addInitScript(
    ([uhidKey, qrKey, denyKey, withNative, withCamera]: [string, string, string, boolean, boolean]) => {
      class FakeBarcodeDetector {
        constructor(options?: { formats?: string[] }) {
          // The real constructor throws on an unsupported format; keep the
          // stub honest about it.
          if (!options?.formats?.includes("qr_code")) throw new TypeError("unsupported format");
        }

        static async getSupportedFormats() {
          return ["qr_code"];
        }

        async detect() {
          const value = window.localStorage.getItem(uhidKey);
          return value === null ? [] : [{ rawValue: value }];
        }
      }

      Object.defineProperty(window, "BarcodeDetector", {
        value: withNative ? FakeBarcodeDetector : undefined,
        configurable: true,
        writable: true,
      });

      Object.defineProperty(window.navigator, "mediaDevices", {
        configurable: true,
        value: {
          enumerateDevices: async () =>
            withCamera
              ? [{ kind: "audioinput" }, { kind: "videoinput" }]
              : [{ kind: "audioinput" }],

          getUserMedia: async () => {
            if (window.localStorage.getItem(denyKey) !== null) {
              const denial = new Error("Permission denied");
              denial.name = "NotAllowedError";
              throw denial;
            }

            const canvas = document.createElement("canvas");
            canvas.width = 640;
            canvas.height = 480;
            const context = canvas.getContext("2d");

            let tick = 0;
            const paint = () => {
              if (context === null) return;
              tick += 1;
              context.fillStyle = "#ffffff";
              context.fillRect(0, 0, canvas.width, canvas.height);

              const stored = window.localStorage.getItem(qrKey);
              if (stored !== null) {
                const modules = JSON.parse(stored) as boolean[][];
                const scale = 10;
                const left = (canvas.width - modules.length * scale) / 2;
                const top = (canvas.height - modules.length * scale) / 2;
                context.fillStyle = "#000000";
                modules.forEach((row, y) =>
                  row.forEach((dark, x) => {
                    if (dark) context.fillRect(left + x * scale, top + y * scale, scale, scale);
                  }),
                );
              }

              // A pixel in the corner that changes every tick, so the canvas
              // always has a new frame to send.
              context.fillStyle = tick % 2 === 0 ? "#fefefe" : "#ffffff";
              context.fillRect(0, 0, 1, 1);
            };
            paint();
            const stream = canvas.captureStream(10);
            const painting = window.setInterval(paint, 100);
            stream
              .getVideoTracks()[0]
              .addEventListener("ended", () => window.clearInterval(painting));

            return stream;
          },
        },
      });
    },
    [UHID_KEY, QR_KEY, DENY_KEY, native, camera] as [string, string, string, boolean, boolean],
  );
}

/** A UHID's QR as a module matrix, encoded exactly as `QrSvg` encodes it. */
function qrModules(value: string): boolean[][] {
  const qr = qrcode(0, "M");
  qr.addData(value);
  qr.make();
  const count = qr.getModuleCount();
  return Array.from({ length: count }, (_, row) =>
    Array.from({ length: count }, (_, column) => qr.isDark(row, column)),
  );
}

async function setStored(page: Page, key: string, value: string): Promise<void> {
  await page.evaluate(([k, v]) => window.localStorage.setItem(k, v), [key, value] as [string, string]);
}

/** The scanner icon in the top bar — the only place it lives. */
const scanButton = (page: Page) =>
  page.getByRole("banner").getByRole("button", { name: "Scan a card" });

/**
 * Record every camera track the page is handed, so a test can check they
 * were stopped without needing the video element to still exist.
 */
async function recordTracks(page: Page): Promise<void> {
  await page.evaluate(() => {
    const media = window.navigator.mediaDevices;
    const original = media.getUserMedia.bind(media);
    const issued: MediaStreamTrack[] = [];
    (window as unknown as { __tracks: MediaStreamTrack[] }).__tracks = issued;
    media.getUserMedia = async (constraints?: MediaStreamConstraints) => {
      const stream = await original(constraints);
      issued.push(...stream.getTracks());
      return stream;
    };
  });
}

const trackStates = (page: Page) =>
  page.evaluate(() =>
    (window as unknown as { __tracks: MediaStreamTrack[] }).__tracks.map((track) => track.readyState),
  );

/**
 * Wait until React has hydrated, without depending on any data being there.
 *
 * Pressing F3 proves the client is live, because the shortcut is a listener
 * `UniversalSearch` attaches on hydration. Without this an "is absent"
 * assertion would be vacuous: the scan button is client-only, so it is
 * trivially absent before hydration.
 */
async function waitForHydration(page: Page): Promise<void> {
  await page.keyboard.press("F3");
  const box = page.getByRole("combobox");
  await expect(box).toBeVisible({ timeout: 45_000 });
  await page.keyboard.press("Escape");
  await expect(box).toBeHidden();
}

test.describe("the camera permission", () => {
  test("lets this app use the camera, and nothing else gains anything", async ({ page }) => {
    /**
     * The policy used to be `camera=()`, which forbids the camera to every
     * page including our own — so `getUserMedia` was refused before any
     * prompt, on every device, and the scanner could never have worked
     * outside a stubbed test. `(self)` grants it to this origin only: a
     * framed page or a third-party script's iframe still gets nothing.
     */
    const response = await page.request.get("/login");
    const policy = response.headers()["permissions-policy"];

    expect(policy).toContain("camera=(self)");
    expect(policy).toContain("microphone=()");
    expect(policy).toContain("geolocation=()");
    expect(policy).toContain("payment=()");
  });
});

test.describe("the camera scan button", () => {
  test("sits in the top bar, on every screen", async ({ page }) => {
    await stubCamera(page);
    await signIn(page, ACCOUNTS.reception);

    await page.goto("/reception");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });
    // One way in, not two: the page itself no longer carries a copy.
    await expect(page.getByRole("main").getByRole("button", { name: "Scan a card" })).toHaveCount(0);

    await page.goto("/account/two-factor");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });
  });

  test("is there for a nurse too", async ({ page }) => {
    // The person it is most for: a tablet, a ward round, no counter scanner.
    await stubCamera(page);
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/queue");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });
  });

  test("fits a phone's top bar without pushing the page sideways", async ({ page }) => {
    /**
     * The bar was already full at phone width — menu, search, language, user
     * — and this adds a fifth thing to it on exactly the devices with the
     * least room. Headless Chromium has no camera, so the tablet layout specs
     * never see the icon; this is the one that does.
     */
    await page.setViewportSize({ width: 375, height: 812 });
    await stubCamera(page);
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/queue");
    await expect(scanButton(page)).toBeInViewport({ timeout: 45_000 });

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(overflow, `the page scrolls sideways by ${overflow}px`).toBeLessThanOrEqual(0);
  });

  test("is absent on a device with no camera", async ({ page }) => {
    /**
     * The counter PC. It has a USB scanner and no webcam, and an icon that
     * opens only to say it cannot work teaches staff to ignore icons.
     */
    await stubCamera(page, { camera: false });
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/reception");
    await waitForHydration(page);
    await expect(scanButton(page)).toHaveCount(0);
  });

  test("on an iPad, reads a real QR code from the camera and opens the patient", async ({
    page,
  }) => {
    /**
     * The case this was built for. No `BarcodeDetector`, as on every iPhone
     * and iPad, so the scanner has to download its decoder and read the code
     * out of the video itself. The code on screen is the card's actual QR.
     */
    await stubCamera(page, { native: false });
    const { name, uhid } = await registerAndReadSlip(page);
    await setStored(page, QR_KEY, JSON.stringify(qrModules(uhid)));

    await page.goto("/reception");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });
    await scanButton(page).click();
    await expect(page.getByTestId("scan-preview")).toBeVisible();

    await expect(page).toHaveURL(/\/patients\/[0-9a-f-]{36}/, { timeout: 45_000 });
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  });

  test("with a native decoder, opens the patient", async ({ page }) => {
    await stubCamera(page);
    const { name, uhid } = await registerAndReadSlip(page);
    await setStored(page, UHID_KEY, uhid);

    await page.goto("/reception");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });

    await scanButton(page).click();
    await expect(page.getByRole("dialog")).toContainText("Hold the QR code");
    await expect(page.getByTestId("scan-preview")).toBeVisible();

    await expect(page).toHaveURL(/\/patients\/[0-9a-f-]{36}/, { timeout: 45_000 });
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  });

  test("releases the camera once it has read a card", async ({ page }) => {
    /**
     * Not housekeeping. The indicator light stays lit until every track is
     * stopped, and a camera left running on a shared machine in a hospital is
     * the kind of thing that gets software removed from the ward. Asserted on
     * the track's `readyState`, which is what drives the light.
     */
    await stubCamera(page);
    const { uhid } = await registerAndReadSlip(page);
    await setStored(page, UHID_KEY, uhid);

    await page.goto("/reception");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });
    await recordTracks(page);

    await scanButton(page).click();
    await expect(page).toHaveURL(/\/patients\/[0-9a-f-]{36}/, { timeout: 45_000 });
    await expect.poll(() => trackStates(page)).toEqual(["ended"]);
  });

  test("stops the camera when the screen is put away", async ({ page }) => {
    /**
     * A tablet locked mid-scan, or the app switched away from. The camera
     * must not keep running behind the lock screen, and coming back to a
     * frozen preview would look like a hang.
     */
    await stubCamera(page);
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/queue");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });
    await recordTracks(page);

    await scanButton(page).click();
    await expect.poll(() => trackStates(page)).toEqual(["live"]);

    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await expect(page.getByRole("dialog")).toBeHidden();
    await expect.poll(() => trackStates(page)).toEqual(["ended"]);
  });

  test("gives up after a minute of reading nothing", async ({ page }) => {
    /**
     * The idle lock covers the screen after ten minutes but does not close
     * dialogs under it, so a scanner left open would film the ward behind the
     * lock screen. A minute is far longer than any real scan takes.
     */
    await page.clock.install();
    await stubCamera(page);
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/queue");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });
    await recordTracks(page);

    await scanButton(page).click();
    await expect.poll(() => trackStates(page)).toEqual(["live"]);

    await page.clock.runFor(61_000);

    await expect(page.getByRole("dialog")).toBeHidden();
    await expect.poll(() => trackStates(page)).toEqual(["ended"]);
  });

  test("a blocked camera explains itself instead of dying quietly", async ({ page }) => {
    /**
     * Somebody hit Block on the permission prompt, or did last week and
     * forgot. The dialog says what to do — and that the counter scanner and
     * typing still work, so the answer is never "wait for IT".
     */
    await stubCamera(page);
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/reception");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });
    await setStored(page, DENY_KEY, "1");

    await scanButton(page).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("The camera is blocked");
    await expect(dialog).toContainText("counter scanner");
    await expect(page.getByTestId("scan-preview")).toHaveCount(0);
    await expect(page).toHaveURL(/\/reception/);
  });
});
