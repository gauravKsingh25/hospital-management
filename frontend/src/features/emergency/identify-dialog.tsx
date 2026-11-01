"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Link2, Search } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { useDebounced } from "@/lib/hooks/use-debounced";
import { cn } from "@/lib/utils";
import type {
  Gender,
  Page,
  Patient,
  PatientIdentifyRequest,
  PatientListItem,
} from "@/types/api";

const GENDERS = ["MALE", "FEMALE", "OTHER"] as const satisfies readonly Gender[];

/** What the dialog needs to know about the patient being named. */
export type UnidentifiedPatient = Pick<
  PatientListItem,
  "id" | "full_name" | "uhid" | "gender" | "age_years"
>;

type Candidate = { id: string; full_name: string; uhid: string };

/**
 * Say who an unidentified emergency patient is (UX-01).
 *
 * Two ways, in the order they matter: **somebody already registered** — the
 * relative has the old card, or the name finds them — and **a new person**.
 * Linking keeps this record (the one the live visit, bed and bill point at)
 * and folds the old one into it, allergies included; the old card goes on
 * opening the patient. A name typed for a new person that turns out to be
 * registered already comes back from the server as that record, offered for
 * linking, rather than as a second history.
 */
export function IdentifyDialog({
  patient,
  open,
  onOpenChange,
}: {
  patient: UnidentifiedPatient;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("emergency");
  const tReg = useTranslations("registration");
  const tPatient = useTranslations("patient");
  const common = useTranslations("common");
  const queryClient = useQueryClient();
  const router = useRouter();

  const [query, setQuery] = useState("");
  const [linking, setLinking] = useState<Candidate | null>(null);
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [age, setAge] = useState(patient.age_years !== null ? String(patient.age_years) : "");
  const [gender, setGender] = useState<Gender | null>(
    patient.gender === "MALE" || patient.gender === "FEMALE" ? patient.gender : null,
  );

  const debounced = useDebounced(query.trim(), 300);
  const search = useQuery({
    queryKey: ["patients", "search", debounced],
    enabled: open && debounced.length >= 3,
    queryFn: ({ signal }) =>
      api.get<Page<PatientListItem>>("/patients/search", {
        query: { q: debounced, limit: 8 },
        signal,
      }),
    staleTime: 30_000,
  });
  // Another unknown patient is not an identity, and neither is this one.
  const candidates = (search.data?.items ?? []).filter(
    (row) => !row.is_unidentified && row.id !== patient.id,
  );

  const reset = () => {
    setQuery("");
    setLinking(null);
    setName("");
    setPhone("");
  };

  const identify = useMutation({
    mutationFn: (body: PatientIdentifyRequest) =>
      api.post<Patient>(`/patients/${patient.id}/identify`, body),
    onSuccess: (identified) => {
      toast.success(t("identified", { tag: patient.full_name, name: identified.full_name }));
      // The board, the needs-identification list and any open record all
      // carried the placeholder name.
      void queryClient.invalidateQueries({ queryKey: ["queue"] });
      void queryClient.invalidateQueries({ queryKey: ["patients"] });
      router.refresh();
      reset();
      onOpenChange(false);
    },
    onError: (error) => {
      if (!isApiError(error)) throw error;
      const details = error.details;
      if (error.code === "patient_already_exists" && details) {
        // Typed as a new person, but registered already: offer the record.
        setLinking({
          id: String(details.patient_id),
          full_name: String(details.full_name),
          uhid: String(details.uhid),
        });
        return;
      }
      toast.error(error.message);
    },
  });

  const ageNumber = age.trim() === "" ? null : Number(age);
  const newPersonReady =
    name.trim().length > 0 &&
    phone.replace(/\D/g, "").length >= 10 &&
    gender !== null &&
    ageNumber !== null &&
    Number.isInteger(ageNumber) &&
    ageNumber >= 0 &&
    ageNumber <= 130;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("identifyTitle")}</DialogTitle>
          <DialogDescription>
            {patient.full_name} · {patient.uhid}
          </DialogDescription>
        </DialogHeader>

        {linking ? (
          <div className="space-y-4 text-left" data-testid="identify-confirm-link">
            <p className="text-sm">
              {t("linkConfirm", {
                tag: patient.full_name,
                name: linking.full_name,
                uhid: linking.uhid,
              })}
            </p>
            <p className="text-muted-foreground text-sm">{t("linkExplain")}</p>
            <div className="flex flex-wrap justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                className="h-tap"
                onClick={() => setLinking(null)}
              >
                {common("back")}
              </Button>
              <Button
                type="button"
                className="h-tap"
                disabled={identify.isPending}
                onClick={() => identify.mutate({ existing_patient_id: linking.id })}
                data-testid="identify-link-confirm"
              >
                <Link2 aria-hidden className="size-4" />
                {t("link")}
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-6 text-left">
            <section className="space-y-2">
              <Label htmlFor="identify-search">{t("findExisting")}</Label>
              <div className="relative">
                <Search
                  aria-hidden
                  className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
                />
                <Input
                  id="identify-search"
                  className="h-tap pl-9 text-base"
                  autoComplete="off"
                  placeholder={t("findExistingPlaceholder")}
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
              {debounced.length >= 3 && search.isSuccess && candidates.length === 0 ? (
                <p className="text-muted-foreground text-xs">{t("noneFound")}</p>
              ) : null}
              {candidates.length > 0 ? (
                <ul className="divide-y rounded-lg border" data-testid="identify-candidates">
                  {candidates.map((row) => (
                    <li key={row.id} className="flex items-center gap-3 px-3 py-2">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium">{row.full_name}</div>
                        <div className="text-muted-foreground tabular text-xs">
                          {[row.uhid, row.phone, row.age_years !== null ? `${row.age_years} y` : null]
                            .filter(Boolean)
                            .join(" · ")}
                        </div>
                      </div>
                      {row.is_deceased ? (
                        // A dead record cannot be the patient in casualty; the
                        // server refuses it too.
                        <span className="text-muted-foreground text-xs">
                          {tPatient("deceased")}
                        </span>
                      ) : (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="h-tap shrink-0"
                          onClick={() => setLinking(row)}
                        >
                          {t("thisIsThem")}
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              ) : null}
            </section>

            <form
              className="space-y-4 border-t pt-4"
              noValidate
              onSubmit={(event) => {
                event.preventDefault();
                if (!newPersonReady || !gender || ageNumber === null) return;
                identify.mutate({
                  full_name: name.trim(),
                  phone: phone.trim(),
                  gender,
                  age_years: ageNumber,
                });
              }}
            >
              <p className="text-sm font-medium">{t("newPerson")}</p>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2 sm:col-span-2">
                  <Label htmlFor="identify-name">{tReg("fullName")}</Label>
                  <Input
                    id="identify-name"
                    className="h-tap text-base"
                    autoComplete="off"
                    autoCapitalize="words"
                    spellCheck={false}
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="identify-phone">{tReg("phone")}</Label>
                  <Input
                    id="identify-phone"
                    type="tel"
                    inputMode="numeric"
                    className="h-tap tabular text-base"
                    autoComplete="off"
                    placeholder={tReg("phonePlaceholder")}
                    value={phone}
                    onChange={(event) => setPhone(event.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="identify-age">{tReg("age")}</Label>
                  <Input
                    id="identify-age"
                    type="number"
                    inputMode="numeric"
                    min={0}
                    max={130}
                    className="h-tap tabular text-base"
                    value={age}
                    onChange={(event) => setAge(event.target.value)}
                  />
                </div>
                <fieldset className="space-y-2 sm:col-span-2">
                  <legend className="mb-2 text-sm font-medium">{tReg("gender")}</legend>
                  <div className="flex gap-2">
                    {GENDERS.map((value) => (
                      <button
                        key={value}
                        type="button"
                        aria-pressed={gender === value}
                        onClick={() => setGender(value)}
                        className={cn(
                          "min-h-tap flex-1 rounded-lg border px-3 text-sm font-medium transition-colors",
                          gender === value
                            ? "border-primary bg-primary/10 text-primary"
                            : "hover:bg-muted",
                        )}
                      >
                        {tPatient(`gender${value}`)}
                      </button>
                    ))}
                  </div>
                </fieldset>
              </div>
              <p className="text-muted-foreground text-xs">{t("phoneNeeded")}</p>
              <div className="flex justify-end">
                <Button
                  type="submit"
                  className="h-tap"
                  disabled={!newPersonReady || identify.isPending}
                  data-testid="identify-save"
                >
                  {identify.isPending ? t("saving") : t("saveNewPerson")}
                </Button>
              </div>
            </form>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
