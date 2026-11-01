"use client";

import { FileDown, Printer } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { billPath, printInPlace } from "@/features/billing/print/print-in-place";

/** Statuses a bill can be corrected from: issued, whatever has been paid. */
export const CORRECTABLE = new Set(["ISSUED", "PARTIALLY_PAID", "PAID"]);

/** Print and PDF for one bill, both in place — the page does not change. */
export function PrintActions({ invoiceId, number }: { invoiceId: string; number: string }) {
  const accounts = useTranslations("accounts");
  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className="h-tap"
        aria-label={accounts("downloadBill", { number })}
        onClick={() => {
          printInPlace(billPath(invoiceId));
          toast.info(accounts("pdfHint"));
        }}
      >
        <FileDown aria-hidden />
        {accounts("pdf")}
      </Button>
      <Button
        size="sm"
        className="h-tap"
        aria-label={accounts("printBill", { number })}
        onClick={() => printInPlace(billPath(invoiceId))}
      >
        <Printer aria-hidden />
        {accounts("print")}
      </Button>
    </>
  );
}
