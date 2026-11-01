"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { Users } from "lucide-react";

import { Button } from "@/components/ui/button";
import { LinkButton } from "@/components/ui/link-button";
import { Input } from "@/components/ui/input";
import { LoadError } from "@/components/load-error";
import { Skeleton } from "@/components/ui/skeleton";
import { EncounterStatusChip } from "@/components/status-chip";
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
import type { Gender, Page, PatientListItem, PatientSort, PatientVisitStats } from "@/types/api";

const PAGE_SIZE = 25;
const GENDERS: Gender[] = ["FEMALE", "MALE", "OTHER", "UNKNOWN"];
const SORTS: PatientSort[] = ["RECENT", "NAME", "UHID"];

type Filters = {
  gender: Gender | "";
  status: "" | "living" | "deceased";
  sort: PatientSort;
  from: string;
  to: string;
};

const NO_FILTERS: Filters = { gender: "", status: "", sort: "RECENT", from: "", to: "" };

const SELECT =
  "border-input bg-background h-tap rounded-md border px-2.5 text-sm shadow-xs " +
  "focus-visible:ring-ring/50 focus-visible:border-ring outline-none focus-visible:ring-3 " +
  "disabled:opacity-50";

function FilterField({ label, htmlFor, children }: { label: string; htmlFor: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <label htmlFor={htmlFor} className="text-muted-foreground block text-xs font-medium">
        {label}
      </label>
      {children}
    </div>
  );
}

/**
 * The patient directory — every record the hospital holds, in one place.
 *
 * The search box answers "find this person"; the directory answers the
 * other questions staff actually ask: "the woman who came in yesterday
 * afternoon", "every patient registered on the 5th", "who have we lost this
 * month". So it filters by sex, by whether the patient is living, and by the
 * day they registered (the hospital's day, not UTC's), and it shows each
 * person's visit count and last visit so a returning patient is obvious at a
 * glance.
 *
 * Search here is the *patient* search, not the universal one: this screen is
 * a list of patients and paging it. While a search term is in the box the
 * filters stand aside — the search ranks by how well each record matches,
 * which a filter would silently cut.
 *
 * Visit counts come from `GET /encounters/visit-stats` for exactly the rows on
 * screen — one request per page, and nothing clinical in it.
 */
