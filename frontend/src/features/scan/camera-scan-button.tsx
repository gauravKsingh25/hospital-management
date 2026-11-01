"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";
import { ScanQrCode } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  cameraFailureReason,
  cameraScanSupport,
  firstUhid,
  hasCamera,
  loadQrDetector,
  type BarcodeDetectorLike,
  type CameraFailure,
} from "@/lib/camera-scan";
import { useResolveScan } from "@/lib/hooks/use-resolve-scan";

/**
 * How often a frame is examined.
 *
 * Not `requestAnimationFrame`: decoding sixty frames a second to answer a
 * question a person needs answered once would heat a tablet for nothing. Five
 * looks a second is imperceptible against the two-second target and leaves the
 * camera preview smooth, because the preview is the video element and does not
 * wait on the detector.
 */
const POLL_MS = 200;

/**
 * How long the scanner stays open without reading a card.
 *
 * Far longer than any real scan, and far shorter than the ten minutes after
 * which the idle lock covers the screen. The lock does not close dialogs
 * under it, so without this a scanner left open would keep filming the ward
 * behind the lock screen.
 */
const SCAN_TIMEOUT_MS = 60_000;

/** Enough decoded frames to draw from — `HTMLMediaElement.HAVE_CURRENT_DATA`. */
const HAVE_CURRENT_DATA = 2;

/**
 * Whether this browser can use a camera at all, read through
 * `useSyncExternalStore`.
 *
 * The answer does not exist on the server, and the two obvious ways to handle
 * that are both wrong: rendering the button optimistically and removing it on
 * hydration is a visible flash, and setting it from an effect is a cascading
 * render. `useSyncExternalStore` is the built-in answer — a server snapshot of
 * `false` and a client snapshot of the truth, resolved during hydration.
 */
const subscribeNever = () => () => {};
const readSupport = () => cameraScanSupport(window);
const unsupportedOnServer = () => false;

/**
 * Whether the device has a camera — the asynchronous half of the same question.
 *
 * `enumerateDevices` answers without a permission prompt, and the answer can
 * change: a webcam plugged into a counter PC fires `devicechange`. Held at
 * module level because the snapshot must be readable synchronously, and there
 * is one top bar, so one scanner.
 *
 * A failed listing counts as a camera. Hiding the scanner on a guess would
 * make it vanish with no explanation; showing it costs, at worst, a dialog
 * that says there is no camera.
 */
let cameraPresent = false;

function subscribeCamera(onChange: () => void): () => void {
  if (!cameraScanSupport(window)) return () => {};
  const media = navigator.mediaDevices;
  let live = true;

  const update = (present: boolean) => {
    if (!live || present === cameraPresent) return;
    cameraPresent = present;
    onChange();
  };
  const check = () => {
    if (typeof media.enumerateDevices !== "function") return update(true);
    media.enumerateDevices().then(
      (devices) => update(hasCamera(devices)),
      () => update(true),
    );
  };

  check();
  media.addEventListener?.("devicechange", check);
  return () => {
    live = false;
    media.removeEventListener?.("devicechange", check);
  };
}

const readCamera = () => cameraPresent;

/**
 * Scan a patient card or token slip with the device camera, from the top bar
 * (CLAUDE.md §7b, QR lookup).
 *
 * For whoever has no counter scanner within reach — a nurse on a ward round
 * with a tablet, a doctor with a phone. Works on any device with a camera and
 * TLS: the browser's own decoder where it has one, a downloaded one where it
 * does not (`loadQrDetector`).
 *
 * **It renders nothing where it cannot work** — no camera, or plain HTTP. A
 * counter PC with no webcam never shows the icon, because a button that opens
 * only to explain its own impossibility teaches staff to ignore buttons.
 */
