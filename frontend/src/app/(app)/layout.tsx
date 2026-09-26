import { KeyboardShortcuts } from "@/components/app-shell/keyboard-shortcuts";
import { Sidebar } from "@/components/app-shell/sidebar";
import { UniversalSearch } from "@/components/app-shell/universal-search";
import { UserMenu } from "@/components/app-shell/user-menu";
import { LocaleSwitcher } from "@/components/locale-switcher";
import { MobileNav } from "@/components/app-shell/mobile-nav";
import { getCurrentUser } from "@/lib/api/server";

/**
 * The signed-in shell.
 *
 * A server component, so the nav, the header and the user's identity render
 * as HTML with no JavaScript cost. Only the three genuinely interactive
 * pieces — search, the shortcut bindings and the user menu — are client
 * components (CLAUDE.md §4: server components by default).
 *
 * `getCurrentUser()` is called once here and the permission list passed down
 * as props. Layouts do not re-render on navigation within the group, so this
 * is one `/auth/me` per sign-in-and-navigate session rather than one per
 * page — and no component below has to fetch the user for itself.
 */
export default async function AppLayout({ children }: LayoutProps<"/">) {
  const user = await getCurrentUser();

  return (
    <div className="flex min-h-svh">
      <Sidebar permissions={user.permissions} />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="bg-background/95 supports-[backdrop-filter]:bg-background/75 sticky top-0 z-30 flex h-16 items-center gap-2 border-b px-3 backdrop-blur sm:gap-3 sm:px-5 no-print">
          <MobileNav permissions={user.permissions} />
          <UniversalSearch />
          <div className="ml-auto flex items-center gap-1">
            <LocaleSwitcher />
            <UserMenu user={user} />
          </div>
        </header>

        {/* `min-w-0` matters: without it a wide table inside a flex column
            refuses to shrink and pushes the whole page into a horizontal
            scroll, which on a counter screen hides the primary action. */}
        <main className="min-w-0 flex-1 px-3 py-5 sm:px-5 sm:py-6">{children}</main>
      </div>

      <KeyboardShortcuts permissions={user.permissions} />
    </div>
  );
}
