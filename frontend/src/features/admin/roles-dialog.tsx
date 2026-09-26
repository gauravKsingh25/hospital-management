"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAdminMutation } from "@/features/admin/use-admin-resource";
import { api } from "@/lib/api/client";
import type { Role, User } from "@/types/api";

/**
 * Changing what one member of staff may do.
 *
 * The endpoint **replaces** the role set rather than adding to it, and this
 * dialog is shaped to match: every role is a checkbox pre-ticked from the
 * user's current set, so what you see is what they will have. A form that
 * offered "add a role" against a replace endpoint would silently remove the
 * others.
 *
 * Saving with nothing ticked is refused here rather than by the server, which
 * accepts it: an account with no roles can still sign in and can then do
 * nothing at all, which its owner experiences as the software being broken
 * rather than as a permissions decision. Removing someone's access is
 * deactivation, and that is a different button.
 */
export function RolesDialog({
  user,
  roles,
  open,
  onOpenChange,
  queryKey,
}: {
  user: User;
  roles: Role[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  queryKey: readonly unknown[];
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");
  const [selected, setSelected] = useState<string[]>(user.roles);

  const save = useAdminMutation(
    queryKey,
    (codes: string[]) => api.post<User>(`/users/${user.id}/roles`, { role_codes: codes }),
    { successMessage: t("rolesUpdated"), onDone: () => onOpenChange(false) },
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("changeRoles")}</DialogTitle>
          <DialogDescription>
            {user.full_name} · {user.email}
          </DialogDescription>
        </DialogHeader>

        <fieldset className="space-y-1 text-left">
          <legend className="sr-only">{t("roles")}</legend>
          {roles.map((role) => (
            <label
              key={role.code}
              className="flex min-h-tap cursor-pointer items-start gap-2.5 text-sm"
            >
              <input
                type="checkbox"
                checked={selected.includes(role.code)}
                onChange={(event) =>
                  setSelected((current) =>
                    event.target.checked
                      ? [...current, role.code]
                      : current.filter((code) => code !== role.code),
                  )
                }
                className="accent-primary mt-3 size-4 shrink-0"
              />
              <span className="py-2.5">
                {role.name}
                {role.description ? (
                  <span className="text-muted-foreground block text-xs">{role.description}</span>
                ) : null}
              </span>
            </label>
          ))}
        </fieldset>

        {selected.length === 0 ? (
          <p role="alert" className="text-caution-foreground text-sm">
            {t("atLeastOneRole")}
          </p>
        ) : null}

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={selected.length === 0 || save.isPending}
            onClick={() => save.mutate(selected)}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