export function CameraScanButton() {
  const t = useTranslations("scan");
  const { resolve } = useResolveScan();

  const supported = useSyncExternalStore(subscribeNever, readSupport, unsupportedOnServer);
  const camera = useSyncExternalStore(subscribeCamera, readCamera, unsupportedOnServer);

  const [open, setOpen] = useState(false);
  const [ready, setReady] = useState(false);
  const [failure, setFailure] = useState<CameraFailure | null>(null);

  // The video element arrives through a callback ref rather than a plain one,
  // because it mounts with the dialog — a `ref.current` read in the effect
  // below could still be null on the pass that opens it. The boolean is only
  // there to re-run the effect once the element exists; the element itself
  // stays in a ref, since it is a mutable DOM node and not React state.
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [attached, setAttached] = useState(false);
  const attachVideo = useCallback((node: HTMLVideoElement | null) => {
    videoRef.current = node;
    setAttached(node !== null);
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!open || !attached || video === null) return;

    let stopped = false;
    let stream: MediaStream | null = null;
    let timer: ReturnType<typeof setInterval> | undefined;

    /**
     * Release the camera.
     *
     * Not housekeeping — the indicator light stays lit until every track is
     * stopped, and a camera left running on a shared machine in a hospital is
     * the kind of thing that gets software removed from the ward. Every exit
     * from this effect goes through here, including the failure paths.
     *
     * It reads `giveUp` and `onVisibility`, declared below. Safe: nothing
     * calls this until both exist.
     */
    const stop = () => {
      stopped = true;
      if (timer !== undefined) clearInterval(timer);
      clearTimeout(giveUp);
      document.removeEventListener("visibilitychange", onVisibility);
      stream?.getTracks().forEach((track) => track.stop());
      video.srcObject = null;
    };

    const close = () => {
      stop();
      setOpen(false);
    };

    // A tablet locked mid-scan, or switched to another app: stop now rather
    // than film the inside of a pocket, and rather than come back to a frozen
    // preview that looks like a hang.
    const onVisibility = () => {
      if (document.visibilityState === "hidden") close();
    };
    document.addEventListener("visibilitychange", onVisibility);
    const giveUp = setTimeout(close, SCAN_TIMEOUT_MS);

    const scanFrame = async (detector: BarcodeDetectorLike) => {
      if (stopped || video.readyState < HAVE_CURRENT_DATA) return;

      let found: string | null = null;
      try {
        found = firstUhid(await detector.detect(video));
      } catch {
        // A rejected frame is not a failure. The detector throws on a frame it
        // cannot read, which is most of them while the card is still moving.
        return;
      }

      if (found === null || stopped) return;
      close();
      resolve(found);
    };

    void (async () => {
      // Started before the camera is asked for, not after: on a first scan
      // the permission prompt is the slow part, and an iPad's decoder
      // download fits inside it. Its rejection is handled where it is awaited;
      // the empty catch only stops it being reported as unhandled meanwhile.
      const detecting = loadQrDetector(window);
      detecting.catch(() => {});

      try {
        // `environment` is the rear camera on a phone or tablet — the one
        // pointed at the card rather than at the nurse. A laptop has only one
        // and ignores the hint.
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
        });
        if (stopped) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }

        video.srcObject = stream;
        await video.play();

        const detector = await detecting;
        if (detector === null) throw new Error("No QR decoder could be made");
        if (stopped) return;

        setReady(true);
        timer = setInterval(() => void scanFrame(detector), POLL_MS);
      } catch (error) {
        // The dialog may already have closed while the permission prompt was
        // up, in which case the rejection is our own teardown and not news.
        // A decoder that failed to download lands here too, as "failed".
        if (!stopped) setFailure(cameraFailureReason(error));
        stop();
      }
    })();

    return stop;
  }, [open, attached, resolve]);

  if (!supported || !camera) return null;

  return (
    <>
      <Button
        variant="ghost"
        // A full tap target (UX-03) — this is the button reached for with a
        // card in the other hand.
        className="h-tap min-w-tap"
        aria-label={t("button")}
        title={t("button")}
        onClick={() => {
          setFailure(null);
          setReady(false);
          setOpen(true);
        }}
      >
        <ScanQrCode aria-hidden className="size-5" />
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("title")}</DialogTitle>
            <DialogDescription>{failure === null ? t("hint") : t(failure)}</DialogDescription>
          </DialogHeader>

          {failure === null ? (
            <div className="bg-muted relative aspect-[3/4] w-full overflow-hidden rounded-lg sm:aspect-[4/3]">
              <video
                ref={attachVideo}
                data-testid="scan-preview"
                // Muted and inline are what let a browser start playback
                // without a second gesture; iOS refuses to play inline
                // without both.
                muted
                playsInline
                className="size-full object-cover"
              />

              {/* Where to hold the card. Decorative: the decoder reads the
                  whole frame, so a code outside the square still scans. */}
              <div
                aria-hidden
                className="pointer-events-none absolute inset-0 flex items-center justify-center"
              >
                <div className="aspect-square w-3/5 max-w-64 rounded-xl border-4 border-white/90 shadow-[0_0_0_9999px_rgb(0_0_0/0.35)]" />
              </div>

              {ready ? null : (
                <p
                  role="status"
                  className="absolute inset-x-0 bottom-3 text-center text-sm font-medium text-white"
                >
                  {t("starting")}
                </p>
              )}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
