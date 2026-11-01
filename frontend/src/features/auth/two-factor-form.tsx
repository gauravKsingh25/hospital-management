"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { useTranslations } from "next-intl";
import { AlertCircle, ShieldCheck } from "lucide-react";

import type { TwoFactorFormState } from "@/features/auth/actions";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

function Submit({ label }: { label: string }) {
  const t = useTranslations("auth");
  const { pending } = useFormStatus();
  return (
    <Button type="submit" className="h-tap w-full text-base" disabled={pending}>
      <ShieldCheck aria-hidden className="size-4" />
      {pending ? t("checkingCode") : label}
    </Button>
  );
}

/**
 * The six digits from the authenticator app (SEC-05), and whether to
 * remember this device. Used for every sign-in once two-step is on, and for
 * the first code that proves a newly set-up app works.
 */
export function TwoFactorForm({
  action,
  submitLabel,
}: {
  action: (state: TwoFactorFormState, formData: FormData) => Promise<TwoFactorFormState>;
  submitLabel: string;
}) {
  const t = useTranslations("auth");
  const [state, formAction] = useActionState(action, {});

  return (
    <form action={formAction} className="space-y-4" noValidate>
      {state.error ? (
        <Alert variant="destructive">
          <AlertCircle aria-hidden />
          <AlertDescription>{state.error}</AlertDescription>
        </Alert>
      ) : null}
      <div className="space-y-2">
        <Label htmlFor="code">{t("code")}</Label>
        <Input
          id="code"
          name="code"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9 ]*"
          maxLength={7}
          autoFocus
          className="h-tap tabular text-center text-xl tracking-[0.3em]"
          aria-invalid={Boolean(state.error)}
        />
        <p className="text-muted-foreground text-xs">{t("codeHint")}</p>
      </div>
      <label className="min-h-tap flex cursor-pointer items-center gap-3 text-sm">
        <input type="checkbox" name="remember" className="size-5" />
        {t("rememberDevice")}
      </label>
      <Submit label={submitLabel} />
    </form>
  );
}
