"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Lock } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { signOut } from "@/features/auth/actions";
import { setSessionLocked } from "@/lib/api/client";

// Minutes without a touch, a key or a scroll before the screen locks. Ten by
// default; a deployment sets NEXT_PUBLIC_IDLE_LOCK_MINUTES to change it.
const IDLE_MINUTES = Number(process.env.NEXT_PUBLIC_IDLE_LOCK_MINUTES) || 10;
const ACTIVITY = ["pointerdown", "keydown", "wheel", "touchstart"] as const;

type UnlockResult = { unlocked?: boolean; signedOut?: boolean; error?: string };

/**
 * Lock a shared tablet nobody is using (SEC-04).
 *
 * A ward tablet left on the counter stayed signed in as the last nurse for
 * the rest of the browser session. Now, after a few idle minutes, the screen
 * goes opaque and the session ends on the server. The page underneath stays
 * mounted, so a half-written note or a half-filled form is still there when
 * the same person types their password; "Switch user" signs them out for
 * whoever is picking the tablet up.
 *
 * Locking and unlocking go through `fetch` to route handlers, not server
 * actions: an action that changes cookies re-renders the page on the server,
 * and with the session ended that re-render is a redirect to the login form.
 */
export function IdleLock({ user }: { user: { full_name: string; email: string } }) {
  const t = useTranslations("auth");
  const queryClient = useQueryClient();
  const router = useRouter();
  const [locked, setLocked] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const lastActivity = useRef(0);
  // The lock request, kept so an unlock never overtakes it: an unlock that
  // arrives first finds no lock cookie and is answered "signed out".
  const lockRequest = useRef<Promise<unknown> | null>(null);

  useEffect(() => {
    if (locked) return;
    lastActivity.current = Date.now();
    const touch = () => {
      lastActivity.current = Date.now();
    };
    for (const event of ACTIVITY) window.addEventListener(event, touch, { passive: true });

    const timer = window.setInterval(() => {
      if (Date.now() - lastActivity.current < IDLE_MINUTES * 60_000) return;
      window.clearInterval(timer);
      // Set before the session ends, so the 401s that follow do not navigate.
      setSessionLocked(true);
      setLocked(true);
      lockRequest.current = fetch("/auth/lock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: user.email }),
        credentials: "same-origin",
      }).catch(() => undefined);
    }, 15_000);

    return () => {
      window.clearInterval(timer);
      for (const event of ACTIVITY) window.removeEventListener(event, touch);
    };
  }, [locked, user.email]);

  const unlock = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const password = new FormData(event.currentTarget).get("password");
    if (typeof password !== "string" || !password) {
      setError(t("passwordRequired"));
      return;
    }
    setPending(true);
    setError(null);
    try {
      await lockRequest.current;
      const response = await fetch("/auth/unlock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
        credentials: "same-origin",
      });
      const result = (await response.json()) as UnlockResult;
      if (result.signedOut) {
        // The lock outlived its cookie. A hard load, as in `lib/api/client.ts`:
        // it discards the query cache, which may hold the previous user's
        // patients.
        window.location.assign(new URL("/login", window.location.origin));
        return;
      }
      if (result.unlocked) {
        setSessionLocked(false);
        setLocked(false);
        // Everything fetched while locked failed; fetch it again.
        void queryClient.invalidateQueries();
        router.refresh();
        return;
      }
      setError(result.error ?? t("invalidCredentials"));
    } catch {
      setError(t("invalidCredentials"));
    } finally {
      setPending(false);
    }
  };

  if (!locked) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="idle-lock-title"
      // Opaque, not a dimmed overlay: the patient list underneath must not be
      // readable by whoever walks past the counter.
      className="bg-background fixed inset-0 z-[100] flex items-center justify-center p-4"
      data-testid="idle-lock"
    >
      <div className="w-full max-w-sm space-y-5 text-center">
        <Lock aria-hidden className="text-muted-foreground mx-auto size-8" />
        <div className="space-y-1">
          <h2 id="idle-lock-title" className="text-xl font-semibold">
            {t("lockTitle")}
          </h2>
          <p className="text-muted-foreground text-sm">
            {t("lockHint", { name: user.full_name })}
          </p>
        </div>

        <form onSubmit={unlock} className="space-y-3 text-left" noValidate>
          <div className="space-y-2">
            <Label htmlFor="lock-password">{t("password")}</Label>
            <Input
              id="lock-password"
              name="password"
              type="password"
              autoComplete="current-password"
              autoFocus
              className="h-tap text-base"
              aria-invalid={Boolean(error)}
            />
            {error ? (
              <p className="text-destructive text-sm" role="alert">
                {error}
              </p>
            ) : null}
          </div>
          <Button type="submit" className="h-tap w-full" disabled={pending}>
            {pending ? t("unlocking") : t("unlock")}
          </Button>
        </form>

        <form action={signOut}>
          <Button type="submit" variant="outline" className="h-tap w-full">
            {t("switchUser")}
          </Button>
        </form>
      </div>
    </div>
  );
}
