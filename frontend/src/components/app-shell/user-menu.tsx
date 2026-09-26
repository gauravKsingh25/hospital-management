"use client";

import { useTranslations } from "next-intl";
import { useTransition } from "react";
import { LogOut, User } from "lucide-react";

import { signOut } from "@/features/auth/actions";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { CurrentUser } from "@/types/api";

/**
 * Who is signed in, and how to stop being signed in.
 *
 * The roles are shown, not just the name. On a shared counter machine the
 * question "whose session is this?" comes up constantly, and an answer that
 * takes two clicks to find is an answer nobody checks before recording
 * something under someone else's account.
 */
export function UserMenu({ user }: { user: CurrentUser }) {
  const t = useTranslations("common");
  const [pending, startTransition] = useTransition();

  const initials = user.full_name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button variant="ghost" className="h-tap gap-2 px-2" aria-label={user.full_name}>
            <span className="bg-primary/10 text-primary flex size-8 shrink-0 items-center justify-center rounded-full text-xs font-semibold">
              {initials || <User aria-hidden className="size-4" />}
            </span>
            <span className="hidden max-w-32 truncate text-sm font-medium sm:inline">
              {user.full_name}
            </span>
          </Button>
        }
      />
      <DropdownMenuContent align="end" className="w-60">
        {/* The label must live inside a group: Base UI's `Menu.GroupLabel`
            reads its context from `Menu.Group` and throws without one. */}
        <DropdownMenuGroup>
          <DropdownMenuLabel className="font-normal">
            <p className="truncate text-sm font-medium">{user.full_name}</p>
            <p className="text-muted-foreground truncate text-xs">{user.email}</p>
            <p className="text-muted-foreground mt-1 text-xs">
              {user.roles.map((role) => role.replaceAll("_", " ").toLowerCase()).join(", ")}
            </p>
          </DropdownMenuLabel>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          disabled={pending}
          // A server action, so the refresh token is revoked on the backend
          // rather than just forgotten in this browser. On a shared machine,
          // forgetting is not signing out.
          onClick={() => startTransition(() => void signOut())}
        >
          <LogOut aria-hidden className="size-4" />
          {t("signOut")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
