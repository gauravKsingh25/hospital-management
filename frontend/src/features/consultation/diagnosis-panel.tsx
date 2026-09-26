"use client";

import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Plus } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { Diagnosis } from "@/types/api";

/**
 * Diagnoses — one field and a button.
 *
 * No ICD-10 picker. The backend makes the code optional deliberately (see
 * `DiagnosisCreate`): a working diagnosis is recorded at speed in the room
 * and coded properly afterwards by records staff, and forcing a doctor to
 * search a code list mid-consultation only teaches them to pick whichever
 * wrong code appears first. That is worse than no code, because a wrong code
 * looks like data.
 *
 * The first diagnosis recorded is marked primary. Almost always right, and
 * cheaper than asking — a second one can be promoted later by anyone with the
 * records screen.
 */
export function DiagnosisPanel({
  encounterId,
  diagnoses,
  disabled,
  onSaved,
}: {
  encounterId: string;
  diagnoses: Diagnosis[];
  disabled: boolean;
  onSaved: () => void;
}) {
  const t = useTranslations("consultation");
  const [description, setDescription] = useState("");

  const add = useMutation({
    mutationFn: (value: string) =>
      api.post<Diagnosis>(`/encounters/${encounterId}/diagnoses`, {
        description: value,
        is_primary: diagnoses.length === 0,
      }),
    onSuccess: () => {
      setDescription("");
      onSaved();
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const submit = () => {
    const value = description.trim();
    if (value) add.mutate(value);
  };

  return (
    <section className="space-y-3 rounded-lg border p-4" aria-labelledby="diagnoses-heading">
      <h2 id="diagnoses-heading" className="text-sm font-semibold">
        {t("diagnoses")}
      </h2>

      {diagnoses.length > 0 ? (
        <ul className="space-y-1.5">
          {diagnoses.map((diagnosis) => (
            <li key={diagnosis.id} className="flex items-start gap-2 text-sm">
              <span className="flex-1">{diagnosis.description}</span>
              {diagnosis.is_primary ? (
                <span className="bg-primary/10 text-primary rounded px-1.5 py-0.5 text-[10px] font-medium">
                  {t("primary")}
                </span>
              ) : null}
              {diagnosis.code ? (
                <span className="text-muted-foreground tabular text-xs">{diagnosis.code}</span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-muted-foreground text-sm">{t("noDiagnoses")}</p>
      )}

      <div className="flex gap-2">
        <Input
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          onKeyDown={(event) => {
            // Enter adds and clears, so several diagnoses go in without the
            // hand leaving the keyboard.
            if (event.key === "Enter") {
              event.preventDefault();
              submit();
            }
          }}
          disabled={disabled}
          placeholder={t("diagnosisPlaceholder")}
          className="h-tap"
          aria-label={t("diagnosis")}
        />
        <Button
          onClick={submit}
          disabled={disabled || add.isPending || description.trim().length === 0}
          className="h-tap shrink-0"
          aria-label={t("addDiagnosis")}
        >
          <Plus aria-hidden className="size-4" />
        </Button>
      </div>
    </section>
  );
}
