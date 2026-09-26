import { getTranslations } from "next-intl/server";

import { LiveQueue } from "@/features/queue/live-queue";
import { NoPermission } from "@/components/no-permission";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("myPatients") };
}

/**
 * The doctor's worklist (CLAUDE.md §7b).
 *
 * One list, no dashboard. A doctor arriving at their screen wants to know who
 * is next and to be one click from that patient's chart — counts, charts and
 * summaries are for the people who manage the clinic, not the person running
 * it thirty patients deep.
 *
 * `/queue/mine` resolves the doctor from the signed-in user, so nobody has to
 * know their own record id, and a doctor covering someone else's clinic does
 * not see the wrong list.
 */
export default async function DoctorPage() {
  const t = await getTranslations("queue");
  const user = await getCurrentUser();

  if (!can(user, "queue:read")) return <NoPermission />;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
      <LiveQueue mine showConsultLink />
    </div>
  );
}
