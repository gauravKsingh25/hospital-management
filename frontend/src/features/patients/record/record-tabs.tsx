import Link from "next/link";

import { cn } from "@/lib/utils";

export type RecordTab = "overview" | "visits" | "admissions" | "reports" | "billing" | "profile";

export const RECORD_TABS: readonly RecordTab[] = [
  "overview",
  "visits",
  "admissions",
  "reports",
  "billing",
  "profile",
];

export function parseTab(value: string | string[] | undefined): RecordTab {
  const candidate = Array.isArray(value) ? value[0] : value;
  return RECORD_TABS.includes(candidate as RecordTab) ? (candidate as RecordTab) : "overview";
}

/**
 * The record's sections, as links rather than client-side tabs.
 *
 * A link per section means the server renders only the section being read —
 * an invoice list nobody opened is never fetched — and the URL says where you
 * are, so "open the patient's lab reports" is something you can paste to a
 * colleague. No tab library, no client JavaScript.
 */
export function RecordTabs({
  patientId,
  active,
  tabs,
}: {
  patientId: string;
  active: RecordTab;
  tabs: { key: RecordTab; label: string; count?: number }[];
}) {
  return (
    <nav aria-label="Patient record" className="border-border -mx-1 overflow-x-auto border-b">
      <ul className="flex min-w-max gap-1 px-1">
        {tabs.map((tab) => {
          const current = tab.key === active;
          return (
            <li key={tab.key}>
              <Link
                href={tab.key === "overview" ? `/patients/${patientId}` : `/patients/${patientId}?tab=${tab.key}`}
                scroll={false}
                aria-current={current ? "page" : undefined}
                data-testid={`record-tab-${tab.key}`}
                className={cn(
                  "-mb-px inline-flex h-11 items-center gap-2 border-b-2 px-3 text-sm font-medium transition-colors",
                  current
                    ? "border-primary text-foreground"
                    : "text-muted-foreground hover:text-foreground border-transparent",
                )}
              >
                {tab.label}
                {tab.count !== undefined ? (
                  <span
                    className={cn(
                      "tabular rounded-full px-1.5 text-xs",
                      current ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
                    )}
                  >
                    {tab.count}
                  </span>
                ) : null}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
