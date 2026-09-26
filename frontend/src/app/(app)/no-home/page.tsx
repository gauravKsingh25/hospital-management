import { getTranslations } from "next-intl/server";
import { UserCog } from "lucide-react";

import { getCurrentUser } from "@/lib/api/server";

/**
 * Where a user with no usable role lands.
 *
 * This happens: an account is created before its roles are assigned, or a
 * role is edited and leaves someone holding nothing. Redirecting them in a
 * loop, or dropping them on an empty dashboard, produces a support call that
 * begins "it's broken". Naming the actual problem produces one that begins
 * "can you give me the receptionist role", which somebody can act on.
 */
export default async function NoHomePage() {
  const t = await getTranslations("home");
  const user = await getCurrentUser();

  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-4 py-16 text-center">
      <div className="bg-muted text-muted-foreground flex size-12 items-center justify-center rounded-full">
        <UserCog aria-hidden className="size-6" />
      </div>
      <div>
        <h1 className="text-lg font-semibold">{t("noHome")}</h1>
        <p className="text-muted-foreground mt-1 text-sm">{t("noHomeHint")}</p>
      </div>
      <p className="text-muted-foreground text-xs">
        {user.email}
        {user.roles.length > 0 ? ` · ${user.roles.join(", ")}` : ""}
      </p>
    </div>
  );
}
