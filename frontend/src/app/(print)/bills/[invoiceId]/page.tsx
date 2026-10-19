import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { LinkButton } from "@/components/ui/link-button";
import { PrintButton } from "@/features/billing/print/print-button";
import { PrintedBill } from "@/features/billing/print/printed-bill";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { PrintableBill } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("bill");
  return { title: t("title") };
}

/** An invoice, printable. `?print=1` opens the print dialog on load. */
export default async function PrintedInvoicePage({
  params,
  searchParams,
}: {
  params: Promise<{ invoiceId: string }>;
  searchParams: Promise<{ print?: string }>;
}) {
  const t = await getTranslations("bill");
  const user = await getCurrentUser();
  if (!can(user, "invoice:read") || !user.hospital_id) return <NoPermission />;

  const [{ invoiceId }, { print }] = await Promise.all([params, searchParams]);
  const bill = await serverFetch<PrintableBill>(`/billing/invoices/${invoiceId}/printable`);

  return (
    <>
      <div className="no-print mx-auto flex max-w-[210mm] items-center justify-between gap-3 px-6 pt-4">
        <LinkButton href="/billing" variant="outline" className="h-tap">
          {t("back")}
        </LinkButton>
        <p className="text-muted-foreground text-sm">{t("pdfHint")}</p>
        <PrintButton autoPrint={print === "1"} />
      </div>
      <PrintedBill bill={bill} hospitalId={user.hospital_id} />
    </>
  );
}
