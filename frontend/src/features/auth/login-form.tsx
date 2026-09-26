"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { useTranslations } from "next-intl";
import { AlertCircle, LogIn } from "lucide-react";

import { signIn, type AuthFormState } from "@/features/auth/actions";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Sign-in.
 *
 * A plain `<form action={…}>` posting to a server action, which means it
 * works before React has hydrated — a real consideration on the ageing
 * counter machines this runs on, where the first paint and the first
 * interaction can be seconds apart.
 *
 * The password never touches client-side state: it goes from the input into
 * the FormData and out to the server. Nothing holds it in a React variable
 * where a devtools snapshot or an error reporter could pick it up.
 */
function SubmitButton() {
  const t = useTranslations("auth");
  // `useFormStatus` reads the parent form's state, so the button knows it is
  // submitting without the form having to thread a prop down to it.
  const { pending } = useFormStatus();

  return (
    <Button type="submit" className="h-tap w-full text-base" disabled={pending}>
      {pending ? (
        t("signingIn")
      ) : (
        <>
          <LogIn aria-hidden className="size-4" />
          {t("signIn")}
        </>
      )}
    </Button>
  );
}

export function LoginForm({ next }: { next: string }) {
  const t = useTranslations("auth");
  const [state, formAction] = useActionState<AuthFormState, FormData>(signIn, {});

  return (
    <form action={formAction} className="space-y-5" noValidate>
      <input type="hidden" name="next" value={next} />

      {state.error ? (
        <Alert variant="destructive" role="alert">
          <AlertCircle aria-hidden className="size-4" />
          <AlertDescription>{state.error}</AlertDescription>
        </Alert>
      ) : null}

      <div className="space-y-2">
        <Label htmlFor="email" className="text-sm font-medium">
          {t("email")}
        </Label>
        <Input
          id="email"
          name="email"
          type="email"
          inputMode="email"
          autoComplete="username"
          // The cursor lands in the first field on load, so signing in is
          // type-tab-type-enter without touching the mouse.
          autoFocus
          required
          placeholder={t("emailPlaceholder")}
          className="h-tap text-base"
          aria-invalid={Boolean(state.fieldErrors?.email)}
          aria-describedby={state.fieldErrors?.email ? "email-error" : undefined}
        />
        {state.fieldErrors?.email ? (
          <p id="email-error" className="text-destructive text-sm">
            {state.fieldErrors.email}
          </p>
        ) : null}
      </div>

      <div className="space-y-2">
        <Label htmlFor="password" className="text-sm font-medium">
          {t("password")}
        </Label>
        <Input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          className="h-tap text-base"
          aria-invalid={Boolean(state.fieldErrors?.password)}
          aria-describedby={state.fieldErrors?.password ? "password-error" : undefined}
        />
        {state.fieldErrors?.password ? (
          <p id="password-error" className="text-destructive text-sm">
            {state.fieldErrors.password}
          </p>
        ) : null}
      </div>

      <SubmitButton />
    </form>
  );
}
