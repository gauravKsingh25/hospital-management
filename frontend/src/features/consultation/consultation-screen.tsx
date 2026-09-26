"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { BedDouble, Play } from "lucide-react";
import { toast } from "sonner";

import { CompleteVisit } from "@/features/consultation/complete-visit";
import { DiagnosisPanel } from "@/features/consultation/diagnosis-panel";
import { NoteEditor } from "@/features/consultation/note-editor";
import { OrdersPanel } from "@/features/consultation/orders-panel";
import { OutcomeSummary, RecordOutcome } from "@/features/consultation/terminal-outcome";
import { VitalsStrip } from "@/features/consultation/vitals-strip";
import { AdmitDialog } from "@/features/wards/admit-dialog";
import { EncounterStatusChip } from "@/components/status-chip";
import { PatientIdentityStrip, SafetyBanner } from "@/components/patient/safety-banner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { Encounter, EncounterChart, NoteTemplate } from "@/types/api";

/**
 * The doctor's whole surface, on one screen.
 *
 * CLAUDE.md §7 asks for the smallest possible doctor surface: review vitals,
 * write the note, pick a diagnosis, place orders, complete. That is exactly
 * the five things here and nothing else — no navigation between sub-tabs, no
 * separate "add diagnosis" page, no dialog to place an order. Every one of
 * those would be a click, and §7b's budget is sixty seconds for the whole
 * consultation.
 *
 * ## Why the whole screen is one client component
 *
 * The rest of the app is server-rendered by default. This screen is not,
 * because every part of it mutates and the parts depend on each other:
 * completing a visit depends on what is pending, which changes when an order
 * is placed, which changes what the complete button will do. Modelling that
 * as five server round trips would be slower and would flicker.
 *
 * The initial chart still comes from the server (see the page), so the first
 * paint is complete — this component hydrates it rather than fetching it.
 */
