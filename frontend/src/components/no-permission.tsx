import { getTranslations } from "next-intl/server";
import { ShieldOff } from "lucide-react";

import { LinkButton } from "@/components/ui/link-button";

/**
 * Shown when a screen exists but this role may not open it.
 *
 * Rendered rather than thrown. Next's `forbidden()` is still behind the
 * `authInterrupts` flag, and more importantly a 403 page loses the shell —
 * so a user who lands here by following a stale bookmark would also lose the
 * navigation that would take them somewhere useful.
 *
 * The message says what to do next. "Access denied" leaves someone stuck;
 * "ask an administrator to change your role" is an action.
 */
export async function NoPermission() {
  const t = await getTranslations("errors");

  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-4 py-16 text-center">
      <div className="bg-muted text-muted-foreground flex size-12 items-center justify-center rounded-full">
        <ShieldOff aria-hidden className="size-6" />
      </div>
      <div>
        <h1 className="text-lg font-semibold">{t("forbidden")}</h1>
        <p className="text-muted-foreground mt-1 text-sm">{t("forbiddenHint")}</p>
      </div>
      <LinkButton variant="outline" className="h-tap" href="/">
        {t("goHome")}
      </LinkButton>
    </div>
  );
}
