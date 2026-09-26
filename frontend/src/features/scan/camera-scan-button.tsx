"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";
import { ScanLine } from "lucide-react";

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
  newQrDetector,
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

/** Enough decoded frames to draw from — `HTMLMediaElement.HAVE_CURRENT_DATA`. */
const HAVE_CURRENT_DATA = 2;

/**
 * Whether this browser can scan, read through `useSyncExternalStore`.
 *
 * The answer does not exist on the server, and the two obvious ways to handle
 * that are both wrong: rendering the button optimistically and removing it on
 * hydration is a visible flash, and setting it from an effect is a cascading
 * render that the React Compiler rightly objects to. `useSyncExternalStore`
 * is the built-in answer to exactly this — a server snapshot of `false` and a
 * client snapshot of the truth, resolved during hydration.
 *
 * The subscription never fires because the answer never changes: a browser
 * does not grow a `BarcodeDetector` mid-session.
 */
const subscribeNever = () => () => {};
const readSupport = () => cameraScanSupport(window);
const unsupportedOnServer = () => false;

/**
 * Scan a patient card with the device camera (CLAUDE.md §7b, QR lookup).
 *
 * The optional half of the QR feature. A counter has a USB scanner and needs
 * none of this; a nurse on a ward round has a tablet and no scanner, and this
 * is for them.
 *
 * **It renders nothing where it cannot work.** `BarcodeDetector` is absent on
 * Windows desktop Chrome and in Firefox, and `getUserMedia` is absent without
 * TLS — so on the machine most likely to be sitting at a reception counter,
 * this button simply is not there. That is deliberate: a button that opens a
 * dialog to explain its own impossibility teaches staff to ignore buttons.
 */
export function CameraScanButton() {
  const t = useTranslations("scan");
  const { resolve } = useResolveScan();

  const supported = useSyncExternalStore(subscribeNever, readSupport, unsupportedOnServer);

  const [open, setOpen] = useState(false);
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
     */
    const stop = () => {
      stopped = true;
      if (timer !== undefined) clearInterval(timer);
      stream?.getTracks().forEach((track) => track.stop());
      video.srcObject = null;
    };

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
      stop();
      setOpen(false);
      resolve(found);
    };

    void (async () => {
      const detector = newQrDetector(window);
      if (detector === null) {
        setFailure("failed");
        return;
      }

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
      } catch (error) {
        // The dialog may already have closed while the permission prompt was
        // up, in which case the rejection is our own teardown and not news.
        if (!stopped) setFailure(cameraFailureReason(error));
        stop();
        return;
      }

      timer = setInterval(() => void scanFrame(detector), POLL_MS);
    })();

    return stop;
  }, [open, attached, resolve]);

  if (!supported) return null;

  return (
    <>
      <Button
        variant="outline"
        className="h-tap"
        onClick={() => {
          setFailure(null);
          setOpen(true);
        }}
      >
        <ScanLine aria-hidden className="size-4" />
        {t("button")}
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("title")}</DialogTitle>
            <DialogDescription>{failure === null ? t("hint") : t(failure)}</DialogDescription>
          </DialogHeader>

          {failure === null ? (
            <video
              ref={attachVideo}
              data-testid="scan-preview"
              // Muted and inline are what let a browser start playback without
              // a second gesture; iOS refuses to play inline without both.
              muted
              playsInline
              className="bg-muted aspect-[4/3] w-full rounded-lg object-cover"
            />
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
