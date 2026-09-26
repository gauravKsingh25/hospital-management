"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import type { Department, Doctor, User } from "@/types/api";

const DOCTORS_KEY = ["admin", "doctors"] as const;

/**
 * Doctor profiles.
 *
 * A doctor is two records, and the separation is deliberate on the backend's
 * part: `identity` owns who can sign in, `scheduling` owns who holds a clinic.
 * The consequence staff actually hit is that a newly created account with the
 * DOCTOR role **still cannot be picked at the counter** until it has a profile
 * here — which, before this screen, required running a script.
 *
 * So the picker lists accounts that hold the doctor role and do not yet have a
 * profile. Anything else would make the most common failure — "the new doctor
 * isn't in the dropdown" — invisible from the screen meant to fix it.
 */
export function DoctorsScreen({
  staff,
  departments,
  canManage,
}: {
  staff: User[];
  departments: Department[];
  canManage: boolean;
}) {
  const t = useTranslations("admin");
  const [creating, setCreating] = useState(false);

  const doctors = useAdminList<Doctor>(DOCTORS_KEY, "/doctors", { limit: 100 });
  const rows = doctors.data?.items ?? [];

  const withProfile = new Set(rows.map((doctor) => doctor.user_id));
  const candidates = staff.filter(
    (member) => member.roles.includes("DOCTOR") && !withProfile.has(member.id),
  );

  const departmentName = (id: string | null | undefined) =>
    departments.find((department) => department.id === id)?.name ?? "—";

  return (
    <AdminShell
      title={t("doctorsTitle")}
      description={t("doctorsDescription")}
      action={
        canManage ? (
          <Button className="h-tap" onClick={() => setCreating(true)}>
            <Plus aria-hidden className="size-4" />
            {t("addDoctor")}
          </Button>
        ) : undefined
      }
    >
      {canManage && candidates.length > 0 ? (
        <p className="border-caution/50 bg-caution/10 rounded-md border px-3 py-2 text-sm">
          {t("doctorsWithoutProfile", { count: candidates.length })}
        </p>
      ) : null}

      {rows.length === 0 ? (
        <AdminEmpty message={t("doctorsEmpty")} hint={t("doctorsEmptyHint")} />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableCaption className="sr-only">{t("doctorsTitle")}</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>{t("name")}</TableHead>
                <TableHead>{t("specialty")}</TableHead>
                <TableHead>{t("department")}</TableHead>
                <TableHead>{t("registrationNumber")}</TableHead>
                <TableHead className="text-right">{t("acceptingAppointments")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((doctor) => (
                <TableRow key={doctor.id} data-testid="doctor-row">
                  <TableCell className="font-medium">{doctor.display_name}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {doctor.specialty ?? "—"}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {departmentName(doctor.department_id)}
                  </TableCell>
                  <TableCell className="text-muted-foreground tabular">
                    {/* Absent is worth flagging, not blanking: a report cannot
                        be signed without one (see the diagnostics module). */}
                    {doctor.registration_number ?? (
                      <span className="text-caution-foreground">{t("missing")}</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    {doctor.is_accepting_appointments ? t("yes") : t("no")}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <DoctorDialog
        candidates={candidates}
        departments={departments}
        open={creating}
        onOpenChange={setCreating}
      />
    </AdminShell>
  );
}

function DoctorDialog({
  candidates,
  departments,
  open,
  onOpenChange,
}: {
  candidates: User[];
  departments: Department[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");

  const [userId, setUserId] = useState("");
  const [specialty, setSpecialty] = useState("");
  const [registration, setRegistration] = useState("");
  const [departmentId, setDepartmentId] = useState("");

  const create = useAdminMutation(
    DOCTORS_KEY,
    () =>
      api.post<Doctor>("/doctors", {
        user_id: userId,
        specialty: specialty.trim() || null,
        registration_number: registration.trim() || null,
        department_id: departmentId || null,
      }),
    {
      successMessage: t("doctorCreated"),
      onDone: () => {
        setUserId("");
        setSpecialty("");
        setRegistration("");
        setDepartmentId("");
        onOpenChange(false);
      },
    },
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("addDoctor")}</DialogTitle>
          <DialogDescription>{t("addDoctorHint")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="space-y-1.5">
            <Label htmlFor="doctor-user">{t("staffAccount")}</Label>
            <Select value={userId} onValueChange={(value) => setUserId(value ?? "")}>
              <SelectTrigger id="doctor-user" className="h-tap w-full">
                <SelectValue placeholder={t("chooseAccount")} />
              </SelectTrigger>
              <SelectContent>
                {candidates.map((member) => (
                  <SelectItem key={member.id} value={member.id}>
                    {member.full_name} — {member.email}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {candidates.length === 0 ? (
              <p className="text-muted-foreground text-xs">{t("noDoctorCandidates")}</p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="doctor-specialty">{t("specialty")}</Label>
            <Input
              id="doctor-specialty"
              value={specialty}
              onChange={(event) => setSpecialty(event.target.value)}
              className="h-tap"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="doctor-registration">{t("registrationNumber")}</Label>
            <Input
              id="doctor-registration"
              value={registration}
              onChange={(event) => setRegistration(event.target.value)}
              className="h-tap tabular"
            />
            <p className="text-muted-foreground text-xs">{t("registrationNumberHint")}</p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="doctor-department">
              {t("department")}{" "}
              <span className="text-muted-foreground">({common("optional")})</span>
            </Label>
            <Select value={departmentId} onValueChange={(value) => setDepartmentId(value ?? "")}>
              <SelectTrigger id="doctor-department" className="h-tap w-full">
                <SelectValue placeholder={common("none")} />
              </SelectTrigger>
              <SelectContent>
                {departments.map((department) => (
                  <SelectItem key={department.id} value={department.id}>
                    {department.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!userId || create.isPending}
            onClick={() => create.mutate(undefined)}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
