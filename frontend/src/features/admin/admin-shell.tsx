import type { ReactNode } from "react";

/**
 * The frame every administration screen shares.
 *
 * These screens are the one part of the system a member of staff visits
 * rarely, under time pressure, usually because something is wrong — a new
 * doctor started this morning, a price is out of date, a test cannot be
 * ordered. So each one states in a sentence what it is for and what breaks
 * without it, and that sentence is a prop rather than something each screen
 * decides how to render.
 */
export function AdminShell({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description: string;
  /** A primary action rendered beside the heading — usually "Add …". */
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-2xl">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          <p className="text-muted-foreground mt-1 text-sm">{description}</p>
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}

/** An empty state that says what to do, not just that there is nothing. */
export function AdminEmpty({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="text-muted-foreground rounded-lg border border-dashed py-12 text-center">
      <p className="text-foreground text-sm font-medium">{message}</p>
      {hint ? <p className="mt-1 text-sm">{hint}</p> : null}
    </div>
  );
}
