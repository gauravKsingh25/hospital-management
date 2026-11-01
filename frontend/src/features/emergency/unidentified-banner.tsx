"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { UserSearch } from "lucide-react";

import { Button } from "@/components/ui/button";
import { IdentifyDialog, type UnidentifiedPatient } from "@/features/emergency/identify-dialog";

/**
 * On the record of a patient nobody has named yet (UX-01): says so plainly,
 * says what it costs — no messages reach them — and offers the way out.
 */
export function UnidentifiedBanner({
  patient,
  canIdentify,
}: {
  patient: UnidentifiedPatient;
  canIdentify: boolean;
}) {
  const t = useTranslations("emergency");
  const [open, setOpen] = useState(false);

  return (
    <div
      role="status"
      className="border-destructive/40 bg-destructive/10 flex flex-wrap items-center gap-3 rounded-lg border px-4 py-3"
      data-testid="unidentified-banner"
    >
      <UserSearch aria-hidden className="text-destructive size-5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="text-destructive text-sm font-semibold">{t("notIdentified")}</p>
        <p className="text-muted-foreground text-xs">{t("notIdentifiedHint")}</p>
      </div>
      {canIdentify ? (
        <>
          <Button type="button" className="h-tap" onClick={() => setOpen(true)}>
            {t("identify")}
          </Button>
          <IdentifyDialog patient={patient} open={open} onOpenChange={setOpen} />
        </>
      ) : null}
    </div>
  );
}
