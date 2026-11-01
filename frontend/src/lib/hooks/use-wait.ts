"use client";

import { useTranslations } from "next-intl";

import { splitWait } from "@/lib/duration";

/**
 * A wait in the reader's language, in units read at a glance (UX-04):
 * "45 min", "2 h 5 min", "2 d 21 h". See `lib/duration.ts`.
 */
export function useWaitFormatter(): (totalMinutes: number) => string {
  const t = useTranslations("common");
  return (totalMinutes) => {
    const { days, hours, minutes } = splitWait(totalMinutes);
    if (days > 0) return t("waitDays", { days, hours });
    if (hours > 0) return t("waitHours", { hours, minutes });
    return t("waitMinutes", { minutes });
  };
}
