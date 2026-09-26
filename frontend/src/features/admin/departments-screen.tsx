"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { Plus } from "lucide-react";

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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import { useAdminList, useAdminMutation } from "@/features/admin/use-admin-resource";
import { api } from "@/lib/api/client";
import type { Department, DepartmentType } from "@/types/api";

const DEPARTMENTS_KEY = ["admin", "departments"] as const;

const TYPES: DepartmentType[] = ["OPD", "IPD", "EMERGENCY", "DIAGNOSTIC", "PHARMACY", "SUPPORT"];

/**
 * Departments.
 *
 * The quietest of these screens and the one most things hang off: doctors,
 * wards and service items all optionally belong to a department, and none of
 * those pickers has anything to offer until this list exists.
 *
 * The code is uppercased and underscored by the backend on the way in, so what
 * an administrator types is not necessarily what is stored — the field shows
 * the normalised form back to them rather than pretending otherwise.
 */
export function DepartmentsScreen({ canManage }: { canManage: boolean }) {
  const t = useTranslations("admin");
  const [creating, setCreating] = useState(false);

  const departments = useAdminList<Department>(DEPARTMENTS_KEY, "/departments", { limit: 100 });
  const rows = departments.data?.items ?? [];

  return (
    <AdminShell
      title={t("departmentsTitle")}
      description={t("departmentsDescription")}
      action={
        canManage ? (
          <Button className="h-tap" onClick={() => setCreating(true)}>
            <Plus aria-hidden className="size-4" />
            {t("addDepartment")}
          </Button>
        ) : undefined
      }
    >
      {rows.length === 0 ? (
        <AdminEmpty message={t("departmentsEmpty")} hint={t("departmentsEmptyHint")} />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableCaption className="sr-only">{t("departmentsTitle")}</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>{t("code")}</TableHead>
                <TableHead>{t("name")}</TableHead>
                <TableHead>{t("type")}</TableHead>
                <TableHead>{t("location")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((department) => (
                <TableRow key={department.id} data-testid="department-row">
                  <TableCell className="tabular font-medium">{department.code}</TableCell>
                  <TableCell>{department.name}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {department.department_type}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {department.location ?? "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <DepartmentDialog open={creating} onOpenChange={setCreating} />
    </AdminShell>
  );
}

function DepartmentDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");

  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [type, setType] = useState<DepartmentType>("OPD");
  const [location, setLocation] = useState("");

  const create = useAdminMutation(
    DEPARTMENTS_KEY,
    () =>
      api.post<Department>("/departments", {
        code,
        name,
        department_type: type,
        location: location.trim() || null,
      }),
    {
      successMessage: t("departmentCreated"),
      onDone: () => {
        setCode("");
        setName("");
        setLocation("");
        onOpenChange(false);
      },
    },
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("addDepartment")}</DialogTitle>
          <DialogDescription>{t("addDepartmentHint")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="space-y-1.5">
            <Label htmlFor="department-code">{t("code")}</Label>
            <Input
              id="department-code"
              value={code}
              // Uppercased as they type, matching what the backend stores. A
              // field that silently rewrites its value on save is a field
              // people distrust.
              onChange={(event) => setCode(event.target.value.toUpperCase().replace(/\s+/g, "_"))}
              className="h-tap tabular"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="department-name">{t("name")}</Label>
            <Input
              id="department-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="h-tap"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="department-type">{t("type")}</Label>
            <Select
              value={type}
              onValueChange={(value) => setType((value ?? "OPD") as DepartmentType)}
            >
              <SelectTrigger id="department-type" className="h-tap w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TYPES.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="department-location">
              {t("location")}{" "}
              <span className="text-muted-foreground">({common("optional")})</span>
            </Label>
            <Input
              id="department-location"
              value={location}
              onChange={(event) => setLocation(event.target.value)}
              className="h-tap"
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={code.trim().length < 2 || name.trim().length < 2 || create.isPending}
            onClick={() => create.mutate(undefined)}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
