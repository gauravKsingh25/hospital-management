"use client";

import { useQuery } from "@tanstack/react-query";
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
import type { Analyte, CatalogueItem, SpecimenType } from "@/types/api";

const CATALOGUE_KEY = ["admin", "catalogue"] as const;

// Every value the backend accepts except NONE, which is what a radiology
// study gets and is set by the discipline rather than chosen.
const SPECIMEN_TYPES: SpecimenType[] = [
  "BLOOD",
  "SERUM",
  "PLASMA",
  "URINE",
  "STOOL",
  "SPUTUM",
  "SWAB",
  "TISSUE",
  "CSF",
  "FLUID",
];

/**
 * The investigations the hospital can order.
 *
 * Two rules the backend enforces and this form respects rather than
 * rediscovering: a laboratory test needs a specimen type, and a radiology
 * study must not have one. The form switches on discipline so the impossible
 * combination cannot be typed, instead of being typed and then rejected.
 *
 * Analytes are the second half and the one people forget. A test with none can
 * be ordered and accessioned, and then the technician opens the report to find
 * nothing to type into — so the list marks tests that have no analytes rather
 * than letting them look finished.
 */
export function CatalogueScreen({ canManage }: { canManage: boolean }) {
  const t = useTranslations("admin");

  const [creating, setCreating] = useState(false);
  const [addingAnalyteTo, setAddingAnalyteTo] = useState<CatalogueItem | null>(null);

  const catalogue = useAdminList<CatalogueItem>(CATALOGUE_KEY, "/diagnostics/catalogue", {
    limit: 200,
  });
  const rows = catalogue.data?.items ?? [];

  // One request per test, but only for lab tests and only on a screen an
  // administrator opens occasionally — the trade a per-row count is worth here
  // and would not be on a worklist.
  const analyteCounts = useQuery({
    queryKey: [...CATALOGUE_KEY, "analytes", rows.map((row) => row.id).join(",")],
    enabled: rows.length > 0,
    queryFn: async ({ signal }) => {
      const entries = await Promise.all(
        rows
          .filter((row) => row.discipline === "LAB")
          .map(async (row) => {
            const analytes = await api.get<Analyte[]>(
              `/diagnostics/catalogue/${row.id}/analytes`,
              { signal },
            );
            return [row.id, analytes.length] as const;
          }),
      );
      return Object.fromEntries(entries) as Record<string, number>;
    },
    staleTime: 30_000,
  });

  return (
    <AdminShell
      title={t("catalogueTitle")}
      description={t("catalogueDescription")}
      action={
        canManage ? (
          <Button className="h-tap" onClick={() => setCreating(true)}>
            <Plus aria-hidden className="size-4" />
            {t("addTest")}
          </Button>
        ) : undefined
      }
    >
      {rows.length === 0 ? (
        <AdminEmpty message={t("catalogueEmpty")} hint={t("catalogueEmptyHint")} />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableCaption className="sr-only">{t("catalogueTitle")}</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>{t("code")}</TableHead>
                <TableHead>{t("name")}</TableHead>
                <TableHead>{t("discipline")}</TableHead>
                <TableHead>{t("specimen")}</TableHead>
                <TableHead>{t("analytes")}</TableHead>
                <TableHead className="text-right">{t("actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((item) => {
                const count = analyteCounts.data?.[item.id];
                const isLab = item.discipline === "LAB";
                return (
                  <TableRow key={item.id} data-testid="catalogue-row">
                    <TableCell className="tabular font-medium">{item.code}</TableCell>
                    <TableCell>{item.name}</TableCell>
                    <TableCell className="text-muted-foreground">{item.discipline}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {item.specimen_type === "NONE" ? "—" : item.specimen_type}
                    </TableCell>
                    <TableCell className="tabular">
                      {!isLab ? (
                        <span className="text-muted-foreground">{t("narrativeReport")}</span>
                      ) : count === 0 ? (
                        <span className="text-caution-foreground font-medium">
                          {t("noAnalytesYet")}
                        </span>
                      ) : (
                        (count ?? "—")
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {canManage && isLab ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-tap"
                          onClick={() => setAddingAnalyteTo(item)}
                        >
                          {t("addAnalyte")}
                        </Button>
                      ) : null}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <TestDialog open={creating} onOpenChange={setCreating} />

      {addingAnalyteTo ? (
        <AnalyteDialog
          item={addingAnalyteTo}
          open
          onOpenChange={(next) => {
            if (!next) setAddingAnalyteTo(null);
          }}
        />
      ) : null}
    </AdminShell>
  );
}

function TestDialog({
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
  const [discipline, setDiscipline] = useState<"LAB" | "RADIOLOGY">("LAB");
  const [specimenType, setSpecimenType] = useState<SpecimenType>("BLOOD");
  const [container, setContainer] = useState("");

  const isLab = discipline === "LAB";

  const create = useAdminMutation(
    CATALOGUE_KEY,
    () =>
      api.post<CatalogueItem>("/diagnostics/catalogue", {
        code,
        name,
        discipline,
        // The backend refuses a radiology study with a specimen and a lab test
        // without one, so the payload follows the discipline rather than the
        // last thing the form had selected.
        specimen_type: isLab ? specimenType : "NONE",
        container: isLab ? container.trim() || null : null,
      }),
    {
      successMessage: t("testCreated"),
      onDone: () => {
        setCode("");
        setName("");
        setContainer("");
        onOpenChange(false);
      },
    },
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("addTest")}</DialogTitle>
          <DialogDescription>{t("addTestHint")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="test-code">{t("code")}</Label>
              <Input
                id="test-code"
                value={code}
                onChange={(event) => setCode(event.target.value.toUpperCase())}
                className="h-tap tabular"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="test-discipline">{t("discipline")}</Label>
              <Select
                value={discipline}
                onValueChange={(value) => setDiscipline((value ?? "LAB") as "LAB" | "RADIOLOGY")}
              >
                <SelectTrigger id="test-discipline" className="h-tap w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="LAB">LAB</SelectItem>
                  <SelectItem value="RADIOLOGY">RADIOLOGY</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="test-name">{t("name")}</Label>
            <Input
              id="test-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="h-tap"
            />
          </div>

          {isLab ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="test-specimen">{t("specimen")}</Label>
                <Select
                  value={specimenType}
                  onValueChange={(value) => setSpecimenType((value ?? "BLOOD") as SpecimenType)}
                >
                  <SelectTrigger id="test-specimen" className="h-tap w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SPECIMEN_TYPES.map((option) => (
                      <SelectItem key={option} value={option}>
                        {option}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="test-container">
                  {t("container")}{" "}
                  <span className="text-muted-foreground">({common("optional")})</span>
                </Label>
                <Input
                  id="test-container"
                  value={container}
                  onChange={(event) => setContainer(event.target.value)}
                  className="h-tap"
                  placeholder={t("containerPlaceholder")}
                />
              </div>
            </div>
          ) : null}
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
 * Adding a measured quantity, with the band it is judged against.
 *
 * The reference range is not optional in practice even though the API allows
 * an analyte without one: a value with no band is a number the report cannot
 * flag, which is most of what a lab report is for. So the low/high pair is
 * required here, and the sex-specific band is offered because haemoglobin —
 * the most-ordered analyte in the country — genuinely differs by it.
 */
function AnalyteDialog({
  item,
  open,
  onOpenChange,
}: {
  item: CatalogueItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");

  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [unit, setUnit] = useState("");
  const [low, setLow] = useState("");
  const [high, setHigh] = useState("");
  const [sex, setSex] = useState("");

  const create = useAdminMutation(
    CATALOGUE_KEY,
    () =>
      api.post<Analyte>(`/diagnostics/catalogue/${item.id}/analytes`, {
        code,
        name,
        unit: unit.trim() || null,
        ranges: [{ low, high, sex: sex || null }],
      }),
    {
      successMessage: t("analyteCreated"),
      onDone: () => {
        setCode("");
        setName("");
        setUnit("");
        setLow("");
        setHigh("");
        onOpenChange(false);
      },
    },
  );

  const rangeValid = low.trim() !== "" && high.trim() !== "" && Number(low) < Number(high);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("addAnalyte")}</DialogTitle>
          <DialogDescription>{item.name}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="analyte-code">{t("code")}</Label>
              <Input
                id="analyte-code"
                value={code}
                onChange={(event) => setCode(event.target.value.toUpperCase())}
                className="h-tap tabular"
              />
            </div>
            <div className="space-y-1.5 sm:col-span-2">
              <Label htmlFor="analyte-name">{t("name")}</Label>
              <Input
                id="analyte-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="h-tap"
              />
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="analyte-unit">{t("unit")}</Label>
              <Input
                id="analyte-unit"
                value={unit}
                onChange={(event) => setUnit(event.target.value)}
                className="h-tap"
                placeholder="g/dL"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="analyte-low">{t("rangeLow")}</Label>
              <Input
                id="analyte-low"
                value={low}
                onChange={(event) => setLow(event.target.value)}
                inputMode="decimal"
                className="h-tap tabular"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="analyte-high">{t("rangeHigh")}</Label>
              <Input
                id="analyte-high"
                value={high}
                onChange={(event) => setHigh(event.target.value)}
                inputMode="decimal"
                className="h-tap tabular"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="analyte-sex">{t("appliesTo")}</Label>
            <Select value={sex} onValueChange={(value) => setSex(value ?? "")}>
              <SelectTrigger id="analyte-sex" className="h-tap w-full">
                <SelectValue placeholder={t("everyone")} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="MALE">MALE</SelectItem>
                <SelectItem value="FEMALE">FEMALE</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-muted-foreground text-xs">{t("appliesToHint")}</p>
          </div>

          {low && high && !rangeValid ? (
            <p role="alert" className="text-caution-foreground text-sm">
              {t("rangeInvalid")}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={!code.trim() || !name.trim() || !rangeValid || create.isPending}
            onClick={() => create.mutate(undefined)}
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
