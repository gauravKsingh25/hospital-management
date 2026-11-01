"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";
import { UserSearch } from "lucide-react";

import { Button } from "@/components/ui/button";
import { IdentifyDialog, type UnidentifiedPatient } from "@/features/emergency/identify-dialog";
import { api } from "@/lib/api/client";
import type { Page, PatientListItem } from "@/types/api";

/**
 * Reception's list of emergency patients nobody has named yet (UX-01).
 *
 * Renders nothing when there are none — which is almost always. When there
 * are, it sits above the queue because the people who can name the patient
 * (relatives, the police) walk up to this desk, and the patient's messages
 * stay held until somebody does.
 */
export function NeedsIdentification({ canIdentify }: { canIdentify: boolean }) {
  const t = useTranslations("emergency");
  const format = useFormatter();
  const [naming, setNaming] = useState<UnidentifiedPatient | null>(null);

  const list = useQuery({
    queryKey: ["patients", "unidentified"],
    queryFn: ({ signal }) =>
      api.get<Page<PatientListItem>>("/patients", {
        query: { unidentified: true, include_inactive: true, limit: 50 },
        signal,
      }),
    refetchInterval: 30_000,
  });

  const rows = list.data?.items ?? [];
  if (rows.length === 0) return null;

  return (
    <section
      className="border-destructive/40 bg-destructive/5 rounded-lg border p-4"
      aria-labelledby="needs-identification-title"
      data-testid="needs-identification"
    >
      <h2
        id="needs-identification-title"
        className="text-destructive flex items-center gap-2 text-sm font-semibold"
      >
        <UserSearch aria-hidden className="size-4" />
        {t("needsIdentification", { count: list.data?.total ?? rows.length })}
      </h2>
      <p className="text-muted-foreground mt-1 text-xs">{t("needsIdentificationHint")}</p>

      <ul className="mt-3 divide-y rounded-md border bg-background">
        {rows.map((row) => (
          <li key={row.id} className="flex flex-wrap items-center gap-3 px-3 py-2">
            <div className="min-w-0 flex-1">
              <Link
                href={`/patients/${row.id}`}
                className="text-sm font-medium hover:underline"
                data-testid="unidentified-name"
              >
                {row.full_name}
              </Link>
              <div className="text-muted-foreground tabular text-xs">
                {[
                  row.uhid,
                  row.age_years !== null ? t("aboutAge", { age: row.age_years }) : null,
                  format.relativeTime(new Date(row.registered_at)),
                  row.is_deceased ? t("deceased") : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </div>
            </div>
            {canIdentify ? (
              <Button
                type="button"
                size="sm"
                className="h-tap"
                onClick={() => setNaming(row)}
                data-testid="identify-button"
              >
                {t("identify")}
              </Button>
            ) : null}
          </li>
        ))}
      </ul>

      {naming ? (
        <IdentifyDialog
          patient={naming}
          open
          onOpenChange={(open) => {
            if (!open) setNaming(null);
          }}
        />
      ) : null}
    </section>
  );
}
