"use client";

import { useMutation } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { Diagnosis, DiagnosisInput } from "@/types/api";

/**
 * Record a diagnosis against a visit that is still open.
 *
 * The consultation screen that used to carry this no longer exists — doctors
 * see OPD patients in person — so without it nothing in the app could put a
 * condition on the record, and "what has this patient been treated for?"
 * would stay blank forever. It lives on the visit card for whoever holds
 * `diagnosis:record`: the doctor, or the records officer coding what the
 * doctor wrote on paper (CLAUDE.md §7 — staff do the data entry).
 *
 * Four fields and the code is optional: a working diagnosis is recorded at
 * speed, and an ICD code can be added on the next pass.
 */
export function AddDiagnosis({ encounterId, patientName }: { encounterId: string; patientName: string }) {
  const t = useTranslations("record");
  const common = useTranslations("common");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [description, setDescription] = useState("");
  const [code, setCode] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [primary, setPrimary] = useState(true);

  const reset = () => {
    setDescription("");
    setCode("");
    setConfirmed(false);
    setPrimary(true);
  };

  const save = useMutation({
    mutationFn: (payload: DiagnosisInput) =>
      api.post<Diagnosis>(`/encounters/${encounterId}/diagnoses`, payload),
    onSuccess: () => {
      toast.success(t("diagnosisSaved"));
      setOpen(false);
      reset();
      // The visit card and the problem list are server-rendered.
      router.refresh();
    },
    onError: (error) => {
      toast.error(isApiError(error) ? error.message : common("somethingWentWrong"));
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!description.trim()) return;
    save.mutate({
      description: description.trim(),
      code: code.trim() ? code.trim().toUpperCase() : null,
      code_system: "ICD-10",
      diagnosis_type: confirmed ? "FINAL" : "PROVISIONAL",
      certainty: confirmed ? "CONFIRMED" : "PROBABLE",
      is_primary: primary,
    });
  };

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-8 text-xs"
        onClick={() => setOpen(true)}
        data-testid="add-diagnosis"
      >
        <Plus aria-hidden className="size-3.5" />
        {t("addDiagnosis")}
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("addDiagnosis")}</DialogTitle>
            <DialogDescription>{patientName}</DialogDescription>
          </DialogHeader>

          <form onSubmit={submit} className="space-y-4" noValidate>
            <div className="space-y-1.5">
              <Label htmlFor={`dx-${encounterId}`}>{t("diagnosis")}</Label>
              <Input
                id={`dx-${encounterId}`}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                maxLength={300}
                className="h-tap"
                autoFocus
                required
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`dx-code-${encounterId}`}>
                {t("icdCode")} <span className="text-muted-foreground">({common("optional")})</span>
              </Label>
              <Input
                id={`dx-code-${encounterId}`}
                value={code}
                onChange={(event) => setCode(event.target.value)}
                maxLength={16}
                className="h-tap tabular w-40"
                placeholder="E11"
              />
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="size-4"
                  checked={confirmed}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                {t("dxFINAL")}
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="size-4"
                  checked={primary}
                  onChange={(event) => setPrimary(event.target.checked)}
                />
                {t("primaryDiagnosis")}
              </label>
            </div>

            <DialogFooter>
              <Button type="button" variant="outline" className="h-tap" onClick={() => setOpen(false)}>
                {common("cancel")}
              </Button>
              <Button type="submit" className="h-tap" disabled={!description.trim() || save.isPending}>
                {common("save")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
