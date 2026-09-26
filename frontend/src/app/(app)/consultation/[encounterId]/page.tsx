import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { ConsultationScreen } from "@/features/consultation/consultation-screen";
import { NoPermission } from "@/components/no-permission";
import { ApiError } from "@/lib/api/error";
import { getCurrentUser, serverFetch } from "@/lib/api/server";
import { can } from "@/lib/permissions";
import type { EncounterChart, NoteTemplate } from "@/types/api";

export async function generateMetadata() {
  const t = await getTranslations("consultation");
  return { title: t("title") };
}

/**
 * The consultation screen.
 *
 * The chart arrives in **one** request. `/encounters/{id}/chart` returns the
 * encounter, the safety banner, vitals, notes, diagnoses, orders and the
 * pending list together — assembled server-side precisely so this screen
 * opens in one round trip rather than six. On a hospital connection that is
 * most of the difference between meeting CLAUDE.md §7b's 60-second target and
 * missing it.
 *
 * Fetched here, in a server component, so the first paint already contains
 * the patient's name and their allergies. A client-side fetch would render an
 * empty frame first — and an empty frame is a moment where a doctor can start
 * reading a chart that has no safety banner on it yet.
 *
 * Templates are fetched alongside because they are the other half of the
 * low-doctor-effort goal (§7), and waiting for them after the page loads
 * would mean the first thing a doctor reaches for is the slowest.
 */
export default async function ConsultationPage({
  params,
}: PageProps<"/consultation/[encounterId]">) {
  const { encounterId } = await params;
  const user = await getCurrentUser();

  if (!can(user, "note:write") && !can(user, "encounter:read")) {
    return <NoPermission />;
  }

  let chart: EncounterChart;
  try {
    chart = await serverFetch<EncounterChart>(`/encounters/${encounterId}/chart`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    if (error instanceof ApiError && error.isForbidden) return <NoPermission />;
    throw error;
  }

  // Templates are a convenience, not a requirement. A doctor whose role lacks
  // `template:read` still gets a fully working screen, just without the
  // shortcuts — so this failing must not take the consultation down with it.
  let templates: NoteTemplate[] = [];
  if (can(user, "template:read")) {
    try {
      templates = await serverFetch<NoteTemplate[]>("/note-templates");
    } catch {
      templates = [];
    }
  }

  return (
    <ConsultationScreen
      encounterId={encounterId}
      initialChart={chart}
      templates={templates}
      canWrite={can(user, "note:write")}
      canDiagnose={can(user, "diagnosis:record")}
      canOrder={can(user, "order:place")}
      canComplete={can(user, "encounter:complete")}
      // `admission:create`, not `encounter:admit`: this button takes a bed as
      // well as moving the visit, and taking a bed is the IPD module's act.
      canAdmit={can(user, "admission:create")}
      // Three permissions rather than one (§6, §8). A records officer holds
      // all three and none of the clinical ones; a doctor holds both sets.
      // Keeping them apart is what lets a hospital give the death register to
      // records without also handing them a prescription pad.
      canRecordDeath={can(user, "encounter:record_death")}
      canRecordReferral={can(user, "encounter:record_referral")}
      canRecordLama={can(user, "encounter:record_lama")}
    />
  );
}
