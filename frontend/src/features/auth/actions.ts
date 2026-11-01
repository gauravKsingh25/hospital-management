"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { z } from "zod";

import { ApiError } from "@/lib/api/error";
import { callApi } from "@/lib/api/fetch";
import { serverFetch } from "@/lib/api/server";
import { readTwoFactorChallenge } from "@/lib/two-factor";
import {
  DEVICE_COOKIE_NAME,
  LOCK_COOKIE_NAME,
  TWO_FACTOR_COOKIE_NAME,
  TWO_FACTOR_MAX_AGE_SECONDS,
  DEVICE_MAX_AGE_SECONDS,
  clearSession,
  readSession,
  sessionFromTokens,
  writeSession,
  type TokenPair,
} from "@/lib/session";

/**
 * Sign-in, sign-out and password change.
 *
 * These are server actions rather than calls through `/bff` because each one
 * writes the session cookie, and only server actions and route handlers can.
 * That is also why `/bff` refuses `auth/login`: there is exactly one way to
 * obtain a session in this application, and it is this file.
 */

/** SEC-05: what `/auth/login` answers when an authenticator code is next. */
type TwoFactorChallenge = {
  two_factor_required: true;
  enrol: boolean;
  challenge_token: string;
  expires_in: number;
};

export type AuthFormState = {
  error?: string;
  fieldErrors?: Partial<Record<"email" | "password" | "currentPassword" | "newPassword" | "confirmPassword", string>>;
};

/**
 * Only ever redirect within this application — see the same guard in
 * `app/auth/refresh/route.ts`. `next` comes from a query string, so without
 * this a crafted link turns the hospital's login page into an open redirect.
 */
function safeNext(raw: FormDataEntryValue | null): string {
  if (typeof raw !== "string" || !raw) return "/";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  return raw;
}

const credentials = z.object({
  email: z.string().trim().min(1).pipe(z.email()),
  password: z.string().min(1),
});

export async function signIn(
  _previous: AuthFormState,
  formData: FormData,
): Promise<AuthFormState> {
  const t = await getTranslations("auth");

  const parsed = credentials.safeParse({
    email: formData.get("email"),
    password: formData.get("password"),
  });

  if (!parsed.success) {
    const fieldErrors: AuthFormState["fieldErrors"] = {};
    for (const issue of parsed.error.issues) {
      const field = issue.path[0];
      if (field === "email") fieldErrors.email = t("emailInvalid");
      if (field === "password") fieldErrors.password = t("passwordRequired");
    }
    return { fieldErrors };
  }

  const store = await cookies();
  let result: TokenPair | TwoFactorChallenge;
  try {
    result = await callApi<TokenPair | TwoFactorChallenge>("/auth/login", {
      method: "POST",
      body: { ...parsed.data, device_token: store.get(DEVICE_COOKIE_NAME)?.value ?? null },
    });
  } catch (error) {
    if (error instanceof ApiError) {
      // Deliberately the same message for "no such account" and "wrong
      // password". Distinguishing them tells an attacker which hospital
      // email addresses are real, which is the first step of a targeted
      // phishing campaign against named staff.
      if (error.status === 401 || error.status === 403) {
        return { error: t("invalidCredentials") };
      }
      return { error: error.message };
    }
    throw error;
  }

  const next = safeNext(formData.get("next"));
  if ("two_factor_required" in result) {
    // SEC-05: the password was right; the code from the app comes next.
    store.set({
      name: TWO_FACTOR_COOKIE_NAME,
      value: JSON.stringify({ token: result.challenge_token, enrol: result.enrol, next }),
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "strict",
      path: "/",
      maxAge: TWO_FACTOR_MAX_AGE_SECONDS,
    });
    redirect(result.enrol ? "/login/two-factor/setup" : "/login/two-factor");
  }

  const session = sessionFromTokens(result);
  writeSession(store, session);
  // Signing in afresh retires any idle lock left on this browser (SEC-04).
  store.delete(LOCK_COOKIE_NAME);

  // `redirect` throws, so nothing below runs. It is outside the try above on
  // purpose: catching it would swallow the navigation and leave the user
  // staring at the login form after a successful sign-in.
  redirect(session.mustChangePassword ? "/change-password" : next);
}

