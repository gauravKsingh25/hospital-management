"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DEFAULT_SHORTCUTS,
  formatShortcut,
  isTypingTarget,
  matchesShortcut,
} from "@/lib/shortcuts";

/**
 * Binds the global shortcuts and renders the help dialog.
 *
 * Mounted once in the app shell. Both the bindings and the help list come
 * from `DEFAULT_SHORTCUTS`, so the dialog cannot describe a key that does
 * something else — a stale shortcut list is worse than no list, because
 * somebody will trust it.
 *
 * Search (F3) is bound by `UniversalSearch` itself, which owns the dialog
 * state; this component skips it rather than duplicating that wiring.
 */
export function KeyboardShortcuts({ permissions }: { permissions: readonly string[] }) {
  const t = useTranslations("shortcuts");
  const router = useRouter();
  const [helpOpen, setHelpOpen] = useState(false);

  const held = new Set(permissions);
  const available = DEFAULT_SHORTCUTS.filter(
    (shortcut) =>
      !shortcut.permissions || shortcut.permissions.some((permission) => held.has(permission)),
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // A printable-character shortcut must never fire while somebody is
      // writing a clinical note. Function keys are safe and stay live, which
      // is most of why §7b chose them.
      const typing = isTypingTarget(event.target);

      for (const shortcut of available) {
        if (shortcut.action.kind === "search") continue;
        if (!matchesShortcut(event, shortcut)) continue;
        if (typing && shortcut.key.length === 1) continue;

        event.preventDefault();
        if (shortcut.action.kind === "navigate") router.push(shortcut.action.href);
        if (shortcut.action.kind === "help") setHelpOpen((previous) => !previous);
        return;
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [available, router]);

  return (
    <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>
        <ul className="divide-border divide-y">
          {available.map((shortcut) => (
            <li key={shortcut.id} className="flex items-center justify-between gap-4 py-2.5">
              <span className="text-sm">{t(shortcut.labelKey)}</span>
              <kbd className="bg-muted rounded border px-2 py-1 text-xs font-medium whitespace-nowrap">
                {formatShortcut(shortcut)}
              </kbd>
            </li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
