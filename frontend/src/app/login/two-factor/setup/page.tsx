import { redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { ShieldCheck } from "lucide-react";

import { QrSvg } from "@/components/qr-svg";
import { confirmTwoFactorEnrolment } from "@/features/auth/actions";
import { TwoFactorForm } from "@/features/auth/two-factor-form";
import { ApiError } from "@/lib/api/error";
import { callApi } from "@/lib/api/fetch";
import { readTwoFactorChallenge } from "@/lib/two-factor";

export async function generateMetadata() {
  const t = await getTranslations("auth");
  return { title: t("twoFactorSetupTitle") };
}

/**
 * First sign-in for a role that requires two-step sign-in (SEC-05): scan the
 * code into an authenticator app, then type the six digits it shows. Two-step
 * is only switched on once that code proves the app holds the secret.
 */
export default async function TwoFactorSetupPage() {
  const t = await getTranslations("auth");
  const challenge = await readTwoFactorChallenge();
  if (!challenge) redirect("/login");
  if (!challenge.enrol) redirect("/login/two-factor");

  let enrolment: { secret: string; otpauth_uri: string };
  try {
    enrolment = await callApi<{ secret: string; otpauth_uri: string }>("/auth/2fa/enrol/start", {
      method: "POST",
      body: { challenge_token: challenge.token },
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/login");
    throw error;
  }

  // Grouped in fours, the way authenticator apps ask for a typed key.
  const grouped = enrolment.secret.match(/.{1,4}/g)?.join(" ") ?? enrolment.secret;

  return (
    <main className="flex min-h-svh flex-col items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm space-y-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <ShieldCheck aria-hidden className="text-primary size-8" />
          <h1 className="text-2xl font-semibold tracking-tight">{t("twoFactorSetupTitle")}</h1>
          <p className="text-muted-foreground text-sm">{t("twoFactorSetupHint")}</p>
        </div>
        <div className="bg-card space-y-5 rounded-xl border p-6 shadow-sm">
          <div className="flex justify-center rounded-lg bg-white p-2">
            <QrSvg value={enrolment.otpauth_uri} size="12rem" label={t("twoFactorQr")} />
          </div>
          <div className="space-y-1 text-center">
            <p className="text-muted-foreground text-xs">{t("twoFactorTypeKey")}</p>
            <p className="tabular font-mono text-sm break-all" data-testid="totp-secret">
              {grouped}
            </p>
          </div>
          <TwoFactorForm action={confirmTwoFactorEnrolment} submitLabel={t("turnOnAndSignIn")} />
        </div>
      </div>
    </main>
  );
}
