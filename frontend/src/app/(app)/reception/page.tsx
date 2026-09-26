import { getTranslations } from "next-intl/server";
import { UserPlus, Users } from "lucide-react";

import { DoctorQueues } from "@/features/queue/doctor-queues";
import { CameraScanButton } from "@/features/scan/camera-scan-button";
import { DelayAlert } from "@/features/reports/delay-alert";
import { NoPermission } from "@/components/no-permission";
import { LinkButton } from "@/components/ui/link-button";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("reception") };
}

/**
 * Reception's home screen (CLAUDE.md §7b, role-based dashboards).
 *
 * Two things, in the order they are needed: the button that starts the most
 * repeated action of the day, and the queue — one column per doctor, busiest
 * first — that answers the two questions reception is asked all morning:
 * "how long?" and "can I see someone else?". The per-doctor counts are what
 * lets them balance the load, and the Move action on each row is what lets
 * them act on it without leaving the screen.
 *
 * The one thing they cannot see from the queue is *which clinic is behind* —
 * that needs the wait times compared against a threshold — so `DelayAlert`
 * (CLAUDE.md §7b) sits above it and renders nothing at all unless a doctor is
 * running late. It goes here rather than on the reports dashboard because the
 * person who needs it has a patient in front of them asking how long.
 */
export default async function ReceptionPage() {
  const t = await getTranslations("nav");
  const tRegister = await getTranslations("registration");
  const user = await getCurrentUser();

  if (!can(user, "patient:read")) return <NoPermission />;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">{t("reception")}</h1>

        <div className="flex flex-wrap items-center gap-2">
          {/* No permission check: the page already returns early without
              `patient:read`. Renders nothing unless the device can actually
              scan — a counter PC has a USB scanner and no `BarcodeDetector`,
              so most of the time nothing appears here at all. */}
          <CameraScanButton />

          {can(user, "patient:create") ? (
            <LinkButton className="h-tap" href="/reception/register">
              <UserPlus aria-hidden className="size-4" />
              {tRegister("title")}
              {/* The key is shown on the button itself. A shortcut nobody can
                  discover is a shortcut nobody uses. */}
              <kbd className="bg-primary-foreground/15 ml-1 hidden rounded px-1.5 py-0.5 text-[10px] font-medium sm:inline-block">
                F2
              </kbd>
            </LinkButton>
          ) : null}
        </div>
      </header>

      {can(user, "report:operational") ? <DelayAlert /> : null}

      {can(user, "queue:read") ? (
        <DoctorQueues
          canManage={can(user, "queue:manage")}
          canRequestAdmission={can(user, "admission:request")}
        />
      ) : (
        <div className="text-muted-foreground flex flex-col items-center gap-3 rounded-lg border border-dashed py-16">
          <Users aria-hidden className="size-6" />
          <p className="text-sm">{(await getTranslations("common"))("noPermission")}</p>
        </div>
      )}
    </div>
  );
}
