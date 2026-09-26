import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, registerAndReadSlip, signIn } from "./support";

/**
 * Scanning a patient card with the device camera (QR plan §5.6).
 *
 * The optional half of the QR feature — a nurse on a ward round has a tablet
 * and no USB scanner. It is built on the browser's own `BarcodeDetector`, so
 * it ships no decoder and adds nothing to the bundle.
 *
 * ## What these tests can and cannot prove
 *
 * Headless Chromium has neither `BarcodeDetector` nor a camera, and no flag
 * gives it one. So the first test below is the honest one — with nothing
 * stubbed, the button must be **absent** — and the rest run against a stubbed
 * detector and a canvas video stream.
 *
 * That stub is worth being precise about. It replaces exactly two platform
 * pieces: the decoder and the camera. Everything this feature actually
 * contains sits between them and is really exercised — the support gate, the
 * dialog, the frame loop, deciding which decoded string is a card, releasing
 * the camera afterwards, and the resolve-and-navigate that follows. What is
 * *not* covered is whether a real lens reads a real printed code, and no
 * amount of stubbing would cover that. It needs paper and a camera, and it is
 * the same hardware gate the counter scanner has (plan §7).
 */

/** Where the stub reads its answers from, so a test can set them mid-session. */
const UHID_KEY = "e2e-camera-uhid";
const DENY_KEY = "e2e-camera-deny";

/**
 * Give the page a decoder and a camera.
 *
 * Installed as an init script so it is in place before any app code runs —
 * the support check happens on hydration, and a stub applied afterwards would
 * be too late to make the button appear.
 *
 * The fake `getUserMedia` returns a canvas capture stream rather than an empty
 * `MediaStream`: the component waits for the video element to have frames
 * before looking at one, so a stream with no track would hang exactly where a
 * real camera would not.
 */
