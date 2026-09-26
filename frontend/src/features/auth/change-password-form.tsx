"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { useTranslations } from "next-intl";

import { changePassword, type AuthFormState } from "@/features/auth/actions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Set a new password.
 *
 * The rules are the backend's (CLAUDE.md §12 uses a length-first policy with
 * no composition requirements). This form checks only that the two entries
 * match and that the new one is long enough to be worth sending; anything
 * else the server refuses comes back as a message attached to the field.
 * Restating the policy here would be a second copy to keep in step, and the
 * copy that is wrong is always the one the user is looking at.
 */
function SubmitButton() {
  const t = useTranslations("auth");
  const { pending } = useFormStatus();

  return (
    <Button type="submit" className="h-tap w-full text-base" disabled={pending}>
      {pending ? t("signingIn") : t("changePassword")}
    </Button>
  );
}

export function ChangePasswordForm() {
  const t = useTranslations("auth");
  const [state, formAction] = useActionState<AuthFormState, FormData>(changePassword, {});

  return (
    <form action={formAction} className="space-y-5" noValidate>
      <Field
        id="currentPassword"
        label={t("currentPassword")}
        autoComplete="current-password"
        autoFocus
        error={state.fieldErrors?.currentPassword}
      />
      <Field
        id="newPassword"
        label={t("newPassword")}
        autoComplete="new-password"
        error={state.fieldErrors?.newPassword}
      />
      <Field
        id="confirmPassword"
        label={t("confirmPassword")}
        autoComplete="new-password"
        error={state.fieldErrors?.confirmPassword}
      />
      <SubmitButton />
    </form>
  );
}

function Field({
  id,
  label,
  error,
  ...props
}: {
  id: string;
  label: string;
  error?: string;
} & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        name={id}
        type="password"
        required
        className="h-tap text-base"
        aria-invalid={Boolean(error)}
        aria-describedby={error ? `${id}-error` : undefined}
        {...props}
      />
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}
    </div>
  );
}
