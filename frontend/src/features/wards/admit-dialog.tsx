"use client";

import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
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
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { Admission, BoardWard } from "@/types/api";

/**
 * Admitting a patient from the consultation screen.
 *
 * Posts to `/ipd/admissions`, not to the encounter's own `admit` transition.
 * The two are not interchangeable: the IPD endpoint takes the bed *and* moves
 * the encounter, in that order and in one transaction, so no subscriber ever
 * sees an admitted patient with nowhere to be. Calling the clinical transition
 * from here would leave a patient marked ADMITTED and standing in a corridor.
 *
 * The bed is picked from the live board rather than a dropdown of ids, because
 * the decision being made is a physical one — which free bed, in which ward —
 * and a ward's gender policy makes some of them wrong for this patient. The
 * backend refuses those; this shows them greyed with the reason rather than
 * letting somebody pick one and be told no.
 *
 * Everything else is optional, matching `AdmissionCreate`. An admission
 * happens at 2am with a sick patient in a corridor, and a form that blocks on
 * an expected length of stay is a form somebody works around.
 */
/**
 * What is being admitted: a visit (the doctor's consultation screen), or a
 * patient the OPD sent to the admission desk. The desk's route resolves the
 * visit itself — including opening a new IPD visit when the OPD one has
 * already closed — so the dialog never has to know which case it is in.
 */
type AdmitSource =
  { encounterId: string; requestId?: undefined } | { requestId: string; encounterId?: undefined };

export function AdmitDialog({
  encounterId,
  requestId,
  patientName,
  patientGender,
  open,
  onOpenChange,
}: AdmitSource & {
  patientName: string;
  patientGender: string | null | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("wards");
  const common = useTranslations("common");
  const router = useRouter();
  const queryClient = useQueryClient();

  const [bedId, setBedId] = useState("");
  const [diagnosis, setDiagnosis] = useState("");
  const [attendantName, setAttendantName] = useState("");
  const [attendantPhone, setAttendantPhone] = useState("");

  const board = useQuery({
    queryKey: ["ipd", "board"],
    queryFn: ({ signal }) => api.get<BoardWard[]>("/ipd/wards/board", { signal }),
    enabled: open,
    // Short: two people admitting at once must not both be offered bed 4.
    staleTime: 5_000,
  });

  const admit = useMutation({
    mutationFn: () => {
      const details = {
        bed_id: bedId,
        provisional_diagnosis: diagnosis.trim() || null,
        attendant_name: attendantName.trim() || null,
        attendant_phone: attendantPhone.trim() || null,
      };
      return requestId
        ? api.post<Admission>(`/ipd/admission-requests/${requestId}/admit`, details)
        : api.post<Admission>("/ipd/admissions", { encounter_id: encounterId, ...details });
    },
    onSuccess: (admission) => {
      toast.success(t("admitted", { number: admission.admission_number }));
      // The bed board, and the admission desk's list — the patient has left it.
      void queryClient.invalidateQueries({ queryKey: ["ipd"] });
      onOpenChange(false);
      router.push(`/admissions/${admission.id}`);
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const wards = board.data ?? [];
  const anyFree = wards.some((ward) => (ward.beds ?? []).some((bed) => bed.status === "AVAILABLE"));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("admitTitle")}</DialogTitle>
          <DialogDescription>{patientName}</DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] space-y-4 overflow-y-auto text-left">
          <fieldset className="space-y-3">
            <legend className="text-sm font-medium">{t("chooseBed")}</legend>

            {board.isPending ? (
              <p className="text-muted-foreground text-sm">{common("loading")}</p>
            ) : !anyFree ? (
              <p className="border-critical/40 bg-critical/10 rounded-md border px-3 py-2 text-sm">
                {t("noFreeBeds")}
              </p>
            ) : (
              wards.map((ward) => {
                // A ward reserved for one sex cannot take this patient. The
                // backend refuses it (`_assert_ward_accepts`); showing why is
                // better than showing a bed that fails on submit.
                const wrongWard =
                  Boolean(ward.gender_policy) &&
                  Boolean(patientGender) &&
                  ward.gender_policy !== patientGender;
                const free = (ward.beds ?? []).filter((bed) => bed.status === "AVAILABLE");
                if (free.length === 0) return null;

                return (
                  <div key={ward.id} className="space-y-1.5">
                    <p className="text-muted-foreground text-xs">
                      {ward.name} · {ward.bed_class}
                      {wrongWard ? (
                        <span className="text-caution-foreground ml-2 font-medium">
                          {t("wardRestricted", { policy: ward.gender_policy ?? "" })}
                        </span>
                      ) : null}
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {free.map((bed) => (
                        <label
                          key={bed.id}
                          className={cn(
                            "min-h-tap cursor-pointer rounded-md border px-3 py-2 text-sm font-medium",
                            bedId === bed.id ? "border-primary bg-primary/10" : "hover:bg-accent",
                            wrongWard && "cursor-not-allowed opacity-40",
                          )}
                        >
                          <input
                            type="radio"
                            name="bed"
                            value={bed.id}
                            disabled={wrongWard}
                            checked={bedId === bed.id}
                            onChange={() => setBedId(bed.id)}
                            className="sr-only"
                          />
                          {bed.code}
                        </label>
                      ))}
                    </div>
                  </div>
                );
              })
            )}
          </fieldset>

          <div className="space-y-1.5">
            <Label htmlFor="admit-diagnosis">
              {t("provisionalDiagnosis")}{" "}
              <span className="text-muted-foreground">({common("optional")})</span>
            </Label>
            <Textarea
              id="admit-diagnosis"
              value={diagnosis}
              onChange={(event) => setDiagnosis(event.target.value)}
              rows={2}
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="admit-attendant">{t("attendantName")}</Label>
              <Input
                id="admit-attendant"
                value={attendantName}
                onChange={(event) => setAttendantName(event.target.value)}
                className="h-tap"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="admit-attendant-phone">{t("attendantPhone")}</Label>
              <Input
                id="admit-attendant-phone"
                value={attendantPhone}
                onChange={(event) => setAttendantPhone(event.target.value)}
                inputMode="tel"
                className="h-tap tabular"
              />
            </div>
          </div>
          {/* Not idle curiosity: this is who the ward telephones at 3am. */}
          <p className="text-muted-foreground text-xs">{t("attendantHint")}</p>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!bedId || admit.isPending}
            onClick={() => admit.mutate()}
          >
            {t("admit")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
