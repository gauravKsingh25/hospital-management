"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { UserPlus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { AdminEmpty, AdminShell } from "@/features/admin/admin-shell";
import { RolesDialog } from "@/features/admin/roles-dialog";
import { StaffDialog } from "@/features/admin/staff-dialog";
import { useAdminList, useAdminMutation } from "@/features/admin/use-admin-resource";
import { api } from "@/lib/api/client";
import { useDebounced } from "@/lib/hooks/use-debounced";
import { cn } from "@/lib/utils";
import type { Role, User } from "@/types/api";

const STAFF_KEY = ["admin", "users"] as const;

/**
 * Staff accounts and what each of them may do.
 *
 * Two things here are worth stating.
 *
 * **Roles are shown on every row.** They arrive on `UserRead` because the
 * backend now bulk-loads them; before that this screen could not have answered
 * the question it exists to answer — who is a doctor here — without a request
 * per person.
 *
 * **Deactivate, never delete.** An account that saw a patient is part of the
 * audit trail (CLAUDE.md §12), so it is retired rather than removed. The list
 * hides inactive accounts by default and can show them, because "why can this
 * person not log in" is a question somebody asks every few weeks.
 */
export function StaffScreen({
  roles,
  canCreate,
  canAssignRoles,
  canDeactivate,
}: {
  roles: Role[];
  canCreate: boolean;
  canAssignRoles: boolean;
  canDeactivate: boolean;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");

  const [search, setSearch] = useState("");
  const [includeInactive, setIncludeInactive] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editingRolesFor, setEditingRolesFor] = useState<User | null>(null);
  const debounced = useDebounced(search, 250);

  const staff = useAdminList<User>(STAFF_KEY, "/users", {
    search: debounced.trim() || undefined,
    include_inactive: includeInactive,
    limit: 100,
  });

  const deactivate = useAdminMutation(
    STAFF_KEY,
    (userId: string) => api.post<User>(`/users/${userId}/deactivate`),
    { successMessage: t("staffDeactivated") },
  );

  const rows = staff.data?.items ?? [];

  return (
    <AdminShell
      title={t("staffTitle")}
      description={t("staffDescription")}
      action={
        canCreate ? (
          <Button className="h-tap" onClick={() => setCreating(true)}>
            <UserPlus aria-hidden className="size-4" />
            {t("addStaff")}
          </Button>
        ) : undefined
      }
    >
      <div className="flex flex-wrap items-center gap-3">
        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder={t("staffSearchPlaceholder")}
          aria-label={common("search")}
          className="h-tap sm:w-80"
        />
        <label className="flex min-h-tap cursor-pointer items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={includeInactive}
            onChange={(event) => setIncludeInactive(event.target.checked)}
            className="accent-primary size-4"
          />
          {t("showInactive")}
        </label>
      </div>

      {rows.length === 0 ? (
        <AdminEmpty message={t("staffEmpty")} hint={t("staffEmptyHint")} />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableCaption className="sr-only">{t("staffTitle")}</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>{t("name")}</TableHead>
                <TableHead>{t("email")}</TableHead>
                <TableHead>{t("roles")}</TableHead>
                <TableHead className="text-right">{t("actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.id} data-testid="staff-row" className={cn(!row.is_active && "opacity-60")}>
                  <TableCell className="font-medium">
                    {row.full_name}
                    {!row.is_active ? (
                      <span className="text-muted-foreground ml-2 text-xs">{t("inactive")}</span>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{row.email}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {row.roles.length === 0 ? (
                        <span className="text-caution-foreground text-xs font-medium">
                          {t("noRoles")}
                        </span>
                      ) : (
                        row.roles.map((role) => (
                          <span
                            key={role}
                            className="bg-muted rounded px-1.5 py-0.5 text-[11px] font-medium"
                          >
                            {role}
                          </span>
                        ))
                      )}
                    </div>
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-2">
                      {canAssignRoles ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-tap"
                          onClick={() => setEditingRolesFor(row)}
                        >
                          {t("changeRoles")}
                        </Button>
                      ) : null}
                      {canDeactivate && row.is_active ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-tap"
                          disabled={deactivate.isPending}
                          onClick={() => deactivate.mutate(row.id)}
                        >
                          {t("deactivate")}
                        </Button>
                      ) : null}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <StaffDialog
        roles={roles}
        open={creating}
        onOpenChange={setCreating}
        queryKey={STAFF_KEY}
      />

      {editingRolesFor ? (
        <RolesDialog
          user={editingRolesFor}
          roles={roles}
          open
          onOpenChange={(next) => {
            if (!next) setEditingRolesFor(null);
          }}
          queryKey={STAFF_KEY}
        />
      ) : null}
    </AdminShell>
  );
}
