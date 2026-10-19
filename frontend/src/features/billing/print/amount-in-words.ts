/**
 * A bill's total in words, the Indian way: lakh and crore, not million.
 *
 * Words for an amount the server computed — this does no arithmetic on money
 * beyond splitting the decimal string into rupees and paise, which is why it
 * takes the string rather than a number: "150000.50" must not become
 * 150000.49999 on the way through a float.
 */
const ONES = [
  "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
  "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen",
  "Nineteen",
];
const TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"];

function belowHundred(value: number): string {
  if (value < 20) return ONES[value]!;
  const tens = TENS[Math.floor(value / 10)]!;
  return value % 10 ? `${tens}-${ONES[value % 10]}` : tens;
}

function belowThousand(value: number): string {
  const hundreds = Math.floor(value / 100);
  const rest = value % 100;
  const parts = hundreds ? [`${ONES[hundreds]} Hundred`] : [];
  if (rest) parts.push(belowHundred(rest));
  return parts.join(" ");
}

function words(value: number): string {
  if (value === 0) return "Zero";
  const parts: string[] = [];
  const crore = Math.floor(value / 10_000_000);
  const lakh = Math.floor((value % 10_000_000) / 100_000);
  const thousand = Math.floor((value % 100_000) / 1_000);
  const rest = value % 1_000;
  // A crore count can itself run past a hundred (₹150 crore).
  if (crore) parts.push(`${words(crore)} Crore`);
  if (lakh) parts.push(`${belowHundred(lakh)} Lakh`);
  if (thousand) parts.push(`${belowHundred(thousand)} Thousand`);
  if (rest) parts.push(belowThousand(rest));
  return parts.join(" ");
}

export function amountInWords(amount: string): string {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(amount.trim());
  if (!match) return "";
  const rupees = Number(match[1]);
  const paise = Number((match[2] ?? "0").padEnd(2, "0"));
  const paiseWords = paise ? ` and ${belowHundred(paise)} Paise` : "";
  return `Rupees ${words(rupees)}${paiseWords} Only`;
}
