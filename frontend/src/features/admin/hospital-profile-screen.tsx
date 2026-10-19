"use client";

import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRef, useState, type FormEvent } from "react";
import { toast } from "sonner";

import { QrSvg } from "@/components/qr-svg";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import { toFieldErrorMap } from "@/lib/api/field-errors";
import type { Hospital } from "@/types/api";

/**
 * Administration → Hospital profile: what every printed bill carries at the
 * top, and the UPI ID every reception QR pays.
 *
 * The fields save through the ordinary `PATCH /hospitals/{id}`. The logo is a
 * separate upload — the file's own bytes, no form encoding — and each upload
 * is a new version on the server, so a bill issued last year still prints the
 * logo it was issued with.
 *
 * The UPI test QR stays here from the old Payments screen: it carries the
 * saved ID and no amount, so the administrator can scan it with their own
 * phone and see the bank print the hospital's name before a patient does.
 */
type Field =
  | "name"
  | "legal_name"
  | "address_line1"
  | "address_line2"
  | "city"
  | "state"
  | "pincode"
  | "phone"
  | "email"
  | "gstin"
  | "hfr_facility_id"
  | "upi_vpa";

const LETTERHEAD: { field: Field; labelKey: string; hintKey?: string; wide?: boolean }[] = [
  { field: "name", labelKey: "hospitalName", wide: true },
  { field: "legal_name", labelKey: "legalName", hintKey: "legalNameHint", wide: true },
  { field: "address_line1", labelKey: "addressLine1", wide: true },
  { field: "address_line2", labelKey: "addressLine2", wide: true },
  { field: "city", labelKey: "city" },
  { field: "state", labelKey: "state" },
  { field: "pincode", labelKey: "pincode" },
  { field: "phone", labelKey: "phone" },
  { field: "email", labelKey: "email" },
  { field: "gstin", labelKey: "gstin" },
  { field: "hfr_facility_id", labelKey: "hfrId" },
];

type Values = Record<Field, string>;

function valuesOf(hospital: Hospital): Values {
  const pick = (value: string | null | undefined) => value ?? "";
  return {
    name: pick(hospital.name),
    legal_name: pick(hospital.legal_name),
    address_line1: pick(hospital.address_line1),
    address_line2: pick(hospital.address_line2),
    city: pick(hospital.city),
    state: pick(hospital.state),
    pincode: pick(hospital.pincode),
    phone: pick(hospital.phone),
    email: pick(hospital.email),
    gstin: pick(hospital.gstin),
    hfr_facility_id: pick(hospital.hfr_facility_id),
    upi_vpa: pick(hospital.upi_vpa),
  };
}

export function logoUrl(hospitalId: string, version: number): string {
  return `/bff/hospitals/${hospitalId}/logo?version=${version}`;
}

