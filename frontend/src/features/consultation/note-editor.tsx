"use client";

import { useMutation } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { Check, FileText } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api } from "@/lib/api/client";
import { isApiError } from "@/lib/api/error";
import type { ClinicalNote, NoteTemplate } from "@/types/api";

/**
 * The clinical note, plus the templates that make it fast.
 *
 * Templates are the single biggest lever on CLAUDE.md §7's "less doctor,
 * more staff" goal. A viral fever consultation is the same note four hundred
 * times a year; typing it out each time is the cost the system exists to
 * remove. Inserting one drops a skeleton in that the doctor edits, so what is
 * recorded is still theirs.
 *
 * Written and signed in a single call — the backend's `NoteCreate` takes
 * `sign: true` — because a two-step write-then-sign is one more click on the
 * surface §7 wants smallest, and an unsigned note left behind at the end of a
 * clinic is a records problem for somebody else.
 *
 * Ctrl+Enter saves (§7b). A doctor typing a note has both hands on the
 * keyboard, and reaching for the mouse to finish is the slowest part of a
 * fast consultation.
 */
export function NoteEditor({
  encounterId,
  notes,
  templates,
  disabled,
  onSaved,
}: {
  encounterId: string;
  notes: ClinicalNote[];
  templates: NoteTemplate[];
  disabled: boolean;
  onSaved: () => void;
}) {
  const t = useTranslations("consultation");
  const format = useFormatter();
  const [content, setContent] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);

  const save = useMutation({
    mutationFn: (body: string) =>
      api.post<ClinicalNote>(`/encounters/${encounterId}/notes`, {
        content: body,
        note_type: "ASSESSMENT",
        sign: true,
      }),
    onSuccess: () => {
      setContent("");
      onSaved();
      toast.success(t("signed"));
    },
    onError: (error) => toast.error(isApiError(error) ? error.message : String(error)),
  });

  const insertTemplate = (template: NoteTemplate) => {
    // Appended rather than replacing: a doctor who has already typed a line
    // of history should not lose it by reaching for a template afterwards.
    setContent((previous) => (previous ? `${previous.trimEnd()}\n\n${template.body}` : template.body));
    textarea.current?.focus();
  };

  const submit = () => {
    const body = content.trim();
    if (!body) {
      toast.error(t("noteRequired"));
      return;
    }
    save.mutate(body);
  };

  return (
    <section className="space-y-3 rounded-lg border p-4" aria-labelledby="note-heading">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="note-heading" className="text-sm font-semibold">
          {t("note")}
        </h2>

        {templates.length > 0 ? (
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button variant="outline" size="sm" className="h-tap" disabled={disabled}>
                  <FileText aria-hidden className="size-4" />
                  {t("insertTemplate")}
                </Button>
              }
            />
            <DropdownMenuContent align="end" className="max-h-80 w-72 overflow-y-auto">
              {templates.map((template) => (
                <DropdownMenuItem key={template.id} onClick={() => insertTemplate(template)}>
                  <span className="truncate">{template.title}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
      </div>

      <Textarea
        ref={textarea}
        value={content}
        onChange={(event) => setContent(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            submit();
          }
        }}
        disabled={disabled}
        rows={10}
        placeholder={t("notePlaceholder")}
        className="resize-y text-base"
        aria-label={t("note")}
      />

      <div className="flex items-center justify-between gap-3">
        <kbd className="text-muted-foreground text-xs">Ctrl + Enter</kbd>
        <Button
          onClick={submit}
          disabled={disabled || save.isPending || content.trim().length === 0}
          className="h-tap"
        >
          <Check aria-hidden className="size-4" />
          {t("signed")}
        </Button>
      </div>

      {notes.length > 0 ? (
        <ol className="divide-border divide-y border-t pt-3">
          {notes.map((note) => (
            <li key={note.id} className="py-3">
              <div className="text-muted-foreground mb-1 flex flex-wrap items-center gap-2 text-xs">
                <span>{t("recordedBy", { name: note.author_name })}</span>
                <span>·</span>
                <span>{format.dateTime(new Date(note.created_at), "time")}</span>
                <span
                  className={
                    note.is_signed
                      ? "text-success font-medium"
                      : "text-caution-foreground font-medium"
                  }
                >
                  {note.is_signed ? t("signed") : t("draft")}
                </span>
              </div>
              {/* `whitespace-pre-wrap`: a clinical note's line breaks are part
                  of its meaning, and collapsing them turns a structured
                  examination into a paragraph. */}
              <p className="text-sm whitespace-pre-wrap">{note.content}</p>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
