/**
 * Recognising a barcode scanner from its keystrokes.
 *
 * A counter scanner is a keyboard. It types the payload and sends a
 * terminator, and there is no API that says "this came from a scanner" — the
 * only signal is *how fast* the characters arrive. This module turns that
 * signal into a decision, as pure functions over a key sequence, so the rules
 * can be tested exhaustively without a browser.
 *
 * ## Why this is the riskiest piece of the QR feature
 *
 * A global key handler that guesses wrong swallows real typing. Getting it
 * wrong in a hospital means a doctor's note loses a sentence, which is worse
 * than the problem this solves. So there are **two independent guards**, and
 * either one alone would be enough:
 *
 * 1. **Where.** Never active while a text control has focus. A note, a vitals
 *    field, the search box — all left completely alone. That alone makes the
 *    dangerous case impossible.
 * 2. **How fast.** A median gap under 30 ms between characters. The world
 *    record for sustained human typing is around 100 ms per character; a
 *    scanner sits near 5–15 ms. There is no overlap.
 *
 * Then a third, cheaper check: the payload has to look like one of our UHIDs.
 * Anything else is discarded in silence.
 */

/** `CODE-YY-NNNNNN` — the shape `allocate_uhid` produces. */
const UHID_PATTERN = /^[A-Z0-9]{2,12}-\d{2}-\d{6}$/;

/**
 * Characters below which a burst is not worth considering. A UHID is fourteen;
 * eight leaves room for a shorter hospital code without admitting the
 * two-and-three-character sequences that ordinary keyboard use produces.
 */
const MIN_LENGTH = 8;

/**
 * The discriminator. Human typing is an order of magnitude slower even at
 * professional speed, so this is not a close call — which is the point. A
 * threshold that needed tuning would be a threshold that eventually misfires.
 */
const MAX_MEDIAN_GAP_MS = 30;

/**
 * A backstop against a stale buffer, not a real constraint: fourteen
 * characters at the gap above take about 400 ms. It exists so that keys typed
 * minutes apart cannot accumulate into something that looks like a scan.
 */
const MAX_TOTAL_MS = 1_000;

/**
 * How many characters it takes before we are willing to suppress keystrokes.
 * Below this the evidence is too thin; above it, a scan is in progress and its
 * terminator must not reach the page — an `Enter` arriving at a focused button
 * would otherwise press it.
 */
export const SUPPRESS_AFTER = 4;

/** Terminators scanners send. Most are configurable and ship with one of these. */
const TERMINATORS = new Set(["Enter", "Tab"]);

export type ScanKey = {
  /** `KeyboardEvent.key`. */
  key: string;
  /** `event.timeStamp`, or any monotonic millisecond clock. */
  at: number;
};

export function isTerminator(key: string): boolean {
  return TERMINATORS.has(key);
}

/** A single printable character — what a scanner emits, one key at a time. */
export function isPrintable(key: string): boolean {
  return key.length === 1;
}

/**
 * Whether a key sequence arrived faster than a person can type.
 *
 * The **median** gap rather than the mean: one long pause — the operating
 * system scheduling something, a garbage collection — would drag a mean over
 * the threshold and lose a real scan. A median tolerates a couple of outliers
 * in a fourteen-character burst, which is exactly the noise that occurs.
 */
export function isMachineSpeed(keys: readonly ScanKey[]): boolean {
  if (keys.length < 2) return false;

  const gaps: number[] = [];
  for (let index = 1; index < keys.length; index += 1) {
    gaps.push(keys[index].at - keys[index - 1].at);
  }
  gaps.sort((a, b) => a - b);

  const middle = Math.floor(gaps.length / 2);
  const median =
    gaps.length % 2 === 0 ? (gaps[middle - 1] + gaps[middle]) / 2 : gaps[middle];

  return median < MAX_MEDIAN_GAP_MS;
}

/**
 * True once a partial buffer is convincing enough to stop passing keys on.
 *
 * Deliberately decided *before* the terminator arrives. Waiting for it would
 * mean the terminator itself reaches the page, and `Enter` on whatever holds
 * focus is a click nobody asked for.
 */
export function looksLikeScanInProgress(keys: readonly ScanKey[]): boolean {
  return keys.length >= SUPPRESS_AFTER && isMachineSpeed(keys);
}

/**
 * The buffered characters as a scanned UHID, or `null` if they are not one.
 *
 * Takes the character keys only — the caller strips the terminator, because
 * only the caller knows whether one arrived.
 */
export function readScan(keys: readonly ScanKey[]): string | null {
  if (keys.length < MIN_LENGTH) return null;
  if (keys[keys.length - 1].at - keys[0].at > MAX_TOTAL_MS) return null;
  if (!isMachineSpeed(keys)) return null;

  return normaliseUhid(keys.map((entry) => entry.key).join(""));
}

/**
 * A raw payload as a UHID, or `null` if it is not one.
 *
 * Shared by both scanning paths, and that is the point of it existing
 * separately: the counter scanner sends keystrokes and the camera sends a
 * decoded string, but what counts as a card must not depend on which device
 * read it. One definition, one regex, one place to change if the UHID format
 * ever does.
 *
 * Uppercased because a scanner configured with a lower-case keyboard layout
 * is a real thing, and the UHID is case-insensitive by construction.
 */
export function normaliseUhid(raw: string): string | null {
  const value = raw.trim().toUpperCase();
  return UHID_PATTERN.test(value) ? value : null;
}

/**
 * Whether it is safe to watch keystrokes at all, given what has focus.
 *
 * The strong guard. A scan into a focused text field already works without
 * any of this — the characters simply land in the field — so there is nothing
 * to gain by intercepting there, and a great deal to lose: this is the only
 * code in the system that could swallow part of a clinical note.
 *
 * `isContentEditable` covers rich-text editors; the tag checks cover
 * everything else. Buttons and links are *not* excluded, because those are
 * where focus usually sits when somebody reaches for a scanner, and they are
 * also the reason keystrokes get suppressed once a scan is recognised.
 *
 * The content-editable check reads the property rather than testing
 * `instanceof HTMLElement`: that constructor does not exist outside a browser,
 * so the `instanceof` form threw when the tests called this without a DOM.
 * Nothing here needs a DOM to answer, and now nothing here requires one.
 */
export function isSafeScanTarget(element: Element | null): boolean {
  if (element === null) return true;

  const tag = element.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return false;
  if ((element as Partial<HTMLElement>).isContentEditable === true) return false;

  return true;
}
