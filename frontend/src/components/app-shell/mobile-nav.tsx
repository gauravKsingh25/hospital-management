"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Menu } from "lucide-react";

import { visibleNavItems } from "@/components/app-shell/nav-items";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

/**
 * Navigation on a tablet or phone, where the sidebar is hidden.
 *
 * Ward rounds happen on tablets, so this is not a nice-to-have. The items are
 * the same data the sidebar uses, filtered by the same permissions — one list,
 * two presentations.
 */
export function MobileNav({ permissions }: { permissions: readonly string[] }) {
  const t = useTranslations("nav");
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const items = visibleNavItems(permissions);

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="size-tap md:hidden"
        onClick={() => setOpen(true)}
        aria-label={t("openMenu")}
      >
        <Menu aria-hidden className="size-5" />
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-xs">
          <DialogHeader>
            <DialogTitle>{t("openMenu")}</DialogTitle>
            <DialogDescription className="sr-only">{t("openMenu")}</DialogDescription>
          </DialogHeader>
          <ul className="flex flex-col gap-0.5">
            {items.map((item) => {
              const active = pathname === item.href;
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    // Closed here rather than in an effect watching the path.
                    // An effect that calls setState on every navigation is a
                    // cascading render, and this is a direct consequence of
                    // the tap — the menu should shut because the user chose
                    // something, not because a route happened to change.
                    onClick={() => setOpen(false)}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "flex min-h-tap items-center gap-3 rounded-lg px-3 text-sm font-medium",
                      active ? "bg-accent text-accent-foreground" : "hover:bg-accent/50",
                    )}
                  >
                    <item.icon aria-hidden className="size-4.5 shrink-0" />
                    {t(item.labelKey)}
                  </Link>
                </li>
              );
            })}
          </ul>
        </DialogContent>
      </Dialog>
    </>
  );
}