export function ConsultationScreen({
  encounterId,
  initialChart,
  templates,
  canWrite,
  canDiagnose,
  canOrder,
  canComplete,
  canAdmit,
  canRecordDeath,
  canRecordReferral,
  canRecordLama,
}: {
  encounterId: string;
  initialChart: EncounterChart;
  templates: NoteTemplate[];
  canWrite: boolean;
  canDiagnose: boolean;
  canOrder: boolean;
  canComplete: boolean;
  /** May start an inpatient stay — `admission:create`, not `encounter:admit`. */
  canAdmit: boolean;
  /** CLAUDE.md §6's three terminal outcomes, each its own permission. */
  canRecordDeath: boolean;
  canRecordReferral: boolean;
  canRecordLama: boolean;
}) {
  const t = useTranslations("consultation");
  const router = useRouter();
  const queryClient = useQueryClient();

  const chartKey = ["encounter", encounterId, "chart"];

  const { data: chart } = useQuery({
    queryKey: chartKey,
    queryFn: ({ signal }) =>
      api.get<EncounterChart>(`/encounters/${encounterId}/chart`, { signal }),
    // The server already fetched this. Seeding it means no second request on
    // mount, and no flash of a skeleton over data that is already on screen.
    initialData: initialChart,
    // The nurse may add vitals while the doctor has the chart open, and an
    // order placed at the counter changes what is pending. Thirty seconds is
    // short enough to catch both without polling a static screen.
    refetchInterval: 30_000,
  });

  const refreshChart = () => queryClient.invalidateQueries({ queryKey: chartKey });

  const start = useMutation({
    mutationFn: () => api.post<Encounter>(`/encounters/${encounterId}/start`),
    onSuccess: () => {
      void refreshChart();
      // The queue row moves to "with the doctor" — reception and nursing are
      // watching that, so their screens must not stay wrong until they poll.
      void queryClient.invalidateQueries({ queryKey: ["queue"] });
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const [admitting, setAdmitting] = useState(false);

  const status = chart.encounter.status;
  const isOpen = status === "IN_CONSULTATION";
  const isTerminal = !["REGISTERED", "IN_CONSULTATION", "AWAITING_RESULTS", "PENDING_CLEARANCE"].includes(
    status,
  );

  return (
    <div className="space-y-5">
      {/* Sticky, so the allergy warning stays on screen while the doctor
          scrolls through the note they are writing. A safety banner that
          scrolls away is a safety banner that is absent when it matters. */}
      <div className="bg-background/95 supports-[backdrop-filter]:bg-background/80 sticky top-16 z-20 -mx-3 space-y-3 px-3 py-3 backdrop-blur sm:-mx-5 sm:px-5">
        <SafetyBanner banner={chart.banner} />

        <div className="flex flex-wrap items-center justify-between gap-3">
          <PatientIdentityStrip banner={chart.banner} />
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground tabular text-xs">
              {chart.encounter.encounter_number}
            </span>
            <EncounterStatusChip status={status} />
          </div>
        </div>
      </div>

      <VitalsStrip vitals={chart.vitals} />

      {status === "REGISTERED" ? (
        <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-10 text-center">
          <p className="text-muted-foreground text-sm">{t("startPrompt")}</p>
          <Button
            className="h-tap"
            onClick={() => start.mutate()}
            disabled={start.isPending || !canWrite}
            autoFocus
          >
            <Play aria-hidden className="size-4" />
            {t("start")}
          </Button>
        </div>
      ) : null}

      {/* How the visit ended, on a visit that ended unusually. A terminal
          encounter used to render as a status chip and nothing else, so the
          cause of death, the receiving hospital and whether the LAMA form was
          signed were all recorded and none of it readable. */}
      <OutcomeSummary encounter={chart.encounter} />

      {isTerminal ? null : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
          <div className="space-y-5">
            <NoteEditor
              encounterId={encounterId}
              notes={chart.notes}
              templates={templates}
              disabled={!isOpen || !canWrite}
              onSaved={refreshChart}
            />
          </div>

          <div className="space-y-5">
            <DiagnosisPanel
              encounterId={encounterId}
              diagnoses={chart.diagnoses}
              disabled={!isOpen || !canDiagnose}
              onSaved={refreshChart}
            />
            <OrdersPanel
              encounterId={encounterId}
              orders={chart.orders}
              disabled={!isOpen || !canOrder}
              onSaved={refreshChart}
            />
          </div>
        </div>
      )}

      {/*
        Admission sits beside "complete visit" rather than inside it, because
        it is the other ending: the doctor either finishes the visit or keeps
        the patient. Offered only while the visit is open — once it is closed
        or admitted there is nothing to admit.
      */}
      {isTerminal ? null : (
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/*
            The three unplanned exits, behind one quiet control and to the left
            of everything else. They are legal from every active state — a
            patient can die on a trolley in AWAITING_RESULTS, or walk out of the
            waiting room — so this is not gated on the consultation being open,
            unlike admission. See `_UNPLANNED_EXITS`.
          */}
          <RecordOutcome
            encounterId={encounterId}
            patientName={chart.banner.full_name}
            canRecordDeath={canRecordDeath}
            canRecordReferral={canRecordReferral}
            canRecordLama={canRecordLama}
            onRecorded={() => {
              void queryClient.invalidateQueries({ queryKey: ["queue"] });
              void refreshChart();
            }}
          />

          {isOpen && canAdmit ? (
            <Button
              variant="outline"
              className="h-tap"
              onClick={() => setAdmitting(true)}
              data-testid="admit-patient"
            >
              <BedDouble aria-hidden className="size-4" />
              {t("admitToWard")}
            </Button>
          ) : null}
        </div>
      )}

      <AdmitDialog
        encounterId={encounterId}
        patientName={chart.banner.full_name}
        patientGender={chart.banner.gender}
        open={admitting}
        onOpenChange={setAdmitting}
      />

      {isOpen && canComplete ? (
        <CompleteVisit
          encounterId={encounterId}
          pending={chart.pending}
          hasNote={chart.notes.length > 0}
          onCompleted={(next) => {
            void queryClient.invalidateQueries({ queryKey: ["queue"] });
            void refreshChart();
            toast.success(t("completedTo", { status: next.status }));
            // Straight back to the worklist: the next patient is already
            // waiting, and making the doctor navigate there is a click the
            // sixty-second budget cannot afford.
            router.push("/doctor");
          }}
        />
      ) : null}
    </div>
  );
}
