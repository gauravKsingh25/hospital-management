"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";

import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api/client";
import { useDebounced } from "@/lib/hooks/use-debounced";
import { useResolveScan } from "@/lib/hooks/use-resolve-scan";
import { useScanner } from "@/lib/hooks/use-scanner";
import type { SearchHit, SearchResults } from "@/types/api";

/**
 * The universal search box (CLAUDE.md §7b).
 *
 * One field that resolves **everything §7b names**: a UHID, a mobile number, a
 * name, today's token, a visit number off a discharge slip, a booking number
 * off an appointment card, or a doctor.
 *
 * All of it comes from one request. Three of those identifiers live in
 * `patients`, three in `scheduling` and one in `clinical`, and the obvious
 * alternative — this component calling three endpoints and merging — is the
 * one thing that must not happen: merging client-side re-ranks by string
 * distance and can push an exact match on a printed number below a near miss
 * on a name. The server composes and ranks, and this renders.
 *
 * Where a hit *goes* is decided here, because the backend has no idea what a
 * route looks like and should not learn.
 *
 * ## It also listens for a barcode scanner
 *
 * A counter scanner is a keyboard, so a scan into this box already worked with
 * no code at all: press F3, scan, done. `useScanner` removes the F3 — it
 * recognises a scanner by its typing speed and opens the patient directly,
 * from any screen, with no box to focus first.
 *
 * That is the whole benefit, and it is small; the risk of a global key handler
 * is not. See `lib/scanner.ts` for the two independent guards that make it
 * safe, the first of which is that it is completely inert while any text
 * control has focus.
 *
 * Resolving the scanned card is `useResolveScan`, shared with the camera
 * button, so a scan means the same thing whichever device read it.
 */
export function UniversalSearch() {
  const t = useTranslations("search");
  const patient = useTranslations("patient");
  const router = useRouter();

  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const debounced = useDebounced(term, 250);

  // F3 and Ctrl+K both open it: F3 is the function key §7b names, Ctrl+K is
  // what anyone who has used other software will try first.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const isSearchKey =
        event.key === "F3" || (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey));
      if (!isSearchKey) return;
      event.preventDefault();
      setOpen((previous) => !previous);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // One character is enough when it is a digit — the first nine patients of
  // every clinic hold a single-digit token, and the busiest hour of the
  // morning is exactly when somebody types one. A single letter matches most
  // of the hospital, so it waits for a second character. The server applies
  // the same rule; this only avoids a request it would answer with nothing.
  const trimmed = debounced.trim();
  const worthAsking = trimmed.length >= 2 || /^\d$/.test(trimmed);

  const query = useQuery({
    queryKey: ["search", trimmed],
    enabled: open && worthAsking,
    queryFn: ({ signal }) =>
      api.get<SearchResults>("/search", { query: { q: trimmed }, signal }),
    staleTime: 5_000,
  });

  const { resolve } = useResolveScan();

  const go = (path: string) => {
    setOpen(false);
    setTerm("");
    router.push(path);
  };

  /**
   * Where each kind of hit leads.
   *
   * A token or a visit opens the chart — that is what somebody holding a token
   * slip actually wants. Everything else opens the patient record, and a
   * doctor opens the queue filtered to them.
   */
  const openHit = (hit: SearchHit) => {
    if (hit.encounter_id) return go(`/consultation/${hit.encounter_id}`);
    if (hit.patient_id) return go(`/patients/${hit.patient_id}`);
    if (hit.doctor_id) return go("/queue");
    return go("/");
  };

  // What a scan *does* lives in `useResolveScan`, shared with the camera path,
  // so the two devices cannot drift into resolving a card differently.
  useScanner((uhid) => {
    setOpen(false);
    resolve(uhid);
  });

  const hits = query.data?.hits ?? [];

  return (
    <>
      <Button
        variant="outline"
        onClick={() => setOpen(true)}
        className="text-muted-foreground h-tap w-full justify-start gap-2 px-3 font-normal sm:w-72 lg:w-96"
      >
        <Search aria-hidden className="size-4 shrink-0" />
        <span className="truncate">{t("placeholder")}</span>
        <kbd className="bg-muted text-muted-foreground ml-auto hidden rounded border px-1.5 py-0.5 text-[10px] font-medium lg:inline-block">
          F3
        </kbd>
      </Button>

      <CommandDialog open={open} onOpenChange={setOpen} title={t("label")} description={t("hint")}>
        {/* `shouldFilter={false}`: the backend has already ranked these — an
            exact match on a printed number first, then patients, then doctors.
            cmdk's client-side fuzzy filter would re-rank that by string
            distance, which is precisely backwards. */}
        <Command shouldFilter={false}>
          <CommandInput placeholder={t("placeholder")} value={term} onValueChange={setTerm} />
          <CommandList>
            {query.isFetching ? (
              <div className="text-muted-foreground p-4 text-sm">{t("searching")}</div>
            ) : null}

            {!query.isFetching && !worthAsking ? (
              <div className="text-muted-foreground p-4 text-sm">{t("hint")}</div>
            ) : null}

            {!query.isFetching && worthAsking && hits.length === 0 ? (
              <CommandEmpty>{t("noResults", { query: trimmed })}</CommandEmpty>
            ) : null}

            {hits.length > 0 ? (
              <CommandGroup heading={t("results", { count: hits.length })}>
                {hits.map((hit, index) => (
                  <CommandItem
                    // Not the id alone: one patient can appear as both a
                    // record and today's token, and React needs the pair.
                    key={`${hit.kind}-${hit.patient_id ?? hit.doctor_id ?? index}`}
                    value={`${hit.kind}-${index}`}
                    onSelect={() => openHit(hit)}
                    className="flex items-center gap-3 py-2.5"
                  >
                    <span className="bg-muted text-muted-foreground rounded px-1.5 py-0.5 text-[10px] font-medium">
                      {t(`kind${hit.kind}` as "kindPATIENT")}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate font-medium">{hit.title}</p>
                      {hit.subtitle ? (
                        <p className="text-muted-foreground tabular truncate text-xs">
                          {hit.subtitle}
                        </p>
                      ) : null}
                    </div>
                    {hit.is_deceased ? (
                      <span className="bg-foreground/85 text-background rounded px-1.5 py-0.5 text-[10px] font-medium">
                        {patient("deceased")}
                      </span>
                    ) : null}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
          </CommandList>
        </Command>
      </CommandDialog>
    </>
  );
}
