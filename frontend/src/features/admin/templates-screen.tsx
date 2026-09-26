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
import { Textarea } from "@/components/ui/textarea";
import { AdminEmpty, AdminShell } from "@/features/admin/admin-shell";
import { useAdminList, useAdminMutation } from "@/features/admin/use-admin-resource";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type {
  MessageChannel,
  MessageTemplate,
  TemplatePreview as PreviewResult,
} from "@/types/api";

const TEMPLATES_KEY = ["admin", "templates"] as const;

const CHANNELS: MessageChannel[] = ["WHATSAPP", "SMS", "EMAIL"];

/**
 * The hospital's standing copy.
 *
 * Every message the system sends has shipped fallback wording in English. That
 * is the right default — a hospital with no templates still reminds patients
 * of their appointments rather than silently sending nothing — but it is only
 * a default. CLAUDE.md §9 asks for multilingual messaging, and until this
 * screen existed writing a Hindi template meant running a Python script.
 *
 * ## Preview is not a nicety
 *
 * A template is a string with `{placeholders}` in it, and a placeholder the
 * renderer does not recognise stays in the message verbatim. The other way to
 * discover that you typed `{patient}` where the system supplies
 * `{patient_name}` is to read it on a patient's phone. So the preview reports
 * `missing` — the placeholders the copy asks for that nothing will fill — as
 * the primary result, not as a footnote.
 */
export function TemplatesScreen({ canManage }: { canManage: boolean }) {
  const t = useTranslations("admin");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<MessageTemplate | null>(null);

  const templates = useAdminList<MessageTemplate>(TEMPLATES_KEY, "/notifications/templates", {
    limit: 100,
    include_inactive: true,
  });
  const rows = templates.data?.items ?? [];

  return (
    <AdminShell
      title={t("templatesTitle")}
      description={t("templatesDescription")}
      action={
        canManage ? (
          <Button className="h-tap" onClick={() => setCreating(true)}>
            <Plus aria-hidden className="size-4" />
            {t("addTemplate")}
          </Button>
        ) : undefined
      }
    >
      {rows.length === 0 ? (
        // Not an error state. An empty list means every message goes out in the
        // shipped English wording, which works — so the hint says that rather
        // than implying something is broken.
        <AdminEmpty message={t("templatesEmpty")} hint={t("templatesEmptyHint")} />
      ) : (
        <ul className="space-y-2">
          {rows.map((template) => (
            <li key={template.id}>
              <button
                type="button"
                className={cn(
                  "hover:border-primary/50 hover:bg-accent/40 focus-visible:ring-ring w-full",
                  "rounded-lg border p-3 text-left transition-colors focus-visible:ring-2",
                  "focus-visible:outline-none",
                  !template.is_active && "opacity-60",
                )}
                onClick={() => setEditing(template)}
                data-testid="template-row"
                data-code={template.code}
                data-language={template.language}
              >
                <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                  <span className="tabular font-medium">{template.code}</span>
                  <span className="text-muted-foreground text-xs">
                    {template.channel} · {template.language} · {template.category}
                    {template.is_active ? "" : ` · ${t("inactive")}`}
                  </span>
                </div>
                <p className="text-muted-foreground mt-1 line-clamp-2 text-sm">{template.body}</p>
              </button>
            </li>
          ))}
        </ul>
      )}

      {creating ? (
        <TemplateDialog
          onOpenChange={(next) => {
            if (!next) setCreating(false);
          }}
        />
      ) : null}

      {editing ? (
        <TemplateDialog
          template={editing}
          onOpenChange={(next) => {
            if (!next) setEditing(null);
          }}
        />
      ) : null}
    </AdminShell>
  );
}

