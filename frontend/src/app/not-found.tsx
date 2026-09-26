import { getTranslations } from "next-intl/server";
import { FileQuestion } from "lucide-react";

import { LinkButton } from "@/components/ui/link-button";

export default async function NotFound() {
  const t = await getTranslations("errors");

  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-4 px-4 text-center">
      <div className="bg-muted text-muted-foreground flex size-12 items-center justify-center rounded-full">
        <FileQuestion aria-hidden className="size-6" />
      </div>
      <div>
        <h1 className="text-lg font-semibold">{t("notFound")}</h1>
        <p className="text-muted-foreground mt-1 text-sm">{t("notFoundHint")}</p>
      </div>
      <LinkButton className="h-tap" href="/">
        {t("goHome")}
      </LinkButton>
    </main>
  );
}
