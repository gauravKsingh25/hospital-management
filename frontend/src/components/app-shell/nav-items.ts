import type { LucideIcon } from "lucide-react";
import {
  BedDouble,
  BedSingle,
  ChartColumn,
  ClipboardList,
  Contact,
  FlaskConical,
  Home,
  IndianRupee,
  MessageSquareText,
  Pill,
  Settings,
  UserPlus,
  Users,
} from "lucide-react";

import type { Permission } from "@/types/rbac.generated";

/**
 * The navigation, as data.
 *
 * Every item declares the permission it needs, and the shell filters the list
 * against `/auth/me`. That is what makes CLAUDE.md §7b's role-based
 * dashboards fall out of RBAC rather than being a second thing to maintain:
 * a nurse's sidebar is short because a nurse's permission set is short, not
 * because someone wrote a nurse-specific menu.
 *
 * `labelKey` indexes the `nav` namespace in the message files, so nothing
 * here is a display string.
 *
 * ## Only screens that exist
 *
 * This list holds routes that are built, and nothing else. A sidebar entry
 * that leads to a 404 is worse than a missing one: staff learn within a day
 * which links are real, and after that they stop trusting the navigation to
 * tell them what the software can do. Through most of the build that meant
 * keeping finished APIs — billing, wards, diagnostics, reports — out of here
 * until their screens landed, tracked in `PLANNED` below so an omission was
 * visibly deliberate rather than an oversight.
 *
 * `PLANNED` is empty as of the notification screens: every backend module now
 * has somewhere to go. Keep the mechanism — the next one will need it.
 */
export type NavItem = {
  href: string;
  labelKey: string;
  icon: LucideIcon;
  /** Shown when the user holds ANY of these. */
  permissions: Permission[];
  /** Match child routes too — `/consultation/abc` should light up its parent. */
  prefix?: boolean;
};

export const NAV_ITEMS: NavItem[] = [
  {
    href: "/doctor",
    labelKey: "myPatients",
    icon: ClipboardList,
    // The doctor's own worklist. Gated on completing a consultation rather
    // than on reading a queue, because that is what distinguishes the person
    // who sees patients from everyone else who can watch the queue.
    permissions: ["encounter:complete"],
  },
  {
    href: "/reception",
    labelKey: "reception",
    icon: Home,
    permissions: ["patient:create"],
  },
  {
    href: "/reception/register",
    labelKey: "register",
    icon: UserPlus,
    permissions: ["patient:create"],
  },
  {
    href: "/queue",
    labelKey: "queue",
    icon: Users,
    permissions: ["queue:read"],
  },
  {
    // The admission desk: patients the OPD has sent for admission. Its own
    // permission rather than `admission:create`, which reception and nurses
    // hold for admitting from a visit — they should not all get this list.
    href: "/admissions",
    labelKey: "admissionDesk",
    icon: BedSingle,
    permissions: ["admission:desk"],
  },
  {
    href: "/lab",
    labelKey: "diagnostics",
    icon: FlaskConical,
    permissions: ["report:read"],
    // `/lab/reports/…` should keep the parent lit while a technician is
    // entering results.
    prefix: true,
  },
  {
    href: "/billing",
    labelKey: "billing",
    icon: IndianRupee,
    // Reading charges, not invoices: a cashier's entry point is the board of
    // visits that owe money, which exists before any invoice does.
    permissions: ["charge:read"],
    prefix: true,
  },
  {
    href: "/wards",
    labelKey: "wards",
    icon: BedDouble,
    // `bed:read`, not `admission:read`: the board is the screen, and
    // housekeeping needs it without being able to read admissions.
    permissions: ["bed:read"],
  },
  {
    href: "/rounds",
    labelKey: "rounds",
    icon: Pill,
    permissions: ["medication:read"],
  },
  {
    href: "/patients",
    labelKey: "patients",
    icon: Contact,
    // `prefix` deliberately off: `/patients/[id]` is reached from the search
    // box and from a worklist far more often than from this index, and
    // lighting "Patients" while a doctor is reading a chart mislabels where
    // they are.
    permissions: ["patient:read"],
  },
  {
    href: "/messages",
    labelKey: "messages",
    icon: MessageSquareText,
    // `notification:read`, not `send`: the common case is looking, and the
    // question ("did it go out?") is asked far more often than a message is
    // composed by hand.
    permissions: ["notification:read"],
    prefix: true,
  },
  {
    href: "/reports",
    labelKey: "reports",
    icon: ChartColumn,
    // `report:operational` only. The clinical and revenue sections are chosen
    // server-side from the caller's permissions, so a nurse and a cashier open
    // the same link and get different dashboards — which is what §7b's
    // role-based dashboards mean.
    permissions: ["report:operational"],
  },
  {
    href: "/admin",
    labelKey: "administration",
    icon: Settings,
    // Any one of the areas behind the hub is enough to make it worth showing;
    // the hub itself filters the cards. `user:read` is the broadest proxy for
    // "this person administers something".
    permissions: ["user:read", "service:manage", "catalogue:manage", "ward:manage"],
    prefix: true,
  },
];

/**
 * Modules with a complete API and no screens yet.
 *
 * Not rendered. Listed so the gap is visible in code review rather than
 * discovered by a receptionist clicking a link and getting a 404.
 *
 * Empty. Every backend module has a screen; the next one to be built goes
 * here until it does.
 */
export const PLANNED: readonly { href: string; permission: Permission }[] = [];

/**
 * Which items this user may see.
 *
 * Not an authorisation decision — the server refuses regardless. This only
 * stops a nurse's sidebar being full of links that all lead to a 403, which
 * is how staff learn to distrust software.
 */
export function visibleNavItems(permissions: readonly string[]): NavItem[] {
  const held = new Set(permissions);
  return NAV_ITEMS.filter((item) => item.permissions.some((permission) => held.has(permission)));
}