export async function signOut(): Promise<never> {
  const session = await readSession();
  const store = await cookies();

  // Clear locally first. If the backend call fails, the user is still signed
  // out of this browser — the opposite order can leave someone unable to log
  // out because the server is unreachable, which is the moment they most want
  // to (a shared machine they are walking away from).
  clearSession(store);
  store.delete(LOCK_COOKIE_NAME);

  if (session) {
    try {
      await callApi("/auth/logout", {
        method: "POST",
        body: { refresh_token: session.refreshToken },
      });
    } catch {
      // Best effort. The refresh token is now unreachable by this browser and
      // expires on its own; a failed revocation is not worth blocking on.
    }
  }

  redirect("/login");
}

const passwordChange = z
  .object({
    currentPassword: z.string().min(1),
    newPassword: z.string().min(12),
    confirmPassword: z.string().min(1),
  })
  .refine((value) => value.newPassword === value.confirmPassword, {
    path: ["confirmPassword"],
  });

export async function changePassword(
  _previous: AuthFormState,
  formData: FormData,
): Promise<AuthFormState> {
  const t = await getTranslations("auth");

  const parsed = passwordChange.safeParse({
    currentPassword: formData.get("currentPassword"),
    newPassword: formData.get("newPassword"),
    confirmPassword: formData.get("confirmPassword"),
  });

  if (!parsed.success) {
    const fieldErrors: AuthFormState["fieldErrors"] = {};
    for (const issue of parsed.error.issues) {
      const field = issue.path[0];
      if (field === "currentPassword") fieldErrors.currentPassword = t("passwordRequired");
      if (field === "newPassword") fieldErrors.newPassword = t("passwordTooShort", { min: 12 });
      if (field === "confirmPassword") fieldErrors.confirmPassword = t("passwordsDoNotMatch");
    }
    return { fieldErrors };
  }

  try {
    await serverFetch("/auth/change-password", {
      method: "POST",
      body: {
        current_password: parsed.data.currentPassword,
        new_password: parsed.data.newPassword,
      },
    });
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 401) return { fieldErrors: { currentPassword: t("invalidCredentials") } };
      // The backend owns the password policy (CLAUDE.md §12), so its message
      // is shown rather than a guess at what the rule was.
      return { fieldErrors: { newPassword: error.message } };
    }
    throw error;
  }

  // The old session carries `must_change_password`, and `proxy.ts` would send
  // the user straight back here. Signing out forces a clean login with the
  // new password — which is also the only way to be sure it works.
  return signOut();
}

// ---------------------------------------------------------------------------
// Two-step sign-in (SEC-05)
// ---------------------------------------------------------------------------
export type TwoFactorFormState = { error?: string };

async function finishTwoFactor(
  path: "/auth/2fa/verify" | "/auth/2fa/enrol/confirm",
  formData: FormData,
): Promise<TwoFactorFormState> {
  const t = await getTranslations("auth");
  const challenge = await readTwoFactorChallenge();
  // Five minutes have passed, or the page was opened directly: start again.
  if (!challenge) redirect("/login");

  const code = String(formData.get("code") ?? "").replace(/\s/g, "");
  if (!/^\d{6}$/.test(code)) return { error: t("codeFormat") };

  let tokens: TokenPair;
  try {
    tokens = await callApi<TokenPair>(path, {
      method: "POST",
      body: {
        challenge_token: challenge.token,
        code,
        remember_device: formData.get("remember") === "on",
      },
    });
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.code === "invalid_code") return { error: t("codeInvalid") };
      if (error.code === "account_locked") return { error: error.message };
      // The challenge expired or is otherwise spent: back to the password.
      if (error.status === 401) redirect("/login");
      return { error: error.message };
    }
    throw error;
  }

  const store = await cookies();
  const session = sessionFromTokens(tokens);
  writeSession(store, session);
  store.delete(TWO_FACTOR_COOKIE_NAME);
  store.delete(LOCK_COOKIE_NAME);
  if (tokens.device_token) {
    store.set({
      name: DEVICE_COOKIE_NAME,
      value: tokens.device_token,
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: DEVICE_MAX_AGE_SECONDS,
    });
  }
  redirect(session.mustChangePassword ? "/change-password" : challenge.next);
}

/** The code from the app, for someone who has two-step sign-in on. */
export async function verifyTwoFactor(
  _previous: TwoFactorFormState,
  formData: FormData,
): Promise<TwoFactorFormState> {
  return finishTwoFactor("/auth/2fa/verify", formData);
}

/** The first code from a newly set-up app: switches two-step on and signs in. */
export async function confirmTwoFactorEnrolment(
  _previous: TwoFactorFormState,
  formData: FormData,
): Promise<TwoFactorFormState> {
  return finishTwoFactor("/auth/2fa/enrol/confirm", formData);
}