export function PatientIndex({ canSeeVisits }: { canSeeVisits: boolean }) {
  const t = useTranslations("patients");
  const common = useTranslations("common");
  const patient = useTranslations("patient");
  const format = useFormatter();
  const router = useRouter();

  const [term, setTerm] = useState("");
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const debounced = useDebounced(term, 250);
  const searching = debounced.trim().length >= 2;
  const filtered = JSON.stringify(filters) !== JSON.stringify(NO_FILTERS);
  const badRange = Boolean(filters.from && filters.to && filters.from > filters.to);

  const list = useQuery({
    queryKey: ["patients", "index", debounced, offset, searching ? null : filters],
    queryFn: ({ signal }) =>
      searching
        ? api.get<Page<PatientListItem>>("/patients/search", {
            query: { q: debounced.trim(), limit: PAGE_SIZE, offset },
            signal,
          })
        : api.get<Page<PatientListItem>>("/patients", {
            query: {
              limit: PAGE_SIZE,
              offset,
              gender: filters.gender || undefined,
              deceased: filters.status === "" ? undefined : filters.status === "deceased",
              registered_from: filters.from || undefined,
              registered_to: filters.to || undefined,
              sort: filters.sort,
            },
            signal,
          }),
    enabled: !badRange,
    staleTime: 10_000,
  });

  const rows = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const ids = rows.map((row) => row.id);

  const stats = useQuery({
    queryKey: ["patients", "visit-stats", ids],
    queryFn: ({ signal }) =>
      api.get<PatientVisitStats[]>("/encounters/visit-stats", { query: { patient_id: ids }, signal }),
    enabled: canSeeVisits && ids.length > 0,
    staleTime: 10_000,
  });
  const statsById = new Map((stats.data ?? []).map((row) => [row.patient_id, row]));

  const onSearch = (value: string) => {
    setTerm(value);
    // Back to the first page: staying on page 4 of the previous query shows an
    // empty screen for a search that has plenty of results.
    setOffset(0);
  };

  const setFilter = <K extends keyof Filters>(key: K, value: Filters[K]) => {
    setFilters((current) => ({ ...current, [key]: value }));
    setOffset(0);
  };

  const date = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium" });

  return (
    <div className="space-y-5">
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

      <fieldset
        className="bg-card flex flex-wrap items-end gap-3 rounded-xl border p-3"
        disabled={searching}
        data-testid="directory-filters"
      >
        <FilterField label={t("filterGender")} htmlFor="filter-gender">
          <select
            id="filter-gender"
            className={SELECT}
            value={filters.gender}
            onChange={(event) => setFilter("gender", event.target.value as Gender | "")}
          >
            <option value="">{t("filterAll")}</option>
            {GENDERS.map((gender) => (
              <option key={gender} value={gender}>
                {patient(`gender${gender}` as "genderMALE")}
              </option>
            ))}
          </select>
        </FilterField>

        <FilterField label={t("filterStatus")} htmlFor="filter-status">
          <select
            id="filter-status"
            className={SELECT}
            value={filters.status}
            onChange={(event) => setFilter("status", event.target.value as Filters["status"])}
          >
            <option value="">{t("filterAll")}</option>
            <option value="living">{t("filterLiving")}</option>
            <option value="deceased">{t("filterDeceased")}</option>
          </select>
        </FilterField>

        <FilterField label={t("registeredFrom")} htmlFor="filter-from">
          <Input
            id="filter-from"
            type="date"
            className="h-tap w-40"
            value={filters.from}
            max={filters.to || undefined}
            onChange={(event) => setFilter("from", event.target.value)}
          />
        </FilterField>
        <FilterField label={t("registeredTo")} htmlFor="filter-to">
          <Input
            id="filter-to"
            type="date"
            className="h-tap w-40"
            value={filters.to}
            min={filters.from || undefined}
            onChange={(event) => setFilter("to", event.target.value)}
          />
        </FilterField>

        <FilterField label={t("sortBy")} htmlFor="filter-sort">
          <select
            id="filter-sort"
            className={SELECT}
            value={filters.sort}
            onChange={(event) => setFilter("sort", event.target.value as PatientSort)}
          >
            {SORTS.map((sort) => (
              <option key={sort} value={sort}>
                {t(`sort${sort}` as "sortRECENT")}
              </option>
            ))}
          </select>
        </FilterField>

        {filtered ? (
          <Button
            type="button"
            variant="ghost"
            className="h-tap"
            onClick={() => {
              setFilters(NO_FILTERS);
              setOffset(0);
            }}
          >
            {t("clearFilters")}
          </Button>
        ) : null}
      </fieldset>

      {list.isPending && !badRange ? (
        <div className="space-y-2" aria-busy="true">
          {[0, 1, 2].map((row) => (
            <Skeleton key={row} className="h-12 w-full rounded-md" />
          ))}
        </div>
      ) : list.isError && !list.data ? (
        // A failed *background* refetch keeps the last good page on screen.
        <LoadError onRetry={() => list.refetch()} />
      ) : rows.length === 0 || badRange ? (
        <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-lg border border-dashed py-16 text-center">
          <Users aria-hidden className="size-6" />
          <p className="text-foreground text-sm font-medium">
            {searching
              ? t("noResults", { query: debounced })
              : filtered
                ? t("noMatches")
                : t("empty")}
          </p>
          {!searching && !filtered ? <p className="text-sm">{t("emptyHint")}</p> : null}
        </div>
      ) : (
        <>
          <div className="bg-card overflow-x-auto rounded-xl border">
            <Table>
              <TableCaption className="sr-only">{t("title")}</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("uhid")}</TableHead>
                  <TableHead>{t("name")}</TableHead>
                  <TableHead>{t("ageSex")}</TableHead>
                  <TableHead>{t("mobile")}</TableHead>
                  <TableHead className="hidden md:table-cell">{t("city")}</TableHead>
                  <TableHead className="hidden lg:table-cell">{t("registered")}</TableHead>
                  {canSeeVisits ? (
                    <>
                      <TableHead className="text-right">{t("visits")}</TableHead>
                      <TableHead>{t("lastVisit")}</TableHead>
                    </>
                  ) : null}
                  <TableHead className="text-right">{t("actions")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => {
                  const visits = statsById.get(row.id);
                  return (
                    <TableRow
                      key={row.id}
                      data-testid="patient-row"
                      // The whole row opens the record — the button is there
                      // for keyboards and screen readers, not as the only target.
                      onClick={() => router.push(`/patients/${row.id}`)}
                      className={cn("cursor-pointer", row.is_deceased && "bg-muted/40")}
                    >
                      <TableCell className="tabular font-medium">{row.uhid}</TableCell>
                      <TableCell>
                        <span className="font-medium">{row.full_name}</span>
                        {row.is_deceased ? (
                          <span className="bg-foreground/85 text-background ml-2 rounded px-1.5 py-0.5 text-xs font-medium">
                            {patient("deceased")}
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell className="text-muted-foreground tabular">
                        {row.age_years ?? "—"} · {row.gender?.[0] ?? "—"}
                      </TableCell>
                      <TableCell className="text-muted-foreground tabular">{row.phone}</TableCell>
                      <TableCell className="text-muted-foreground hidden md:table-cell">
                        {row.city ?? "—"}
                      </TableCell>
                      <TableCell className="text-muted-foreground hidden lg:table-cell">
                        {date(row.registered_at)}
                      </TableCell>
                      {canSeeVisits ? (
                        <>
                          <TableCell className="tabular text-right" data-testid="visit-count">
                            {visits ? visits.total_visits : <Skeleton className="ml-auto h-4 w-6" />}
                          </TableCell>
                          <TableCell>
                            {!visits ? (
                              <Skeleton className="h-4 w-24" />
                            ) : visits.last_visit_at ? (
                              <div className="flex flex-wrap items-center gap-2">
                                <span className="text-sm">{date(visits.last_visit_at)}</span>
                                {visits.last_visit_status ? (
                                  <EncounterStatusChip status={visits.last_visit_status} />
                                ) : null}
                              </div>
                            ) : (
                              <span className="text-muted-foreground text-sm">{t("neverVisited")}</span>
                            )}
                          </TableCell>
                        </>
                      ) : null}
                      <TableCell className="text-right">
                        <LinkButton
                          size="sm"
                          variant="outline"
                          className="h-tap"
                          href={`/patients/${row.id}`}
                          onClick={(event) => event.stopPropagation()}
                        >
                          {t("open")}
                        </LinkButton>
                      </TableCell>
                    </TableRow>
                  );
                })}
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
