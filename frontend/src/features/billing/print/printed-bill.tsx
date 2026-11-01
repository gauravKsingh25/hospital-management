import { getFormatter, getTranslations } from "next-intl/server";

import { amountInWords } from "@/features/billing/print/amount-in-words";
import { formatMoney, isPositive } from "@/features/billing/money";
import type { Charge, PrintableBill } from "@/types/api";

/**
 * The bill as a document (spec §5.7) — one component for OPD and IPD, final
 * and interim. A server component: nothing on paper needs JavaScript.
 *
 * The letterhead is the one snapshotted when the bill was issued, so a
 * reprint shows the hospital as it was that day; an interim bill uses the
 * profile as it is now. Every number is the server's.
 */
type Letterhead = {
  name?: string | null;
  legal_name?: string | null;
  address_line1?: string | null;
  address_line2?: string | null;
  city?: string | null;
  state?: string | null;
  pincode?: string | null;
  phone?: string | null;
  email?: string | null;
  gstin?: string | null;
  hfr_facility_id?: string | null;
  logo_version?: number | null;
};

const percent = (value: string) => `${Number(value)}%`;

export async function PrintedBill({
  bill,
  hospitalId,
}: {
  bill: PrintableBill;
  hospitalId: string;
}) {
  const t = await getTranslations("bill");
  const categories = await getTranslations("categories");
  const format = await getFormatter();
  const head = bill.letterhead as Letterhead;
  const day = (value: string | null | undefined) =>
    value ? format.dateTime(new Date(value), { day: "2-digit", month: "short", year: "numeric" }) : "—";
  const moment = (value: string) =>
    format.dateTime(new Date(value), {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  const interstate = isPositive(bill.totals.igst_total);
  const place = [head.address_line1, head.address_line2, head.city, head.state, head.pincode]
    .filter(Boolean)
    .join(", ");

  const groups = new Map<string, Charge[]>();
  for (const line of bill.lines) groups.set(line.category, [...(groups.get(line.category) ?? []), line]);
  const columns = interstate ? 10 : 11;

  return (
    <article
      className="mx-auto max-w-[210mm] space-y-5 p-6 text-[13px] leading-snug print:p-0"
      data-testid="printed-bill"
    >
      <header className="flex items-start gap-4 border-b-2 border-black pb-3" data-testid="letterhead">
        {head.logo_version ? (
          // eslint-disable-next-line @next/next/no-img-element -- a tenant-private stored file
          <img
            src={`/bff/hospitals/${hospitalId}/logo?version=${head.logo_version}`}
            alt={t("logoAlt")}
            className="max-h-20 max-w-48 object-contain"
          />
        ) : null}
        <div className="flex-1">
          <p className="text-xl font-bold">{head.legal_name || head.name}</p>
          {head.legal_name && head.name && head.legal_name !== head.name ? (
            <p className="text-sm">{head.name}</p>
          ) : null}
          {place ? <p>{place}</p> : null}
          <p>
            {[head.phone && `${t("phone")} ${head.phone}`, head.email].filter(Boolean).join(" · ")}
          </p>
          <p className="font-medium">
            {[head.gstin && `GSTIN ${head.gstin}`, head.hfr_facility_id && `HFR ${head.hfr_facility_id}`]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
      </header>

      <div className="text-center">
        <h1 className="text-lg font-bold tracking-wide">{bill.title}</h1>
        {bill.status === "CANCELLED" ? <p className="font-bold">{t("cancelled")}</p> : null}
      </div>

      <section className="grid grid-cols-2 gap-x-6 gap-y-1">
        <dl className="space-y-0.5">
          <Row label={t("billNo")} value={bill.number ?? t("notIssued")} />
          <Row label={t("date")} value={day(bill.date)} />
          {bill.place_of_supply ? <Row label={t("placeOfSupply")} value={bill.place_of_supply} /> : null}
        </dl>
        <dl className="space-y-0.5">
          <Row label={t("patient")} value={bill.patient.name} />
          <Row label="UHID" value={bill.patient.uhid} />
          <Row
            label={t("ageSex")}
            value={[bill.patient.age_years ?? null, bill.patient.gender?.[0]].filter((v) => v !== null).join(" / ")}
          />
          {bill.patient.phone ? <Row label={t("phone")} value={bill.patient.phone} /> : null}
        </dl>
        {bill.stay ? (
          <dl className="col-span-2 mt-1 grid grid-cols-2 gap-x-6 gap-y-0.5 border-t pt-1">
            <Row label={t("admissionNo")} value={bill.stay.admission_number} />
            <Row label={t("wardBed")} value={bill.stay.ward_bed ?? "—"} />
            <Row label={t("admitted")} value={day(bill.stay.admitted_at)} />
            <Row label={t("discharged")} value={day(bill.stay.discharged_at)} />
            {bill.stay.attending_doctor ? (
              <Row label={t("doctor")} value={bill.stay.attending_doctor} />
            ) : null}
          </dl>
        ) : null}
      </section>

      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr className="border-y border-black text-left">
            <th scope="col" className="py-1 pr-2">{t("description")}</th>
            <th scope="col" className="py-1 pr-2">SAC/HSN</th>
            <th scope="col" className="py-1 pr-2 text-right">{t("qty")}</th>
            <th scope="col" className="py-1 pr-2 text-right">{t("rate")}</th>
            <th scope="col" className="py-1 pr-2 text-right">{t("discount")}</th>
            <th scope="col" className="py-1 pr-2 text-right">Taxable</th>
            <th scope="col" className="py-1 pr-2 text-right">GST %</th>
            {interstate ? (
              <th scope="col" className="py-1 pr-2 text-right">IGST</th>
            ) : (
              <>
                <th scope="col" className="py-1 pr-2 text-right">CGST</th>
                <th scope="col" className="py-1 pr-2 text-right">SGST</th>
              </>
            )}
            <th scope="col" className="py-1 text-right">{t("amount")}</th>
          </tr>
        </thead>
        {[...groups.entries()].map(([category, lines]) => (
          <tbody key={category}>
            <tr>
              <th scope="rowgroup" colSpan={columns} className="pt-2 text-left text-[11px] font-bold uppercase">
                {categories(category as "OTHER")}
              </th>
            </tr>
            {lines.map((line) => {
              const waived = line.status === "WAIVED";
              return (
                <tr key={line.id} className="border-b border-gray-300 align-top">
                  <td className="py-1 pr-2">
                    {line.description}
                    {waived ? <span className="font-medium"> — {t("waived")}</span> : null}
                  </td>
                  <td className="py-1 pr-2">{line.hsn_sac_code ?? ""}</td>
                  <td className="tabular py-1 pr-2 text-right">{Number(line.quantity)}</td>
                  <td className="tabular py-1 pr-2 text-right">{formatMoney(line.unit_price)}</td>
                  <td className="tabular py-1 pr-2 text-right">{formatMoney(line.discount_amount)}</td>
                  <td className="tabular py-1 pr-2 text-right">{formatMoney(line.taxable_amount)}</td>
                  <td className="tabular py-1 pr-2 text-right">{percent(line.gst_rate)}</td>
                  {interstate ? (
                    <td className="tabular py-1 pr-2 text-right">{formatMoney(line.igst_amount)}</td>
                  ) : (
                    <>
                      <td className="tabular py-1 pr-2 text-right">{formatMoney(line.cgst_amount)}</td>
                      <td className="tabular py-1 pr-2 text-right">{formatMoney(line.sgst_amount)}</td>
                    </>
                  )}
                  <td className={`tabular py-1 text-right ${waived ? "line-through" : ""}`}>
                    {formatMoney(line.total_amount)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        ))}
      </table>

      <section className="ml-auto w-72 space-y-0.5">
        <Total label={t("taxableTotal")} value={bill.totals.taxable_total} />
        {interstate ? (
          <Total label="IGST" value={bill.totals.igst_total} />
        ) : (
          <>
            <Total label="CGST" value={bill.totals.cgst_total} />
            <Total label="SGST" value={bill.totals.sgst_total} />
          </>
        )}
        {isPositive(bill.totals.discount_total) ? (
          <Total label={t("discountTotal")} value={bill.totals.discount_total} />
        ) : null}
        {bill.totals.round_off !== "0.00" ? <Total label={t("roundOff")} value={bill.totals.round_off} /> : null}
        <div className="flex justify-between border-t border-black pt-1 text-base font-bold">
          <span>{t("grandTotal")}</span>
          <span className="tabular">{formatMoney(bill.totals.grand_total)}</span>
        </div>
        <Total label={t("paid")} value={bill.totals.paid} />
        <div className="flex justify-between font-bold">
          <span>{t("balance")}</span>
          <span className="tabular">{formatMoney(bill.totals.balance)}</span>
        </div>
      </section>
      <p className="font-medium">{amountInWords(bill.totals.grand_total)}</p>

      {bill.payments.length ? (
        <section data-testid="receipts">
          <h2 className="mb-1 font-bold">{t("receipts")}</h2>
          <table className="w-full text-[12px]">
            <tbody>
              {bill.payments.map((payment) => (
                <tr key={payment.id} className="border-b border-gray-300">
                  <td className="py-0.5 pr-2">{moment(payment.received_at)}</td>
                  <td className="py-0.5 pr-2">{payment.receipt_number}</td>
                  <td className="py-0.5 pr-2">{t(`method${payment.method}` as "methodCASH")}</td>
                  <td className="py-0.5 pr-2">{payment.reference ?? ""}</td>
                  <td className="tabular py-0.5 text-right">{formatMoney(payment.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      <footer className="flex items-end justify-between border-t pt-3 text-[11px]">
        <div>
          <p>{t("computerGenerated")}</p>
          <p>{t("generatedAt", { when: moment(new Date().toISOString()) })}</p>
        </div>
        <div className="text-center">
          <div className="mb-1 h-10 w-44 border-b border-black" />
          <p>{t("signatory")}</p>
        </div>
      </footer>
    </article>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2">
      <dt className="w-28 shrink-0 text-gray-600">{label}</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  );
}

function Total({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between">
      <span>{label}</span>
      <span className="tabular">{formatMoney(value)}</span>
    </div>
  );
}
