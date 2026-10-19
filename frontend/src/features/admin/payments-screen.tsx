"use client";

import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";

import { QrSvg } from "@/components/qr-svg";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { toFieldErrorMap } from "@/lib/api/field-errors";
import type { Hospital } from "@/types/api";

/**
 * The hospital's UPI ID — where every QR on reception's payment dialog sends
 * money (CLAUDE.md §9, UPI-first).
 *
 * One field, because one field is what this is. The backend validates the
 * `name@bank` shape and normalises case; its message is shown under the input.
 *
 * The QR below is the point of the screen. It carries the saved ID and no
 * amount, so the administrator can scan it with their own phone and see the
 * bank print the hospital's name before a single patient does. A wrong ID is
 * a day of payments to somebody else — the cheapest place to catch it is here.
 */
export function PaymentsScreen({
  hospitalId,
  initialUpiId,
}: {
  hospitalId: string;
  initialUpiId: string;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");
  const [value, setValue] = useState(initialUpiId);
  const [saved, setSaved] = useState(initialUpiId);
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (upiId: string) =>
      api.patch<Hospital>(`/hospitals/${hospitalId}`, { upi_vpa: upiId.trim() || null }),
    onSuccess: (hospital) => {
      const current = hospital.upi_vpa ?? "";
      setValue(current);
      setSaved(current);
      setError(null);
      toast.success(t("upiSaved"));
    },
    onError: (failure) => {
      const field = toFieldErrorMap(failure).upi_vpa;
      if (field) setError(field);
      else toast.error(isApiError(failure) ? failure.message : String(failure));
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    save.mutate(value);
  };

  return (
    <div className="max-w-xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("paymentsTitle")}</h1>
        <p className="text-muted-foreground mt-1 text-sm">{t("paymentsSubtitle")}</p>
      </div>

      <form onSubmit={submit} className="space-y-3 rounded-lg border p-4">
        <div className="space-y-1.5">
          <Label htmlFor="upi-id">{t("upiId")}</Label>
          <Input
            id="upi-id"
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
              setError(null);
            }}
            placeholder="hospital@okicici"
            autoComplete="off"
            spellCheck={false}
            aria-invalid={error !== null}
            aria-describedby="upi-id-help"
            className="h-tap text-base"
          />
          <p id="upi-id-help" className={error ? "text-destructive text-sm" : "text-muted-foreground text-xs"}>
            {error ?? t("upiIdHint")}
          </p>
        </div>
        <Button type="submit" className="h-tap" disabled={save.isPending || value === saved}>
          {common("save")}
        </Button>
      </form>

      {saved ? (
        <section className="flex items-center gap-4 rounded-lg border p-4" aria-labelledby="upi-test">
          <QrSvg
            value={`upi://pay?pa=${encodeURIComponent(saved)}&cu=INR`}
            size="9rem"
            label={t("upiTestQrLabel", { vpa: saved })}
            className="shrink-0"
          />
          <div className="space-y-1">
            <h2 id="upi-test" className="font-medium">
              {t("upiTestTitle")}
            </h2>
            <p className="text-muted-foreground text-sm">{t("upiTestHint")}</p>
            <p className="font-medium">{saved}</p>
          </div>
        </section>
      ) : null}
    </div>
  );
}
