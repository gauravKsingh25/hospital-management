"use client";

import { useLocale, useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useTransition } from "react";
import { Languages } from "lucide-react";

import { LOCALE_LABELS, LOCALES, type Locale } from "@/i18n/config";
import { setLocale } from "@/features/auth/locale-actions";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";

/**
 * Language selection (CLAUDE.md §9).
 *
 * The choice is written to a cookie by a server action rather than held in
 * client state, so it survives a reload, applies to server-rendered pages,
 * and follows the user to every screen. A language picker that only affects
 * the current page is worse than none.
 */
export function LocaleSwitcher() {
  const t = useTranslations("common");
  const locale = useLocale() as Locale;
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  const choose = (next: Locale) => {
    startTransition(async () => {
      await setLocale(next);
      // The locale is resolved server-side, so the rendered output must be
      // fetched again — `refresh` re-runs the server components in place
      // rather than reloading and losing scroll position.
      router.refresh();
    });
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button variant="ghost" size="sm" disabled={pending} aria-label={t("language")}>
            <Languages aria-hidden className="size-4" />
            {LOCALE_LABELS[locale]}
          </Button>
        }
      />
      <DropdownMenuContent align="end">
        {LOCALES.map((option) => (
          <DropdownMenuItem
            key={option}
            onClick={() => choose(option)}
            // Marks the current choice for screen readers, not just visually.
            aria-current={option === locale ? "true" : undefined}
          >
            {LOCALE_LABELS[option]}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
