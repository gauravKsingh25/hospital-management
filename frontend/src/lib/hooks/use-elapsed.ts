"use client";

import { useEffect, useState } from "react";

/**
 * Whole minutes since a timestamp, ticking without a refetch.
 *
 * "Waiting 18 min" has to keep counting even when the queue itself has not
 * changed. Deriving it from the row would freeze the number between polls,
 * and polling faster just to move a clock would be a request a minute per
 * open screen for something the browser can work out itself.
 *
 * The interval is 30 seconds rather than 60: a whole-minute display that
 * updates once a minute can lag the truth by nearly a minute, which is
 * visible when someone is watching it.
 */
export function useElapsed(since: string): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);

  const started = Date.parse(since);
  if (Number.isNaN(started)) return 0;

  // Clamped at zero: a workstation clock a few minutes ahead of the server
  // would otherwise show "waiting -3 min", which reads as a bug.
  return Math.max(0, Math.floor((now - started) / 60_000));
}