async function stubCamera(page: Page): Promise<void> {
  await page.addInitScript(
    ([uhidKey, denyKey]: [string, string]) => {
      class FakeBarcodeDetector {
        constructor(options?: { formats?: string[] }) {
          // The real constructor throws on an unsupported format, and the
          // component depends on that to detect a browser that has the API
          // but cannot do QR. Keep the stub honest about it.
          if (!options?.formats?.includes("qr_code")) throw new TypeError("unsupported format");
        }

        async detect() {
          const value = window.localStorage.getItem(uhidKey);
          return value === null ? [] : [{ rawValue: value }];
        }
      }

      Object.defineProperty(window, "BarcodeDetector", {
        value: FakeBarcodeDetector,
        configurable: true,
        writable: true,
      });

      Object.defineProperty(window.navigator, "mediaDevices", {
        configurable: true,
        value: {
          getUserMedia: async () => {
            if (window.localStorage.getItem(denyKey) !== null) {
              const denial = new Error("Permission denied");
              denial.name = "NotAllowedError";
              throw denial;
            }

            const canvas = document.createElement("canvas");
            canvas.width = 320;
            canvas.height = 240;
            const context = canvas.getContext("2d");

            // A canvas only produces frames when it changes, so keep it
            // changing — otherwise the video element may never report having
            // data and the frame loop would wait forever.
            let tick = 0;
            const paint = () => {
              if (context === null) return;
              tick += 1;
              context.fillStyle = tick % 2 === 0 ? "#222222" : "#232323";
              context.fillRect(0, 0, canvas.width, canvas.height);
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
    [UHID_KEY, DENY_KEY] as [string, string],
  );
}

const scanButton = (page: Page) => page.getByRole("button", { name: "Scan a card" });

/**
 * Wait until React has hydrated, without depending on any data being there.
 *
 * The absence test needs this and the first draft got it wrong: it waited for
 * a queue row, which only exists if some other spec has already registered
 * somebody. Run against a freshly reset database it timed out — a test that
 * passes on leftovers is a test that will fail at the worst moment.
 *
 * Pressing F3 proves the client is live, because the shortcut is a listener
 * `UniversalSearch` attaches on hydration and nothing renders it server-side.
 * It needs no patients, no queue and no fixtures.
 *
 * Without this the assertion below would be vacuous: the scan button is
 * client-only, so "not present" is trivially true before hydration and says
 * nothing about whether the support check works.
 */
async function waitForHydration(page: Page): Promise<void> {
  await page.keyboard.press("F3");
  const box = page.getByRole("combobox");
  await expect(box).toBeVisible({ timeout: 45_000 });
  await page.keyboard.press("Escape");
  await expect(box).toBeHidden();
}

test.describe("the camera scan button", () => {
  test("is absent on a device that cannot scan", async ({ page }) => {
    /**
     * Nothing stubbed — plain Chromium, which has no `BarcodeDetector`, like
     * Windows desktop Chrome and every Firefox. That is the machine most
     * likely to be sitting at a reception counter, so this is the default
     * case rather than the edge one.
     *
     * A button that opens a dialog to explain that it cannot work is worse
     * than no button: staff learn to ignore buttons. The counter scanner and
     * typing the UHID are untouched either way.
     */
    await signIn(page, ACCOUNTS.reception);

    for (const path of ["/reception", "/queue"]) {
      await page.goto(path);
      await waitForHydration(page);
      await expect(scanButton(page)).toHaveCount(0);
    }
  });

  test("appears where the browser can scan, and opens the patient", async ({ page }) => {
    await stubCamera(page);
    const { name, uhid } = await registerAndReadSlip(page);

    // Set after sign-in, because it is the app's own origin that holds it.
    await page.evaluate(
      ([key, value]) => window.localStorage.setItem(key, value),
      [UHID_KEY, uhid] as [string, string],
    );

    await page.goto("/queue");
    // The button is client-only, so seeing it is itself proof of hydration.
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });

    await scanButton(page).click();
    await expect(page.getByRole("dialog")).toContainText("Hold the QR code");
    await expect(page.getByTestId("scan-preview")).toBeVisible();

    // One physical action from here: hold the card up. The loop finds it.
    await expect(page).toHaveURL(/\/patients\/[0-9a-f-]{36}/, { timeout: 45_000 });
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  });

  test("releases the camera once it has read a card", async ({ page }) => {
    /**
     * Not housekeeping. The indicator light stays lit until every track is
     * stopped, and a camera left running on a shared machine in a hospital is
     * the kind of thing that gets software removed from the ward.
     *
     * Asserted on the track's `readyState`, which is the same thing the
     * indicator light is driven by.
     */
    await stubCamera(page);
    const { uhid } = await registerAndReadSlip(page);
    await page.evaluate(
      ([key, value]) => window.localStorage.setItem(key, value),
      [UHID_KEY, uhid] as [string, string],
    );

    await page.goto("/queue");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });

    // Record every track the page hands out, so the check does not depend on
    // still being able to reach the video element after the dialog closes.
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

    await scanButton(page).click();
    await expect(page).toHaveURL(/\/patients\/[0-9a-f-]{36}/, { timeout: 45_000 });

    await expect
      .poll(() =>
        page.evaluate(() =>
          (window as unknown as { __tracks: MediaStreamTrack[] }).__tracks.map(
            (track) => track.readyState,
          ),
        ),
      )
      .toEqual(["ended"]);
  });

  test("a blocked camera explains itself instead of dying quietly", async ({ page }) => {
    /**
     * The failure that actually happens: somebody hits Block on the
     * permission prompt, or blocked it last week and forgot. The dialog has
     * to say what to do — and say that the other two ways in still work, so
     * the answer is never "come back when IT has fixed it".
     */
    await stubCamera(page);
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/queue");
    await expect(scanButton(page)).toBeVisible({ timeout: 45_000 });
    await page.evaluate((key) => window.localStorage.setItem(key, "1"), DENY_KEY);

    await scanButton(page).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("The camera is blocked");
    await expect(dialog).toContainText("counter scanner");
    // No black rectangle sitting under the explanation.
    await expect(page.getByTestId("scan-preview")).toHaveCount(0);
    // And nothing navigated.
    await expect(page).toHaveURL(/\/queue/);
  });
});
