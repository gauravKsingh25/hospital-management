"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useTranslations } from "next-intl";
import { useForm, useWatch } from "react-hook-form";
import { z } from "zod";

import { FieldError } from "@/components/field-error";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAdminMutation } from "@/features/admin/use-admin-resource";
import { api } from "@/lib/api/client";
import { applyFieldErrors } from "@/lib/api/field-errors";
import type { Role, User } from "@/types/api";

/**
 * Creating a staff account.
 *
 * The password is set here and the account is created with
 * `must_change_password` left at the backend's default — so whatever an
 * administrator types across a desk is a one-time credential the member of
 * staff replaces at first login, not a shared secret that lives on a sticky
 * note. That is the backend's behaviour; this form's job is to not undermine
 * it by suggesting the password is permanent.
 *
 * At least one role is required. An account with no roles can sign in and do
 * nothing, which looks to its owner exactly like the software being broken.
 */
const schema = z.object({
  full_name: z.string().trim().min(1, "required").max(200),
  email: z.string().trim().min(1, "required").email("emailInvalid"),
  phone: z.string().trim().max(20).optional(),
  // Mirrors the backend's `min_length=1` on `UserCreate.password`, but stricter
  // on purpose: the backend accepts a short password because it is also the
  // path a password *reset* takes, while a human typing a new one at a desk
  // should not be choosing "abc".
  password: z.string().min(12, "passwordTooShort").max(256),
  role_codes: z.array(z.string()).min(1, "roleRequired"),
});

type Form = z.infer<typeof schema>;

const FIELD_NAMES = ["full_name", "email", "phone", "password", "role_codes"] as const;

export function StaffDialog({
  roles,
  open,
  onOpenChange,
  queryKey,
}: {
  roles: Role[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  queryKey: readonly unknown[];
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");

  const form = useForm<Form>({
    resolver: zodResolver(schema),
    mode: "onBlur",
    defaultValues: { full_name: "", email: "", phone: "", password: "", role_codes: [] },
  });

  const create = useAdminMutation(
    queryKey,
    (values: Form) =>
      api.post<User>("/users", {
        full_name: values.full_name,
        email: values.email,
        phone: values.phone?.trim() || null,
        password: values.password,
        role_codes: values.role_codes,
      }),
    {
      successMessage: t("staffCreated"),
      onDone: () => {
        form.reset();
        onOpenChange(false);
      },
    },
  );

  // `useWatch`, not `form.watch()` — the latter returns a fresh function each
  // render, which makes the React Compiler skip memoising this component.
  const selected = useWatch({ control: form.control, name: "role_codes" }) ?? [];

  const submit = form.handleSubmit((values) =>
    create.mutateAsync(values).catch((error: unknown) => {
      // A duplicate email is the common failure and it names its field, so it
      // belongs under the input rather than in a toast that scrolls away.
      applyFieldErrors(error, form.setError, FIELD_NAMES);
    }),
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("addStaff")}</DialogTitle>
          <DialogDescription>{t("addStaffHint")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} noValidate className="space-y-3 text-left">
          <div className="space-y-1.5">
            <Label htmlFor="staff-name">{t("name")}</Label>
            <Input id="staff-name" className="h-tap" {...form.register("full_name")} />
            <FieldError
              message={form.formState.errors.full_name?.message}
              translate={t as (key: string) => string}
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="staff-email">{t("email")}</Label>
              <Input
                id="staff-email"
                type="email"
                className="h-tap"
                {...form.register("email")}
              />
              <FieldError
                message={form.formState.errors.email?.message}
                translate={t as (key: string) => string}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="staff-phone">
                {t("phone")} <span className="text-muted-foreground">({common("optional")})</span>
              </Label>
              <Input id="staff-phone" inputMode="tel" className="h-tap" {...form.register("phone")} />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="staff-password">{t("temporaryPassword")}</Label>
            <Input
              id="staff-password"
              type="password"
              autoComplete="new-password"
              className="h-tap"
              {...form.register("password")}
            />
            <p className="text-muted-foreground text-xs">{t("temporaryPasswordHint")}</p>
            <FieldError
              message={form.formState.errors.password?.message}
              translate={t as (key: string) => string}
            />
          </div>

          <fieldset className="space-y-1.5">
            <legend className="text-sm font-medium">{t("roles")}</legend>
            <div className="grid grid-cols-2 gap-1.5">
              {roles.map((role) => (
                <label
                  key={role.code}
                  className="flex min-h-tap cursor-pointer items-center gap-2 text-sm"
                >
                  <input
                    type="checkbox"
                    value={role.code}
                    checked={selected.includes(role.code)}
                    onChange={(event) => {
                      const next = event.target.checked
                        ? [...selected, role.code]
                        : selected.filter((code) => code !== role.code);
                      form.setValue("role_codes", next, { shouldValidate: true });
                    }}
                    className="accent-primary size-4 shrink-0"
                  />
                  <span className="truncate">{role.name}</span>
                </label>
              ))}
            </div>
            <FieldError
              message={form.formState.errors.role_codes?.message}
              translate={t as (key: string) => string}
            />
          </fieldset>

          <DialogFooter className="mt-2">
            <Button
              type="button"
              variant="outline"
              className="h-tap"
              onClick={() => onOpenChange(false)}
            >
              {common("cancel")}
            </Button>
            <Button type="submit" className="h-tap" disabled={create.isPending}>
              {t("createAccount")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
