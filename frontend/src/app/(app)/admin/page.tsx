import Link from "next/link";
import { getTranslations } from "next-intl/server";

import { NoPermission } from "@/components/no-permission";
import { visibleAdminSections } from "@/features/admin/admin-sections";
import { getCurrentUser } from "@/lib/api/server";

export async function generateMetadata() {
  const t = await getTranslations("admin");
  return { title: t("title") };
}

/**
 * The administration hub.
 *
 * Everything behind these cards was, until now, configurable only by running a
 * Python script against the database. A hospital could not set its own prices,
 * add a test to the catalogue, or create a staff account — the whole system
 * assumed configuration that only the seed script provided. That is what this
 * section is for, and why it exists ahead of the remaining clinical screens.
 *
 * Cards are filtered by permission, so a records officer sees the areas they
 * administer and nothing else.
 */
export default async function AdminPage() {
  const t = await getTranslations("admin");
  const user = await getCurrentUser();
  const sections = visibleAdminSections(user.permissions);

  if (sections.length === 0) return <NoPermission />;

  return (
    <div className="space-y-6">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-muted-foreground mt-1 text-sm">{t("subtitle")}</p>
      </div>

      <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {sections.map((section) => (
          <li key={section.href}>
            <Link
              href={section.href}
              className="hover:border-primary/50 hover:bg-accent/50 focus-visible:ring-ring block h-full rounded-lg border p-4 transition-colors focus-visible:ring-2 focus-visible:outline-none"
            >
              <section.icon aria-hidden className="text-muted-foreground size-5" />
              <h2 className="mt-3 font-medium">{t(section.titleKey as "staffTitle")}</h2>
              <p className="text-muted-foreground mt-1 text-sm">
                {t(section.descriptionKey as "staffCard")}
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
