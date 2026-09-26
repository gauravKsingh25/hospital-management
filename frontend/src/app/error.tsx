"use client";

import { useTranslations } from "next-intl";
import { useEffect } from "react";
import { AlertOctagon } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * The last line of defence.
 *
 * Deliberately says nothing about what went wrong. A stack trace on a counter
 * screen is no use to the receptionist reading it and is a gift to anyone
 * else standing at that counter — the backend takes the same position (see
 * its unhandled-exception handler, which logs the detail and returns a
 * generic message).
 *
 * `reset()` re-renders the failed segment without a full reload, so a
 * transient failure costs a click rather than the page state.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const t = useTranslations("errors");

  useEffect(() => {
    // The digest correlates this screen with the server log entry, which is
    // the only way to connect "it broke" to what actually happened.
    console.error("unhandled error", error.digest ?? error.message);
  }, [error]);

  return (
    <div className="flex min-h-96 flex-col items-center justify-center gap-4 px-4 text-center">
      <div className="bg-critical/10 text-critical flex size-12 items-center justify-center rounded-full">
        <AlertOctagon aria-hidden className="size-6" />
      </div>
      <div>
        <h1 className="text-lg font-semibold">{t("title")}</h1>
        {error.digest ? (
          <p className="text-muted-foreground mt-1 font-mono text-xs">{error.digest}</p>
        ) : null}
      </div>
      <Button onClick={reset} className="h-tap">
        {t("goHome")}
      </Button>
    </div>
  );
}
