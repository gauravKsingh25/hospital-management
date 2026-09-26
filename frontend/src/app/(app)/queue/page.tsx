import { getTranslations } from "next-intl/server";

import { LiveQueue } from "@/features/queue/live-queue";
import { CameraScanButton } from "@/features/scan/camera-scan-button";
import { NoPermission } from "@/components/no-permission";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("queue");
  return { title: t("title") };
}

/**
 * The whole hospital's queue — nursing's screen, and reception's second one.
 *
 * The same component as the doctor's list, unfiltered. A nurse calling
 * patients for vitals works across every clinic, so scoping this to one
 * doctor would make them switch lists all morning.
 */
export default async function QueuePage() {
  const t = await getTranslations("queue");
  const user = await getCurrentUser();

  if (!can(user, "queue:read")) return <NoPermission />;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        {/* The screen this matters most on: a nurse working the queue from a
            tablet has no USB scanner within reach. */}
        {can(user, "patient:read") ? <CameraScanButton /> : null}
      </header>
      <LiveQueue
        showConsultLink={can(user, "encounter:complete")}
        canRecordVitals={can(user, "vitals:record")}
      />
    </div>
  );
}
