import type { Permission } from "@/types/rbac.generated";

/**
 * Keyboard shortcuts, as data (CLAUDE.md §7b: "configurable, not hardcoded").
 *
 * Defining them as a list rather than as `if (event.key === "F2")` scattered
 * through components buys three things: the help dialog is generated from the
 * same source that binds them, so it can never be out of date; a hospital can
 * be given an override without a code change; and a conflict is visible in one
 * place rather than discovered by a user whose key does two things.
 *
 * ## Why function keys
 *
 * They are what the paper-and-DOS-era software this replaces used, and what
 * long-serving hospital staff already have in their fingers. F2 for "new" is
 * three decades of muscle memory in Indian counter software — worth far more
 * than a more fashionable chord nobody knows.
 *
 * The browser reserves some: F5, F6, F11, F12, Ctrl+N, Ctrl+T, Ctrl+W. None
 * are used here.
 */

export type ShortcutAction =
  | { kind: "navigate"; href: string }
  | { kind: "search" }
  | { kind: "help" };

export type Shortcut = {
  id: string;
  /** `KeyboardEvent.key`, matched case-insensitively. */
  key: string;
  ctrl?: boolean;
  alt?: boolean;
  shift?: boolean;
  /** Indexes the `shortcuts` message namespace. */
  labelKey: string;
  action: ShortcutAction;
  /** Hidden when the user holds none of these. Empty means always shown. */
  permissions?: Permission[];
};

export const DEFAULT_SHORTCUTS: Shortcut[] = [
  {
    id: "new-patient",
    key: "F2",
    labelKey: "newPatient",
    action: { kind: "navigate", href: "/reception/register" },
    permissions: ["patient:create"],
  },
  {
    id: "search",
    key: "F3",
    labelKey: "search",
    action: { kind: "search" },
    permissions: ["patient:read"],
  },
  {
    id: "billing",
    key: "F4",
    labelKey: "queue",
    action: { kind: "navigate", href: "/queue" },
    permissions: ["queue:read"],
  },
  {
    id: "help",
    key: "?",
    shift: true,
    labelKey: "showShortcuts",
    action: { kind: "help" },
  },
];

/** Render a shortcut for display: `Ctrl + Enter`, `F2`, `Shift + ?`. */
export function formatShortcut(shortcut: Shortcut): string {
  const parts: string[] = [];
  if (shortcut.ctrl) parts.push("Ctrl");
  if (shortcut.alt) parts.push("Alt");
  if (shortcut.shift && shortcut.key !== "?") parts.push("Shift");
  parts.push(shortcut.key);
  return parts.join(" + ");
}

export function matchesShortcut(event: KeyboardEvent, shortcut: Shortcut): boolean {
  return (
    event.key.toLowerCase() === shortcut.key.toLowerCase() &&
    event.ctrlKey === Boolean(shortcut.ctrl) &&
    event.altKey === Boolean(shortcut.alt) &&
    // Shift is checked loosely for `?`, which requires Shift on most layouts
    // but not all — an Indian INSCRIPT keyboard produces it differently.
    (shortcut.key === "?" || event.shiftKey === Boolean(shortcut.shift))
  );
}

/**
 * True when the key press belongs to whatever the user is typing into.
 *
 * Without this, F2 inside a "reason for visit" textarea would navigate away
 * and discard what they had written. Function keys are safe in inputs, so
 * they are still allowed through — it is the printable characters like `?`
 * that must not steal focus.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable ||
    target.getAttribute("role") === "textbox"
  );
}
