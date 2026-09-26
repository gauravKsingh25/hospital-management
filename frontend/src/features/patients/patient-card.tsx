"use client";

import { useTranslations } from "next-intl";
import { Printer } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * The print button, and nothing else.
 *
 * The card itself is server-rendered — see the page — so the only thing that
 * needs the browser is `window.print()`. Splitting it out this narrowly keeps
 * the QR encoder on the server for this route: a card is read by a scanner,
 * never by a script.
 */
export function PrintCardButton() {
  const t = useTranslations("card");

  return (
    <Button onClick={() => window.print()} className="h-tap no-print">
      <Printer aria-hidden className="size-4" />
      {t("print")}
    </Button>
  );
}
