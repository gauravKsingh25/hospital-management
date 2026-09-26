import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { RegistrationForm } from "@/features/patients/registration-form";
import { getCurrentUser } from "@/lib/api/server";
import { can } from "@/lib/permissions";

export async function generateMetadata() {
  const t = await getTranslations("registration");
  return { title: t("title") };
}

export default async function RegisterPatientPage() {
  const t = await getTranslations("registration");
  const user = await getCurrentUser();

  // A second, softer guard. FastAPI refuses regardless — this only means
  // somebody who followed a stale link gets a clear "not for you" instead of
  // a form that fails on submit with everything already typed into it.
  if (!can(user, "patient:create")) return <NoPermission />;

  return (
    <div className="space-y-6">
      {/* Not on the printed slip. This screen prints the token, and the
          artifact a patient carries away should not be headed "Register a
          patient — 4 fields. Everything else can be added later." */}
      <header className="mx-auto max-w-2xl no-print">
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-muted-foreground mt-1 text-sm">{t("subtitle")}</p>
      </header>

      <RegistrationForm />
    </div>
  );
}
