import type { Permission, Role } from "@/types/rbac.generated";
import type { CurrentUser } from "@/types/api";

/**
 * Permission and role helpers for deciding what to *render*.
 *
 * Read that last word carefully. Nothing in this file authorises anything.
 * FastAPI checks the permission and the tenant on every request (CLAUDE.md
 * §8), and it will refuse whatever this file mistakenly shows. What these
 * helpers prevent is the other failure: a screen full of buttons that all
 * return 403, which teaches staff that the software is broken.
 *
 * The `Permission` type comes from `rbac.generated.ts`, so a mistyped
 * permission is a build error rather than a control that silently never
 * appears for anybody.
 */

export function can(user: CurrentUser | null, permission: Permission): boolean {
  return user?.permissions.includes(permission) ?? false;
}

/** True when the user holds every one of these. */
export function canAll(user: CurrentUser | null, ...permissions: Permission[]): boolean {
  return permissions.every((permission) => can(user, permission));
}

/** True when the user holds at least one. */
export function canAny(user: CurrentUser | null, ...permissions: Permission[]): boolean {
  return permissions.some((permission) => can(user, permission));
}

export function hasRole(user: CurrentUser | null, role: Role): boolean {
  return user?.roles.includes(role) ?? false;
}

export function hasAnyRole(user: CurrentUser | null, ...roles: Role[]): boolean {
  return roles.some((role) => hasRole(user, role));
}

/**
 * Where a user lands after signing in.
 *
 * CLAUDE.md §7b asks for role-based dashboards: each role sees only its own
 * screens. Ordered by specificity — a doctor who also covers reception is a
 * doctor first, because that is the screen they open thirty times a day.
 *
 * Driven by permissions rather than role names wherever it can be, since
 * roles are editable data. A hospital that renames RECEPTIONIST to FRONT_DESK
 * should not lose its home screen; one that removes `queue:read` from it
 * genuinely should.
 */
export function homePathFor(user: CurrentUser | null): string {
  if (!user) return "/login";

  if (hasRole(user, "DOCTOR")) return "/doctor";
  if (hasAnyRole(user, "LAB_TECH", "RADIOLOGIST")) return "/lab";
  if (hasRole(user, "BILLING_STAFF")) return "/billing";
  if (hasAnyRole(user, "RECEPTIONIST", "CASHIER")) return "/reception";
  if (hasRole(user, "ADMISSION_DESK")) return "/admissions";
  if (hasRole(user, "NURSE")) return "/queue";
  if (hasAnyRole(user, "HOSPITAL_ADMIN", "PLATFORM_ADMIN")) return "/reception";

  // Fall back to whatever this user can actually reach, so an unusual role
  // combination gets a working screen rather than an empty one.
  if (can(user, "queue:read")) return "/queue";
  if (can(user, "patient:read")) return "/reception";
  if (can(user, "report:read")) return "/lab";
  if (can(user, "charge:read")) return "/billing";
  if (can(user, "admission:desk")) return "/admissions";

  return "/no-home";
}
