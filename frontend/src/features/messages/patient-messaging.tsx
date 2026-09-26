"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { AddSuppressionDialog } from "@/features/messages/suppression-list";
import { api } from "@/lib/api/client";
import type { Page, Suppression } from "@/types/api";

/**
 * Whether this patient is being messaged, on the patient's own record.
 *
 * The opt-out belongs here rather than on the blocked list, because the
 * request arrives as a sentence from a person — "stop texting me" — said to
 * whoever has their record open. Putting it on a hospital-wide list would mean
 * finding them again through a picker to record something you were already
 * looking at.
 *
 * DPDP Act 2023 §6 makes consent withdrawable, and a withdrawal that takes a
 * support ticket to action is not really withdrawable.
 */
export function PatientMessaging({
  patientId,
  patientName,
  canManage,
}: {
  patientId: string;
  patientName: string;
  canManage: boolean;
}) {
  const t = useTranslations("messages");
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);

  const key = ["notifications", "suppressions", patientId];

  const blocks = useQuery({
    queryKey: key,
    queryFn: ({ signal }) =>
      api.get<Page<Suppression>>("/notifications/suppressions", {
        query: { patient_id: patientId, limit: 20 },
        signal,
      }),
    staleTime: 30_000,
  });

  const active = blocks.data?.items ?? [];
  const deceased = active.some((block) => block.reason === "DECEASED");

  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">{t("messagingTitle")}</h2>
        {/*
          Not offered at all once a death is recorded. There is nothing useful
          to add on top of a suppression that can never be lifted, and a button
          there would invite somebody to try.
        */}
        {canManage && !deceased && active.length === 0 ? (
          <Button
            variant="outline"
            size="sm"
            className="h-tap"
            onClick={() => setAdding(true)}
            data-testid="block-messaging"
          >
            {t("addSuppression")}
          </Button>
        ) : null}
      </div>

      {active.length === 0 ? (
        <p className="text-muted-foreground rounded-lg border px-4 py-3 text-sm">
          {t("messagingOn")}
        </p>
      ) : (
        <ul className="space-y-1.5">
          {active.map((block) => (
            <li
              key={block.id}
              className="border-foreground/25 bg-foreground/5 rounded-lg border px-4 py-3 text-sm"
              data-testid="patient-suppression"
              data-reason={block.reason}
            >
              <span className="font-medium">
                {t(`reason${block.reason}` as "reasonOPTED_OUT")}
              </span>
              {block.note ? (
                <span className="text-muted-foreground"> — {block.note}</span>
              ) : null}
              {block.reason === "DECEASED" ? (
                <p className="text-muted-foreground mt-1 text-xs">{t("deathNotLiftable")}</p>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      {adding ? (
        <AddSuppressionDialog
          patientId={patientId}
          patientName={patientName}
          onOpenChange={(next) => {
            if (!next) setAdding(false);
          }}
          onDone={() => {
            setAdding(false);
            void queryClient.invalidateQueries({ queryKey: key });
          }}
        />
      ) : null}
    </section>
  );
}
