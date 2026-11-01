import { getTranslations } from "next-intl/server";

import { TwoFactorSettings } from "@/features/auth/two-factor-settings";
import { getCurrentUser } from "@/lib/api/server";

export async function generateMetadata() {
  const t = await getTranslations("auth");
  return { title: t("twoFactorSettings") };
}

/** Your own two-step sign-in (SEC-05). Every signed-in person may open it. */
export default async function TwoFactorAccountPage() {
  const t = await getTranslations("auth");
  const user = await getCurrentUser();

  return (
    <div className="mx-auto max-w-xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">{t("twoFactorSettings")}</h1>
        <p className="text-muted-foreground mt-1 text-sm">{t("twoFactorSettingsHint")}</p>
      </header>
      <section className="rounded-lg border p-5">
        <TwoFactorSettings
          enabled={user.two_factor_enabled}
          required={user.two_factor_required ?? false}
        />
      </section>
    </div>
  );
}
