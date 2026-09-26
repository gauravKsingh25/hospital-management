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
import { AdminEmpty, AdminShell } from "@/features/admin/admin-shell";
import { useAdminList, useAdminMutation } from "@/features/admin/use-admin-resource";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type { Bed, BedClass, Department, Ward } from "@/types/api";

const WARDS_KEY = ["admin", "wards"] as const;
const BEDS_KEY = ["admin", "beds"] as const;

const BED_CLASSES: BedClass[] = [
  "GENERAL",
  "SEMI_PRIVATE",
  "PRIVATE",
  "DELUXE",
  "ICU",
  "HDU",
  "NICU",
  "EMERGENCY",
  "DAY_CARE",
];

/**
 * Wards and beds.
 *
 * Setup, not the nursing station's bed board — that is a different screen for
 * a different person, and it comes with the rest of the inpatient module.
 * This one exists because a bed cannot be created any other way, and an
 * admission has nowhere to go until one is.
 *
 * Beds are shown as a grid rather than a table on purpose: a ward is a
 * physical space and the thing an administrator is checking is "did I create
 * all twenty?", which a grid answers at a glance and a paginated table does
 * not.
 */
export function WardsScreen({
  departments,
  canManage,
}: {
  departments: Department[];
  canManage: boolean;
}) {
  const t = useTranslations("admin");

  const [creatingWard, setCreatingWard] = useState(false);
  const [addingBedsTo, setAddingBedsTo] = useState<Ward | null>(null);
  const [selectedWard, setSelectedWard] = useState<string>("");

  const wards = useAdminList<Ward>(WARDS_KEY, "/ipd/wards", { limit: 100 });
  const wardList = wards.data?.items ?? [];
  const activeWard = selectedWard || wardList[0]?.id || "";

  const beds = useAdminList<Bed>([...BEDS_KEY, activeWard], "/ipd/beds", {
    ward_id: activeWard || undefined,
    limit: 200,
  });
  const bedList = activeWard ? (beds.data?.items ?? []) : [];
  const ward = wardList.find((row) => row.id === activeWard);

  return (
    <AdminShell
      title={t("wardsTitle")}
      description={t("wardsDescription")}
      action={
        canManage ? (
          <Button className="h-tap" onClick={() => setCreatingWard(true)}>
            <Plus aria-hidden className="size-4" />
            {t("addWard")}
          </Button>
        ) : undefined
      }
    >
      {wardList.length === 0 ? (
        <AdminEmpty message={t("wardsEmpty")} hint={t("wardsEmptyHint")} />
      ) : (
        <>
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="ward-picker">{t("ward")}</Label>
              <Select value={activeWard} onValueChange={(value) => setSelectedWard(value ?? "")}>
                <SelectTrigger id="ward-picker" className="h-tap w-72">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {wardList.map((row) => (
                    <SelectItem key={row.id} value={row.id}>
                      {row.code} — {row.name} ({row.bed_class})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {canManage && ward ? (
              <Button
                variant="outline"
                className="h-tap"
                onClick={() => setAddingBedsTo(ward)}
              >
                {t("addBeds")}
              </Button>
            ) : null}
          </div>

          {bedList.length === 0 ? (
            <AdminEmpty message={t("bedsEmpty")} hint={t("bedsEmptyHint")} />
          ) : (
            <section aria-labelledby="beds-heading" className="space-y-3">
              <h2 id="beds-heading" className="text-sm font-semibold">
                {t("bedsInWard", { count: bedList.length })}
              </h2>
              <ul className="grid grid-cols-3 gap-2 sm:grid-cols-6 lg:grid-cols-10">
                {bedList.map((bed) => (
                  <li
                    key={bed.id}
                    data-testid="bed-tile"
                    className={cn(
                      "rounded-md border px-2 py-3 text-center",
                      bed.status === "AVAILABLE" && "border-success/40 bg-success/10",
                      bed.status === "OCCUPIED" && "border-info/40 bg-info/10",
                      bed.status === "CLEANING" && "border-caution/40 bg-caution/10",
                      bed.status === "OUT_OF_SERVICE" && "bg-muted opacity-70",
                    )}
                  >
                    <div className="tabular text-sm font-semibold">{bed.code}</div>
                    <div className="text-muted-foreground mt-0.5 text-[10px] uppercase">
                      {bed.status}
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}

      <WardDialog
        departments={departments}
        open={creatingWard}
        onOpenChange={setCreatingWard}
        onCreated={setSelectedWard}
      />

      {addingBedsTo ? (
        <BedsDialog
          ward={addingBedsTo}
          open
          onOpenChange={(next) => {
            if (!next) setAddingBedsTo(null);
          }}
        />
      ) : null}
    </AdminShell>
  );
}

function WardDialog({
  departments,
  open,
  onOpenChange,
  onCreated,
}: {
  departments: Department[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (wardId: string) => void;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");

  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [bedClass, setBedClass] = useState<BedClass>("GENERAL");
  const [floor, setFloor] = useState("");
  const [departmentId, setDepartmentId] = useState("");
  const [genderPolicy, setGenderPolicy] = useState("");

  const create = useAdminMutation(
    WARDS_KEY,
    () =>
      api.post<Ward>("/ipd/wards", {
        code,
        name,
        bed_class: bedClass,
        floor: floor.trim() || null,
        department_id: departmentId || null,
        gender_policy: genderPolicy || null,
      }),
    {
      successMessage: t("wardCreated"),
      onDone: (created) => {
        setCode("");
        setName("");
        setFloor("");
        onOpenChange(false);
        // Select what was just created. Leaving the picker on whichever ward
        // sorts first means the next thing an administrator does — adding
        // beds — silently lands in the wrong ward.
        onCreated(created.id);
      },
    },
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("addWard")}</DialogTitle>
          <DialogDescription>{t("addWardHint")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="ward-code">{t("code")}</Label>
              <Input
                id="ward-code"
                value={code}
                onChange={(event) => setCode(event.target.value.toUpperCase())}
                className="h-tap tabular"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="ward-class">{t("bedClass")}</Label>
              <Select
                value={bedClass}
                onValueChange={(value) => setBedClass((value ?? "GENERAL") as BedClass)}
              >
                <SelectTrigger id="ward-class" className="h-tap w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {BED_CLASSES.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ward-name">{t("name")}</Label>
            <Input
              id="ward-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="h-tap"
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="ward-floor">
                {t("floor")} <span className="text-muted-foreground">({common("optional")})</span>
              </Label>
              <Input
                id="ward-floor"
                value={floor}
                onChange={(event) => setFloor(event.target.value)}
                className="h-tap"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="ward-gender">{t("genderPolicy")}</Label>
              <Select
                value={genderPolicy}
                onValueChange={(value) => setGenderPolicy(value ?? "")}
              >
                <SelectTrigger id="ward-gender" className="h-tap w-full">
                  <SelectValue placeholder={t("mixed")} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="MALE">MALE</SelectItem>
                  <SelectItem value="FEMALE">FEMALE</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ward-department">
              {t("department")}{" "}
              <span className="text-muted-foreground">({common("optional")})</span>
            </Label>
            <Select value={departmentId} onValueChange={(value) => setDepartmentId(value ?? "")}>
              <SelectTrigger id="ward-department" className="h-tap w-full">
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
            disabled={!code.trim() || !name.trim() || create.isPending}
            onClick={() => create.mutate(undefined)}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Creating beds in a run.
 *
 * A ward has twenty beds and they are numbered, so this creates a range in one
 * action rather than making an administrator open a dialog twenty times. The
 * requests go out sequentially and stop at the first failure, so a clash on
 * bed 7 leaves 1–6 created and says so, rather than half-succeeding silently.
 *
 * **The prefix defaults to the ward's code, and that is not cosmetic.** A bed
 * code is unique per *hospital*, not per ward (`uq_beds_code_live`), which the
 * model hints at with its `'ICU-04'` example. Bare numbers would let the first
 * ward take 1–20 and leave every other ward unable to have a bed 1 — a
 * collision that only appears on the second ward somebody sets up, which is
 * exactly when nobody is looking for it.
 */
function BedsDialog({
  ward,
  open,
  onOpenChange,
}: {
  ward: Ward;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");

  const [prefix, setPrefix] = useState(`${ward.code}-`);
  const [from, setFrom] = useState("1");
  const [to, setTo] = useState("10");

  const first = Number(from);
  const last = Number(to);
  const count = Number.isFinite(first) && Number.isFinite(last) ? last - first + 1 : 0;
  const valid = count > 0 && count <= 100;

  const create = useAdminMutation(
    [...BEDS_KEY, ward.id],
    async () => {
      let created = 0;
      for (let index = first; index <= last; index += 1) {
        await api.post<Bed>("/ipd/beds", {
          ward_id: ward.id,
          code: `${prefix.trim()}${index}`,
        });
        created += 1;
      }
      return created;
    },
    {
      onDone: (created) => {
        setPrefix(`${ward.code}-`);
        onOpenChange(false);
        return created;
      },
      successMessage: t("bedsCreated"),
    },
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("addBeds")}</DialogTitle>
          <DialogDescription>
            {ward.code} — {ward.name}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="bed-prefix">{t("bedPrefix")}</Label>
              <Input
                id="bed-prefix"
                value={prefix}
                onChange={(event) => setPrefix(event.target.value.toUpperCase())}
                className="h-tap tabular"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="bed-from">{t("from")}</Label>
              <Input
                id="bed-from"
                value={from}
                onChange={(event) => setFrom(event.target.value)}
                inputMode="numeric"
                className="h-tap tabular"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="bed-to">{t("to")}</Label>
              <Input
                id="bed-to"
                value={to}
                onChange={(event) => setTo(event.target.value)}
                inputMode="numeric"
                className="h-tap tabular"
              />
            </div>
          </div>

          <p className="text-muted-foreground text-sm">
            {valid
              ? t("bedsPreview", {
                  count,
                  first: `${prefix.trim()}${first}`,
                  last: `${prefix.trim()}${last}`,
                })
              : t("bedsRangeInvalid")}
          </p>
          <p className="text-muted-foreground text-xs">{t("bedPrefixHint")}</p>
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!valid || create.isPending}
            onClick={() => create.mutate(undefined)}
          >
            {create.isPending ? common("loading") : common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
