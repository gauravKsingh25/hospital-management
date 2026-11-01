"use client";

import { Printer } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect } from "react";

import { Button } from "@/components/ui/button";

/**
 * Print, or Save as PDF from the same dialog — no PDF library involved.
 * `autoPrint` is the `?print=1` the dashboard's PDF and Print links carry.
 */
export function PrintButton({ autoPrint, title }: { autoPrint: boolean; title?: string }) {
  const t = useTranslations("bill");
  useEffect(() => {
    // "Save as PDF" names the file after the page title: the bill number.
    if (title) document.title = title;
  }, [title]);
  useEffect(() => {
    if (!autoPrint) return;
    // After the logo has had a moment to load, so it is on the page printed.
    const timer = setTimeout(() => window.print(), 600);
    return () => clearTimeout(timer);
  }, [autoPrint]);

  return (
    <Button className="h-tap" onClick={() => window.print()}>
      <Printer aria-hidden />
      {t("print")}
    </Button>
  );
}
