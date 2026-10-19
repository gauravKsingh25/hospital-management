/**
 * Pages meant for paper: no sidebar, no header, black on white, A4.
 *
 * A route group of its own so the signed-in shell never renders here — the
 * page is the document. Sign-in is still enforced by `proxy.ts`, and every
 * figure comes from the API as on any other screen.
 */
export default function PrintLayout({ children }: LayoutProps<"/">) {
  return (
    <div className="min-h-svh bg-white text-black print:min-h-0">
      <style>{`@page { size: A4; margin: 12mm; }`}</style>
      {children}
    </div>
  );
}
