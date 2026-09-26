import type { LucideIcon } from "lucide-react";
import {
  Bed,
  Building2,
  FlaskConical,
  IndianRupee,
  MessageSquareText,
  Stethoscope,
  Users,
} from "lucide-react";

import type { Permission } from "@/types/rbac.generated";

/**
 * The administration areas, as data.
 *
 * Each one names the permission that gates it, and the hub filters against
 * `/auth/me` — the same mechanism as the sidebar, for the same reason: a card
 * that leads to a 403 teaches staff to distrust the screen.
 *
 * `consequence` is not marketing copy. Every one of these is something the
 * system currently cannot do without somebody running a Python script, and
 * saying what breaks without it is what tells an administrator which card
 * matters today.
 */
export type AdminSection = {
  href: string;
  icon: LucideIcon;
  /** Keys into the `admin` message namespace. */
  titleKey: string;
  descriptionKey: string;
  permissions: Permission[];
};

export const ADMIN_SECTIONS: AdminSection[] = [
  {
    href: "/admin/staff",
    icon: Users,
    titleKey: "staffTitle",
    descriptionKey: "staffCard",
    permissions: ["user:read"],
  },
  {
    href: "/admin/doctors",
    icon: Stethoscope,
    titleKey: "doctorsTitle",
    descriptionKey: "doctorsCard",
    permissions: ["doctor:read"],
  },
  {
    href: "/admin/departments",
    icon: Building2,
    titleKey: "departmentsTitle",
    descriptionKey: "departmentsCard",
    permissions: ["department:read"],
  },
  {
    href: "/admin/services",
    icon: IndianRupee,
    titleKey: "servicesTitle",
    descriptionKey: "servicesCard",
    permissions: ["service:read"],
  },
  {
    href: "/admin/catalogue",
    icon: FlaskConical,
    titleKey: "catalogueTitle",
    descriptionKey: "catalogueCard",
    permissions: ["catalogue:read"],
  },
  {
    href: "/admin/wards",
    icon: Bed,
    titleKey: "wardsTitle",
    descriptionKey: "wardsCard",
    permissions: ["ward:read"],
  },
  {
    href: "/admin/templates",
    icon: MessageSquareText,
    titleKey: "templatesTitle",
    descriptionKey: "templatesCard",
    permissions: ["notification_template:read"],
  },
];

export function visibleAdminSections(permissions: readonly string[]): AdminSection[] {
  const held = new Set(permissions);
  return ADMIN_SECTIONS.filter((section) =>
    section.permissions.some((permission) => held.has(permission)),
  );
}
