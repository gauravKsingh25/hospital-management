"use client";

import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { CheckCircle2, Clock } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { Encounter, PendingItems } from "@/types/api";

/**
 * "I am finished" — the doctor's last action (CLAUDE.md §6).
 *
 * The important thing about this button is what it does *not* do: it does not
 * choose a status. The doctor says they are done; the state machine decides
 * whether that means `COMPLETED` (nothing outstanding) or
 * `PENDING_CLEARANCE` / `AWAITING_RESULTS` (something is). Offering a status
 * dropdown here would put a decision on the smallest surface in the system
 * and give the state machine a second author.
 *
 * So the screen shows what is pending, in the backend's own words, and the
 * button says the same thing regardless. The doctor is informed, not asked.
 *
 * Follow-up is inline rather than behind a dialog. It is the single most
 * common thing a doctor records at the end of an OPD visit, and §7b measures
 * follow-up compliance — a field one click away is a field that gets filled.
 */
export function CompleteVisit({
  encounterId,
  pending,
  hasNote,
  onCompleted,
}: {
  encounterId: string;
  pending: PendingItems;
  hasNote: boolean;
  onCompleted: (encounter: Encounter) => void;
}) {
  const t = useTranslations("consultation");
  const [followUpDate, setFollowUpDate] = useState("");
  const [instructions, setInstructions] = useState("");

  const complete = useMutation({
    mutationFn: () =>
      api.post<Encounter>(`/encounters/${encounterId}/complete`, {
        follow_up_date: followUpDate || null,
        follow_up_instructions: instructions.trim() || null,
      }),
    onSuccess: onCompleted,
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  return (
    <section className="space-y-4 rounded-lg border p-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="follow_up_date">{t("followUpDate")}</Label>
          <Input
            id="follow_up_date"
            type="date"
            value={followUpDate}
            onChange={(event) => setFollowUpDate(event.target.value)}
            className="h-tap"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="follow_up_instructions">{t("followUpInstructions")}</Label>
          <Input
            id="follow_up_instructions"
            value={instructions}
            onChange={(event) => setInstructions(event.target.value)}
            className="h-tap"
          />
        </div>
      </div>

      {pending.total > 0 ? (
        <div className="border-caution/50 bg-caution/10 space-y-1 rounded-md border px-3 py-2">
          <p className="flex items-center gap-2 text-sm font-medium">
            <Clock aria-hidden className="size-4" />
            {t("pendingTitle")}
          </p>
          {/* The backend phrases these for staff to read out to a waiting
              patient, so they are shown verbatim rather than reformatted. */}
          <ul className="text-muted-foreground list-disc pl-5 text-sm">
            {pending.descriptions.map((description) => (
              <li key={description}>{description}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <Button
        onClick={() => complete.mutate()}
        disabled={complete.isPending || !hasNote}
        className="h-tap w-full text-base"
        data-testid="complete-visit"
      >
        <CheckCircle2 aria-hidden className="size-4" />
        {complete.isPending ? t("completing") : t("completeVisit")}
      </Button>

      {!hasNote ? <p className="text-muted-foreground text-center text-xs">{t("noteRequired")}</p> : null}
    </section>
  );
}
