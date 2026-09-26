"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { Activity } from "lucide-react";

import { visibleNavItems } from "@/components/app-shell/nav-items";
import { cn } from "@/lib/utils";

/**
 * The role's navigation.
 *
 * A client component only because it needs `usePathname` to mark the current
 * item. The permission list is a prop, resolved on the server — nothing here
 * fetches the user, and nothing decides access.
 */
export function Sidebar({ permissions }: { permissions: readonly string[] }) {
  const pathname = usePathname();
  const t = useTranslations("nav");
  const app = useTranslations("app");
  const items = visibleNavItems(permissions);

  return (
    <nav
      aria-label={t("openMenu")}
      className="bg-sidebar hidden w-60 shrink-0 flex-col border-r md:flex"
    >
      <Link
        href="/"
        className="flex h-16 items-center gap-2.5 px-5 text-base font-semibold tracking-tight"
      >
        <span className="bg-primary text-primary-foreground flex size-8 shrink-0 items-center justify-center rounded-lg">
          <Activity aria-hidden className="size-4" />
        </span>
        <span className="truncate">{app("name")}</span>
      </Link>

      <ul className="flex flex-1 flex-col gap-0.5 overflow-y-auto p-3">
        {items.map((item) => {
          const active =
            pathname === item.href || (item.prefix && pathname.startsWith(`${item.href}/`));

          return (
            <li key={item.href}>
              <Link
                href={item.href}
                // `page`, not `true` — this marks the current page, which is
                // what a screen reader announces when landing on the nav.
                aria-current={active ? "page" : undefined}
                className={cn(
                  // `min-h-tap`: the 44px floor from §7b, so this works with
                  // a finger on a counter touchscreen and not just a mouse.
                  "flex min-h-tap items-center gap-3 rounded-lg px-3 text-sm font-medium transition-colors",
                  active
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "text-sidebar-foreground/75 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground",
                )}
              >
                <item.icon aria-hidden className="size-4.5 shrink-0" />
                <span className="truncate">{t(item.labelKey)}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
