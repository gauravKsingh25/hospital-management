"use client";

import { useEffect, useState } from "react";

/**
 * Delay a rapidly changing value.
 *
 * Used for search-as-you-type and for the duplicate check that runs while
 * reception enters a name. Both fire on every keystroke otherwise, which on a
 * hospital's connection means a queue of requests arriving out of order — and
 * a duplicate warning that flickers in and out as older responses land after
 * newer ones.
 *
 * 250ms is roughly the gap between keystrokes for a fast typist, so a request
 * goes out when someone pauses to think rather than mid-word.
 */
export function useDebounced<T>(value: T, delayMs = 250): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    // Cleanup on every change is what makes this a debounce rather than a
    // throttle: the pending update is cancelled and rescheduled.
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}
