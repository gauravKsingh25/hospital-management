"use client";

import { useRouter } from "next/navigation";

import { RecordOutcome } from "@/features/outcomes/terminal-outcome";

/**
 * Record a death, a referral out or a LAMA against an open visit, then
 * refresh the page it sits on.
 *
 * This used to live only on the doctor's consultation screen, which no longer
 * exists: doctors see OPD patients in person and do no data entry. The patient
 * page (any open visit) and the admission page (an inpatient) carry it now,
 * for whoever holds the permission — records officer, administrator, doctor.
 * The structured metadata §6 requires is the same as before: the dialog and
 * the backend are unchanged.
 */
export function VisitOutcomeAction({
  encounterId,
  patientName,
  canRecordDeath,
  canRecordReferral,
  canRecordLama,
  currentUserId,
}: {
  encounterId: string;
  patientName: string;
  canRecordDeath: boolean;
  canRecordReferral: boolean;
  canRecordLama: boolean;
  currentUserId?: string;
}) {
  const router = useRouter();
  return (
    <RecordOutcome
      encounterId={encounterId}
      patientName={patientName}
      canRecordDeath={canRecordDeath}
      canRecordReferral={canRecordReferral}
      canRecordLama={canRecordLama}
      currentUserId={currentUserId}
      onRecorded={() => router.refresh()}
    />
  );
}
