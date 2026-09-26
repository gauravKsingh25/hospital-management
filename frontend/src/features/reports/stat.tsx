import { cn } from "@/lib/utils";

/**
 * The pieces every dashboard section is built from.
 *
 * Server components — no state, no handlers — so the dashboard's static half
 * costs the browser nothing to render. Only the parts that poll or respond to
 * the window control are client code.
 *
 * ## Why there is no chart here
 *
 * Almost everything on this screen is a *proportion*: which ward is fullest,
 * which department is busiest, where the money came from. A proportion reads
 * fine as a labelled bar, and a labelled bar is a `div` with a width — it
 * carries its own number, works at any size, and adds nothing to the bundle.
 * The one genuine time series, footfall by day, gets a real chart (see
 * `footfall-chart.tsx`), lazily loaded, which is what CLAUDE.md §4 asks for.
 */

/** A section of the dashboard, with a heading a screen reader can navigate by. */
export function Section({
  id,
  title,
  hint,
  action,
  children,
}: {
  id: string;
  title: string;
  hint?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="space-y-3" data-testid="report-section" data-section={id}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div>
          <h2 id={id} className="font-semibold">
            {title}
          </h2>
          {hint ? <p className="text-muted-foreground text-sm">{hint}</p> : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export type Tone = "default" | "good" | "warn" | "bad" | "muted";

const TONES: Record<Tone, string> = {
  default: "",
  good: "text-success",
  warn: "text-caution-foreground",
  bad: "text-destructive",
  muted: "text-muted-foreground",
};

/**
 * One number, and the words that stop it being misread.
 *
 * `hint` is not decoration. "Collected" and "Earned" are different numbers
 * that look identical on a tile, and a manager who adds them together has
 * double-counted the hospital's month.
 */
export function Stat({
  label,
  value,
  hint,
  tone = "default",
  testId,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: Tone;
  testId?: string;
}) {
  return (
    <div className="min-w-0" data-testid={testId}>
      <dt className="text-muted-foreground truncate text-xs">{label}</dt>
      <dd className={cn("tabular truncate text-2xl font-semibold", TONES[tone])}>{value}</dd>
      {hint ? <p className="text-muted-foreground mt-0.5 text-xs">{hint}</p> : null}
    </div>
  );
}

export function StatGrid({ children, columns = 4 }: { children: React.ReactNode; columns?: 3 | 4 }) {
  return (
    <dl
      className={cn(
        "grid grid-cols-2 gap-4 rounded-lg border p-4",
        columns === 3 ? "sm:grid-cols-3" : "sm:grid-cols-4",
      )}
    >
      {children}
    </dl>
  );
}

/** The empty state a section shows when the window genuinely held nothing. */
export function NothingYet({ message }: { message: string }) {
  return (
    <p className="text-muted-foreground rounded-lg border border-dashed px-4 py-8 text-center text-sm">
      {message}
    </p>
  );
}
