"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { z } from "zod";

import { ApiError } from "@/lib/api/error";
import { callApi } from "@/lib/api/fetch";
import { serverFetch } from "@/lib/api/server";
import {
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

  let tokens: TokenPair;
  try {
    tokens = await callApi<TokenPair>("/auth/login", {
      method: "POST",
      body: parsed.data,
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

  const session = sessionFromTokens(tokens);
  writeSession(await cookies(), session);

  // `redirect` throws, so nothing below runs. It is outside the try above on
  // purpose: catching it would swallow the navigation and leave the user
  // staring at the login form after a successful sign-in.
  redirect(session.mustChangePassword ? "/change-password" : safeNext(formData.get("next")));
}

export async function signOut(): Promise<never> {
  const session = await readSession();
  const store = await cookies();

  // Clear locally first. If the backend call fails, the user is still signed
  // out of this browser — the opposite order can leave someone unable to log
  // out because the server is unreachable, which is the moment they most want
  // to (a shared machine they are walking away from).
  clearSession(store);

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
