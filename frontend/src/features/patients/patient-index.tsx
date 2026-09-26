"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Users } from "lucide-react";

import { Button } from "@/components/ui/button";
import { LinkButton } from "@/components/ui/link-button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api/client";
import { useDebounced } from "@/lib/hooks/use-debounced";
import { cn } from "@/lib/utils";
import type { Page, PatientListItem } from "@/types/api";

const PAGE_SIZE = 25;

/**
 * A browsable patient index.
 *
 * Until now the only way to reach a patient record was the search box, which
 * is fine when you know who you are looking for and useless when you do not —
 * "the woman who came in yesterday afternoon" is a real thing staff say, and
 * the answer is a list ordered by registration.
 *
 * Search here is the *patient* search rather than the universal one: this
 * screen is a list of patients and paging it, so a result set that also
 * contained tokens and doctors could not be paged or rendered in these
 * columns. The universal box in the header is the other tool, for the other
 * question.
 */
export function PatientIndex() {
  const t = useTranslations("patients");
  const common = useTranslations("common");
  const patient = useTranslations("patient");

  const [term, setTerm] = useState("");
  const [offset, setOffset] = useState(0);
  const debounced = useDebounced(term, 250);
  const searching = debounced.trim().length >= 2;

  const list = useQuery({
    queryKey: ["patients", "index", debounced, offset],
    queryFn: ({ signal }) =>
      searching
        ? api.get<Page<PatientListItem>>("/patients/search", {
            query: { q: debounced.trim(), limit: PAGE_SIZE, offset },
            signal,
          })
        : api.get<Page<PatientListItem>>("/patients", {
            query: { limit: PAGE_SIZE, offset },
            signal,
          }),
    staleTime: 10_000,
  });

  const rows = list.data?.items ?? [];
  const total = list.data?.total ?? 0;

  const onSearch = (value: string) => {
    setTerm(value);
    // Back to the first page: staying on page 4 of the previous query shows an
    // empty screen for a search that has plenty of results.
    setOffset(0);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
          <p className="text-muted-foreground mt-1 text-sm">{t("subtitle")}</p>
        </div>
        <Input
          value={term}
          onChange={(event) => onSearch(event.target.value)}
          placeholder={t("searchPlaceholder")}
          aria-label={common("search")}
          className="h-tap sm:w-80"
        />
      </div>

      {rows.length === 0 ? (
        <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-lg border border-dashed py-16 text-center">
          <Users aria-hidden className="size-6" />
          <p className="text-foreground text-sm font-medium">
            {searching ? t("noResults", { query: debounced }) : t("empty")}
          </p>
          {!searching ? <p className="text-sm">{t("emptyHint")}</p> : null}
        </div>
      ) : (
        <>
          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableCaption className="sr-only">{t("title")}</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("uhid")}</TableHead>
                  <TableHead>{t("name")}</TableHead>
                  <TableHead>{t("ageSex")}</TableHead>
                  <TableHead>{t("mobile")}</TableHead>
                  <TableHead className="text-right">{t("actions")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow
                    key={row.id}
                    data-testid="patient-row"
                    className={cn(row.is_deceased && "bg-muted/40")}
                  >
                    <TableCell className="tabular font-medium">{row.uhid}</TableCell>
                    <TableCell>
                      {row.full_name}
                      {row.is_deceased ? (
                        <span className="bg-foreground/85 text-background ml-2 rounded px-1.5 py-0.5 text-[10px] font-medium">
                          {patient("deceased")}
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-muted-foreground tabular">
                      {row.age_years ?? "—"} · {row.gender?.[0] ?? "—"}
                    </TableCell>
                    <TableCell className="text-muted-foreground tabular">{row.phone}</TableCell>
                    <TableCell className="text-right">
                      <LinkButton size="sm" variant="outline" className="h-tap" href={`/patients/${row.id}`}>
                        {t("open")}
                      </LinkButton>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <div className="flex items-center justify-between gap-3">
            <p className="text-muted-foreground text-sm">
              {t("showing", {
                from: offset + 1,
                to: offset + rows.length,
                total,
              })}
            </p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                className="h-tap"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                {common("back")}
              </Button>
              <Button
                variant="outline"
                className="h-tap"
                // `has_more` comes from the server rather than being inferred
                // from `offset + limit < total`: the two disagree the moment a
                // record is soft-deleted between pages.
                disabled={!list.data?.has_more}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                {common("next")}
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