export function HospitalProfileScreen({ hospital: initial }: { hospital: Hospital }) {
  const t = useTranslations("admin");
  const [values, setValues] = useState<Values>(() => valuesOf(initial));
  const [saved, setSaved] = useState<Values>(() => valuesOf(initial));
  const [errors, setErrors] = useState<Partial<Record<Field, string>>>({});
  const [logoVersion, setLogoVersion] = useState<number | null>(initial.logo_version ?? null);
  const [logoError, setLogoError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const changed = (Object.keys(values) as Field[]).some((key) => values[key] !== saved[key]);

  const save = useMutation({
    mutationFn: (next: Values) =>
      api.patch<Hospital>(`/hospitals/${initial.id}`, {
        ...Object.fromEntries(
          (Object.keys(next) as Field[]).map((key) => [key, next[key].trim() || null]),
        ),
        // The name is required; an emptied box keeps the old one.
        name: next.name.trim() || saved.name,
      }),
    onSuccess: (hospital) => {
      const current = valuesOf(hospital);
      setValues(current);
      setSaved(current);
      setErrors({});
      toast.success(t("profileSaved"));
    },
    onError: (failure) => {
      const fields = toFieldErrorMap(failure) as Partial<Record<Field, string>>;
      if (Object.keys(fields).length) setErrors(fields);
      else toast.error(isApiError(failure) ? failure.message : String(failure));
    },
  });

  const upload = useMutation({
    mutationFn: (file: File) => api.put<{ version: number }>(`/hospitals/${initial.id}/logo`, file),
    onSuccess: ({ version }) => {
      setLogoVersion(version);
      setLogoError(null);
      toast.success(t("logoUploaded"));
    },
    onError: (failure) => setLogoError(isApiError(failure) ? failure.message : String(failure)),
    onSettled: () => {
      if (fileInput.current) fileInput.current.value = "";
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    save.mutate(values);
  };

  const field = (name: Field, labelKey: string, hintKey?: string, wide?: boolean) => {
    const id = `hospital-${name}`;
    const error = errors[name];
    return (
      <div key={name} className={wide ? "space-y-1.5 sm:col-span-2" : "space-y-1.5"}>
        <Label htmlFor={id}>{t(labelKey)}</Label>
        <Input
          id={id}
          value={values[name]}
          onChange={(event) => {
            setValues((current) => ({ ...current, [name]: event.target.value }));
            setErrors((current) => ({ ...current, [name]: undefined }));
          }}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={Boolean(error)}
          aria-describedby={error || hintKey ? `${id}-help` : undefined}
          className="h-tap text-base"
        />
        {error || hintKey ? (
          <p
            id={`${id}-help`}
            className={error ? "text-destructive text-sm" : "text-muted-foreground text-xs"}
          >
            {error ?? (hintKey ? t(hintKey) : null)}
          </p>
        ) : null}
      </div>
    );
  };

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("hospitalTitle")}</h1>
        <p className="text-muted-foreground mt-1 text-sm">{t("hospitalSubtitle")}</p>
      </div>

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="logo-heading">
        <h2 id="logo-heading" className="font-medium">
          {t("logoSection")}
        </h2>
        <div className="flex flex-wrap items-center gap-4">
          <div className="bg-muted/40 flex h-20 w-48 items-center justify-center rounded-md border">
            {logoVersion ? (
              // A stored file served through the BFF, not a static asset:
              // next/image would try to optimise a tenant-private image.
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={logoUrl(initial.id, logoVersion)}
                alt={t("logoAlt")}
                className="max-h-16 max-w-44 object-contain"
              />
            ) : (
              <span className="text-muted-foreground px-2 text-center text-xs">{t("noLogo")}</span>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="hospital-logo">{logoVersion ? t("replaceLogo") : t("uploadLogo")}</Label>
            <input
              id="hospital-logo"
              ref={fileInput}
              type="file"
              accept="image/png,image/jpeg"
              aria-label={t("uploadLogo")}
              disabled={upload.isPending}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) upload.mutate(file);
              }}
              className="file:bg-primary file:text-primary-foreground block text-sm file:mr-3 file:h-tap file:rounded-md file:border-0 file:px-4 file:font-medium"
            />
            <p
              className={logoError ? "text-destructive text-sm" : "text-muted-foreground text-xs"}
              role={logoError ? "alert" : undefined}
            >
              {logoError ?? t("logoHint")}
            </p>
          </div>
        </div>
      </section>

      <form onSubmit={submit} className="space-y-6">
        <section className="space-y-3 rounded-lg border p-4" aria-labelledby="letterhead-heading">
          <div>
            <h2 id="letterhead-heading" className="font-medium">
              {t("letterheadSection")}
            </h2>
            <p className="text-muted-foreground text-xs">{t("letterheadHint")}</p>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            {LETTERHEAD.map((item) => field(item.field, item.labelKey, item.hintKey, item.wide))}
          </div>
        </section>

        <section className="space-y-3 rounded-lg border p-4" aria-labelledby="payments-heading">
          <h2 id="payments-heading" className="font-medium">
            {t("paymentsSection")}
          </h2>
          {field("upi_vpa", "upiId", "upiIdHint", true)}
          {saved.upi_vpa ? (
            <div className="flex items-center gap-4">
              <QrSvg
                value={`upi://pay?pa=${encodeURIComponent(saved.upi_vpa)}&cu=INR`}
                size="9rem"
                label={t("upiTestQrLabel", { vpa: saved.upi_vpa })}
                className="shrink-0"
              />
              <div className="space-y-1">
                <p className="font-medium">{t("upiTestTitle")}</p>
                <p className="text-muted-foreground text-sm">{t("upiTestHint")}</p>
                <p className="font-medium">{saved.upi_vpa}</p>
              </div>
            </div>
          ) : null}
        </section>

        <Button type="submit" className="h-tap" disabled={save.isPending || !changed}>
          {t("saveProfile")}
        </Button>
      </form>
    </div>
  );
}