function TemplateDialog({
  template,
  onOpenChange,
}: {
  template?: MessageTemplate;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("admin");
  const common = useTranslations("common");
  const editing = template !== undefined;

  const [code, setCode] = useState(template?.code ?? "");
  const [channel, setChannel] = useState<MessageChannel>(template?.channel ?? "WHATSAPP");
  const [language, setLanguage] = useState(template?.language ?? "en");
  const [subject, setSubject] = useState(template?.subject ?? "");
  const [body, setBody] = useState(template?.body ?? "");
  const [isActive, setIsActive] = useState(template?.is_active ?? true);

  // What the system can send, and which placeholders each one supplies. Without
  // this an administrator has to read the source to write a template.
  const codes = useQuery({
    queryKey: ["admin", "template-codes"],
    queryFn: ({ signal }) =>
      api.get<Record<string, string[]>>("/notifications/templates/codes", { signal }),
    staleTime: Infinity,
  });

  const save = useAdminMutation(
    TEMPLATES_KEY,
    () =>
      editing
        ? api.patch<MessageTemplate>(`/notifications/templates/${template.id}`, {
            subject: subject.trim() || null,
            body,
            is_active: isActive,
          })
        : api.post<MessageTemplate>("/notifications/templates", {
            code,
            channel,
            language,
            // A subject on anything but email is rejected by the backend rather
            // than ignored — silently dropping a field somebody typed is how a
            // hospital comes to believe its SMS has a headline.
            subject: channel === "EMAIL" ? subject.trim() || null : null,
            body,
          }),
    { successMessage: t("templateSaved"), onDone: () => onOpenChange(false) },
  );

  const expected = codes.data?.[editing ? template.code : code] ?? [];

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{editing ? t("editTemplate") : t("addTemplate")}</DialogTitle>
          <DialogDescription>{t("templatesDescription")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-left">
          {editing ? null : (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="template-code">{t("templateCode")}</Label>
                <Select value={code} onValueChange={(value) => setCode(value ?? "")}>
                  <SelectTrigger id="template-code" className="h-tap w-full">
                    <SelectValue placeholder={t("chooseCode")} />
                  </SelectTrigger>
                  <SelectContent>
                    {Object.keys(codes.data ?? {}).map((option) => (
                      <SelectItem key={option} value={option}>
                        {option}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label htmlFor="template-channel">{t("channel")}</Label>
                  <Select
                    value={channel}
                    onValueChange={(value) => setChannel((value ?? "WHATSAPP") as MessageChannel)}
                  >
                    <SelectTrigger id="template-channel" className="h-tap w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {CHANNELS.map((option) => (
                        <SelectItem key={option} value={option}>
                          {option}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="template-language">{t("language")}</Label>
                  <Input
                    id="template-language"
                    value={language}
                    onChange={(event) => setLanguage(event.target.value)}
                    className="h-tap"
                    placeholder="hi"
                  />
                </div>
              </div>
            </>
          )}

          {channel === "EMAIL" ? (
            <div className="space-y-1.5">
              <Label htmlFor="template-subject">{t("subject")}</Label>
              <Input
                id="template-subject"
                value={subject}
                onChange={(event) => setSubject(event.target.value)}
                className="h-tap"
              />
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="template-body">{t("templateBody")}</Label>
            <Textarea
              id="template-body"
              value={body}
              onChange={(event) => setBody(event.target.value)}
              rows={5}
            />
            {expected.length > 0 ? (
              // Shown in the exact syntax the renderer matches — double
              // braces (`PLACEHOLDER_PATTERN`). A hint that shows a different
              // form is worse than no hint: an administrator copies it, the
              // renderer does not recognise it, and the message goes out with
              // the literal text where a name should be.
              <p className="text-muted-foreground text-xs">
                {t("placeholders", { list: expected.map((name) => `{{${name}}}`).join(" ") })}
              </p>
            ) : null}
          </div>

          {editing ? (
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={isActive}
                onChange={(event) => setIsActive(event.target.checked)}
              />
              {t("templateActive")}
            </label>
          ) : null}

          {editing ? <TemplatePreview templateId={template.id} expected={expected} /> : null}
        </div>

        <DialogFooter>
          <Button variant="outline" className="h-tap" onClick={() => onOpenChange(false)}>
            {common("cancel")}
          </Button>
          <Button
            className="h-tap"
            disabled={body.trim().length === 0 || (!editing && !code) || save.isPending}
            onClick={() => save.mutate(undefined)}
            data-testid="save-template"
          >
            {common("save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Render the saved template against sample values, without sending anything.
 *
 * Deliberately previews what is **saved**, not what is in the textarea: the
 * backend owns the rendering rules, and a client-side approximation of them
 * would be a second implementation that agrees right up until it matters.
 */
function TemplatePreview({ templateId, expected }: { templateId: string; expected: string[] }) {
  const t = useTranslations("admin");
  const [result, setResult] = useState<PreviewResult | null>(null);

  const preview = useAdminMutation(
    ["admin", "template-preview"],
    () =>
      api.post<PreviewResult>(`/notifications/templates/${templateId}/preview`, {
        // Sample values keyed by whatever this code expects, so the preview
        // exercises the real placeholder names rather than a fixed set.
        context: Object.fromEntries(expected.map((name) => [name, `<${name}>`])),
      }),
    { onDone: setResult },
  );

  return (
    <div className="space-y-1.5 rounded-lg border p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium">{t("preview")}</p>
        <Button
          size="sm"
          variant="outline"
          className="h-tap"
          disabled={preview.isPending}
          onClick={() => preview.mutate(undefined)}
          data-testid="preview-template"
        >
          {t("renderPreview")}
        </Button>
      </div>

      {result ? (
        <>
          <p className="bg-muted/40 rounded border p-2 text-sm whitespace-pre-wrap">
            {result.body}
          </p>
          {(result.missing ?? []).length > 0 ? (
            // The primary result, not a footnote: an unrecognised placeholder
            // reaches the patient verbatim.
            <p className="text-caution-foreground text-xs" data-testid="missing-placeholders">
              {t("missingPlaceholders", { list: (result.missing ?? []).join(", ") })}
            </p>
          ) : (
            <p className="text-success text-xs">{t("previewClean")}</p>
          )}
        </>
      ) : (
        <p className="text-muted-foreground text-xs">{t("previewHint")}</p>
      )}
    </div>
  );
}
