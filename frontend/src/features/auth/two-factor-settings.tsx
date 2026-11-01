"use client";

import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { ShieldCheck, ShieldOff } from "lucide-react";
import { toast } from "sonner";

import { QrSvg } from "@/components/qr-svg";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";

type Secret = { secret: string; otpauth_uri: string };

/**
 * Turning two-step sign-in on or off for yourself (SEC-05).
 *
 * On: scan, then prove the app works with one code. Off: password and a
 * current code — and not at all for a role that requires it.
 */
export function TwoFactorSettings({ enabled, required }: { enabled: boolean; required: boolean }) {
  const t = useTranslations("auth");
  const router = useRouter();
  const [secret, setSecret] = useState<Secret | null>(null);

  const fail = (error: unknown) => toast.error(isApiError(error) ? error.message : String(error));

  const setup = useMutation({
    mutationFn: () => api.post<Secret>("/auth/2fa/setup"),
    onSuccess: setSecret,
    onError: fail,
  });
  const enable = useMutation({
    mutationFn: (code: string) => api.post("/auth/2fa/enable", { code }),
    onSuccess: () => {
      toast.success(t("twoFactorTurnedOn"));
      setSecret(null);
      router.refresh();
    },
    onError: fail,
  });
  const disable = useMutation({
    mutationFn: (body: { password: string; code: string }) => api.post("/auth/2fa/disable", body),
    onSuccess: () => {
      toast.success(t("twoFactorTurnedOff"));
      router.refresh();
    },
    onError: fail,
  });

  const field = (event: FormEvent<HTMLFormElement>, name: string) =>
    String(new FormData(event.currentTarget).get(name) ?? "").replace(/\s/g, "");

  if (enabled) {
    return (
      <div className="space-y-4">
        <p className="flex items-center gap-2 text-sm font-medium" data-testid="two-factor-status">
          <ShieldCheck aria-hidden className="text-success size-5" />
          {t("twoFactorOn")}
        </p>
        {required ? (
          <p className="text-muted-foreground text-sm">{t("twoFactorRequired")}</p>
        ) : (
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              disable.mutate({
                password: String(new FormData(event.currentTarget).get("password") ?? ""),
                code: field(event, "code"),
              });
            }}
          >
            <div className="space-y-2">
              <Label htmlFor="off-password">{t("password")}</Label>
              <Input id="off-password" name="password" type="password" className="h-tap" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="off-code">{t("code")}</Label>
              <Input id="off-code" name="code" inputMode="numeric" className="h-tap tabular" />
            </div>
            <Button type="submit" variant="outline" className="h-tap" disabled={disable.isPending}>
              <ShieldOff aria-hidden className="size-4" />
              {t("turnOff")}
            </Button>
          </form>
        )}
      </div>
    );
  }

  if (!secret) {
    return (
      <div className="space-y-4">
        <p className="text-muted-foreground text-sm" data-testid="two-factor-status">
          {t("twoFactorOff")}
        </p>
        <Button className="h-tap" onClick={() => setup.mutate()} disabled={setup.isPending}>
          <ShieldCheck aria-hidden className="size-4" />
          {t("setUp")}
        </Button>
      </div>
    );
  }

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        enable.mutate(field(event, "code"));
      }}
    >
      <p className="text-sm">{t("twoFactorSetupHint")}</p>
      <div className="flex justify-center rounded-lg bg-white p-2 sm:justify-start">
        <QrSvg value={secret.otpauth_uri} size="12rem" label={t("twoFactorQr")} />
      </div>
      <div className="space-y-1">
        <p className="text-muted-foreground text-xs">{t("twoFactorTypeKey")}</p>
        <p className="tabular font-mono text-sm break-all" data-testid="totp-secret">
          {secret.secret.match(/.{1,4}/g)?.join(" ")}
        </p>
      </div>
      <div className="space-y-2">
        <Label htmlFor="on-code">{t("code")}</Label>
        <Input
          id="on-code"
          name="code"
          inputMode="numeric"
          autoComplete="one-time-code"
          className="h-tap tabular max-w-48"
        />
      </div>
      <Button type="submit" className="h-tap" disabled={enable.isPending}>
        {t("turnOn")}
      </Button>
    </form>
  );
}
