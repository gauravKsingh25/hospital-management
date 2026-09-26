"use client";

import { useEffect, useRef } from "react";

import {
  isPrintable,
  isSafeScanTarget,
  isTerminator,
  looksLikeScanInProgress,
  readScan,
  type ScanKey,
} from "@/lib/scanner";

/**
 * Watch the keyboard for a barcode scanner, anywhere in the app.
 *
 * The decisions all live in `lib/scanner.ts` as pure functions; this is only
 * the plumbing that feeds them and the one side effect they imply —
 * suppressing keystrokes once a scan is recognised, so the terminator does not
 * press whatever button happens to have focus.
 *
 * Listening in the **capture** phase for that reason: by the bubble phase a
 * focused control has already acted on the key.
 */
export function useScanner(onScan: (uhid: string) => void, enabled = true): void {
  // The callback is read through a ref so a caller passing an inline function
  // does not detach and reattach the listener on every render — which would
  // drop a buffer mid-scan.
  //
  // Assigned in an effect rather than during render: writing a ref while
  // rendering is what the React Compiler forbids, and rightly — a render can
  // be thrown away and re-run, and this one has a listener depending on it.
  const handler = useRef(onScan);
  useEffect(() => {
    handler.current = onScan;
  }, [onScan]);

  useEffect(() => {
    if (!enabled) return;

    let buffer: ScanKey[] = [];
    let suppressing = false;

    const reset = () => {
      buffer = [];
      suppressing = false;
    };

    const onKeyDown = (event: KeyboardEvent) => {
      // A scanner sends plain characters. Anything with a modifier is a person
      // using a shortcut, and Ctrl+K in the middle of a burst is not a burst.
      if (event.ctrlKey || event.metaKey || event.altKey) return reset();

      // The strong guard: never active while a text control has focus.
      if (!isSafeScanTarget(document.activeElement)) return reset();

      if (isTerminator(event.key)) {
        const scanned = readScan(buffer);
        const wasSuppressing = suppressing;
        reset();

        if (scanned !== null) {
          event.preventDefault();
          event.stopPropagation();
          handler.current(scanned);
        } else if (wasSuppressing) {
          // Fast enough to have been suppressing, but the payload was not one
          // of ours. Swallow the terminator anyway: the characters never
          // reached the page, so letting the Enter through alone would fire a
          // button with none of the input that was meant to precede it.
          event.preventDefault();
          event.stopPropagation();
        }
        return;
      }

      if (!isPrintable(event.key)) return reset();

      buffer.push({ key: event.key, at: event.timeStamp });

      if (!suppressing && looksLikeScanInProgress(buffer)) suppressing = true;
      if (suppressing) {
        event.preventDefault();
        event.stopPropagation();
      }
    };

    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [enabled]);
}
