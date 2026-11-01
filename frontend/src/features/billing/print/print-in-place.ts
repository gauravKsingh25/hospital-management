/**
 * Print a bill without leaving the page.
 *
 * The A4 bill page loads in a hidden frame and opens the print dialog itself
 * (`?print=1`), so "Print" on a dashboard row prints the bill — letterhead,
 * GST columns, amount in words — and the accountant stays where they were.
 * The same dialog saves a PDF ("Save as PDF"), named after the bill number.
 * No PDF library is involved.
 */
export function printInPlace(path: string): void {
  const frame = document.createElement("iframe");
  frame.setAttribute("aria-hidden", "true");
  frame.tabIndex = -1;
  frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden";
  frame.src = `${path}${path.includes("?") ? "&" : "?"}print=1`;
  frame.dataset.testid = "print-frame";

  const remove = () => frame.remove();
  frame.addEventListener("load", () => {
    frame.contentWindow?.addEventListener("afterprint", () => setTimeout(remove, 60_000));
  });
  // A dialog left open for ten minutes: the frame has done its job.
  setTimeout(remove, 600_000);
  document.body.appendChild(frame);
}

export const billPath = (invoiceId: string) => `/bills/${invoiceId}`;
export const runningBillPath = (encounterId: string) => `/bills/running/${encounterId}`;
export const summaryPath = (admissionId: string) => `/discharge-summaries/${admissionId}`;
