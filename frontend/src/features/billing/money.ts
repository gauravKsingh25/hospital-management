/**
 * Rendering money.
 *
 * Amounts arrive as decimal *strings* (the backend serialises `Decimal`, never
 * a float, precisely so `1234.56` does not reach a patient as
 * `1234.5600000000001`). They stay strings until the last possible moment —
 * this function — and the conversion here is for display only. Nothing in the
 * frontend does arithmetic on money: totals, tax and balances are computed
 * server-side, where the rounding rules live.
 */

const FORMATTER = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function formatMoney(amount: string | number | null | undefined): string {
  if (amount === null || amount === undefined) return "—";
  const value = typeof amount === "number" ? amount : Number(amount);
  // A malformed amount is shown verbatim rather than as ₹NaN: a cashier
  // seeing the raw value can at least report what the screen said.
  if (!Number.isFinite(value)) return String(amount);
  return FORMATTER.format(value);
}

/** True when a decimal string is greater than zero. */
export function isPositive(amount: string | null | undefined): boolean {
  if (amount === null || amount === undefined) return false;
  const value = Number(amount);
  return Number.isFinite(value) && value > 0;
}
