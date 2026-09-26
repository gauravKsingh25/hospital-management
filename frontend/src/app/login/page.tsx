import { getTranslations } from "next-intl/server";
import { Activity } from "lucide-react";

import { LoginForm } from "@/features/auth/login-form";
import { LocaleSwitcher } from "@/components/locale-switcher";

export async function generateMetadata() {
  const t = await getTranslations("auth");
  return { title: t("signIn") };
}

/**
 * The sign-in screen.
 *
 * `next` is carried through so a user who was sent here mid-task returns to
 * where they were rather than to a home screen — the difference between
 * "sign in again" and "sign in again, then find that patient again".
 */
export default async function LoginPage({ searchParams }: PageProps<"/login">) {
  const t = await getTranslations("auth");
  const app = await getTranslations("app");
  const { next } = await searchParams;

  return (
    <main className="flex min-h-svh flex-col items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <div className="bg-primary text-primary-foreground flex size-12 items-center justify-center rounded-xl">
            <Activity aria-hidden className="size-6" />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">{app("name")}</h1>
          <div>
            <p className="text-base font-medium">{t("welcome")}</p>
            <p className="text-muted-foreground mt-1 text-sm text-balance">{t("subtitle")}</p>
          </div>
        </div>

        <div className="bg-card rounded-xl border p-6 shadow-sm">
          <LoginForm next={typeof next === "string" ? next : "/"} />
        </div>

        {/* Before signing in, not after: someone who cannot read the English
            login screen cannot find a language menu inside the app. */}
        <div className="mt-6 flex justify-center">
          <LocaleSwitcher />
        </div>
      </div>
    </main>
  );
}
