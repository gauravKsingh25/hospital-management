"use client";

import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { restrictToParentElement, restrictToVerticalAxis } from "@dnd-kit/modifiers";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";
import { ArrowRightLeft, BedDouble, Check, GripVertical, Undo2, Users } from "lucide-react";

import { QueueStatusChip } from "@/components/status-chip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { SendToAdmissionDialog } from "@/features/admissions/send-to-admission-dialog";
import { useTodaysAdmissionRequests } from "@/features/admissions/use-admission-requests";
import { ReassignDialog } from "@/features/queue/reassign-dialog";
import { useMarkSeen, useQueueBoard, useReorderEntry } from "@/features/queue/use-queue-board";
import { useElapsed } from "@/lib/hooks/use-elapsed";
import { cn } from "@/lib/utils";
import type { AdmissionRequest, DoctorQueue, QueueBoardEntry } from "@/types/api";

/**
 * Reception's queue: one column per doctor, busiest first.
 *
 * The nurse's screen (`LiveQueue`) is one flat list because a nurse calling
 * for vitals works across every clinic. Reception's question is different —
 * not "who is next" but "which doctor is drowning, and where can I send the
 * person in front of me". That is a comparison between doctors, so the
 * doctors are the columns and the counts are in the headers, where a glance
 * answers it before the patient has finished asking.
 *
 * Every active doctor is shown, including the ones with nobody waiting. The
 * empty column is not clutter; it is the answer.
 *
 * Three things reception can do from here, all without leaving the screen:
 *
 * - **Seen** — record that the doctor has seen the patient. The name is
 *   struck through at once, with a few seconds to undo, then the row drops
 *   into the column's "Seen today" list, still struck through.
 * - **Move** a patient to another doctor's line (the button on the row).
 * - **Re-rank** the line by dragging the handle — the child with the fever
 *   above the routine follow-up. Rank is stored on the server and drives the
 *   doctor's own list and "how many ahead", so the drag is the order, not a
 *   picture of it. Pointer, touch and keyboard (space, arrows, space) all
 *   work; the handle is the only drag surface so the row's text and buttons
 *   still behave like text and buttons.
 *
 * Dragging is confined to one column. Dropping a patient on another doctor
 * is a reassignment with consequences (a new token, a different chart), and
 * that deserves the dialog's confirmation rather than a slip of the wrist.
 */
export function DoctorQueues({
  canManage = false,
  canRequestAdmission = false,
}: {
  canManage?: boolean;
  /** Offer "Send to admission" on seen patients (`admission:request`). */
  canRequestAdmission?: boolean;
}) {
  const t = useTranslations("queue");
  const [dragging, setDragging] = useState(false);
  const board = useQueueBoard({ paused: dragging });
  // Board-level so a pending "Seen" survives the column re-rendering around
  // it on the next poll.
  const seen = useMarkSeen();
  // What became of the patients sent for admission today, keyed by the visit
  // they were seen on. Newest first, so a patient turned away and sent again
  // shows the second request, not the first.
  const requests = useTodaysAdmissionRequests({ enabled: canRequestAdmission });
  const requestByVisit = new Map<string, AdmissionRequest>();
  for (const request of requests.data?.items ?? []) {
    if (!requestByVisit.has(request.encounter_id)) {
      requestByVisit.set(request.encounter_id, request);
    }
  }

  if (board.isPending) {
    return (
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" aria-busy="true">
        {[0, 1, 2].map((column) => (
          <Skeleton key={column} className="h-48 w-full rounded-lg" />
        ))}
      </div>
    );
  }

  const columns = board.data ?? [];

  if (columns.length === 0) {
    return (
      <div className="text-muted-foreground flex flex-col items-center gap-2 rounded-lg border border-dashed py-16 text-center">
        <Users aria-hidden className="size-6" />
        <p className="text-foreground text-sm font-medium">{t("noDoctors")}</p>
        <p className="text-sm">{t("noDoctorsHint")}</p>
      </div>
    );
  }

  const totalWaiting = columns.reduce((sum, column) => sum + column.waiting, 0);

  return (
    <div className="space-y-3">
      <p className="text-muted-foreground text-sm">
        {t("boardSummary", { waiting: totalWaiting, doctors: columns.length })}
        {canManage ? ` · ${t("dragHint")}` : ""}
      </p>
      {/* `items-start`: each column is as tall as its own line. Stretched to
          the busiest doctor's height, a short column's "Seen today" list sat
          a screen away from the patients above it. */}
      <div
        className="grid items-start gap-4 md:grid-cols-2 xl:grid-cols-3"
        data-testid="doctor-queues"
      >
        {columns.map((column) => (
          <DoctorColumn
            key={column.doctor_id}
            column={column}
            columns={columns}
            canManage={canManage}
            onDragStateChange={setDragging}
            canRequestAdmission={canRequestAdmission}
            requestByVisit={requestByVisit}
            pendingSeen={seen.pending}
            onMarkSeen={seen.mark}
            onUndoSeen={seen.undo}
          />
        ))}
      </div>
    </div>
  );
}

// The statuses that hold a place in the line and so can be re-ranked. Mirrors
// the backend's rule so the handle is absent rather than present-and-failing;
// the backend still decides.
const RANKABLE = new Set<QueueBoardEntry["status"]>(["WAITING", "CALLED", "SKIPPED"]);

// The statuses a token can be moved to another doctor from. Narrower: a
// patient the doctor has just called is expected to walk in.
const MOVABLE = new Set<QueueBoardEntry["status"]>(["WAITING", "SKIPPED"]);

// How many of today's seen patients a column shows before "Show all". A
// busy clinic sees sixty; the line still has to be visible above them.
const SEEN_PREVIEW = 5;

function DoctorColumn({
  column,
  columns,
  canManage,
  canRequestAdmission,
  requestByVisit,
  onDragStateChange,
  pendingSeen,
  onMarkSeen,
  onUndoSeen,
}: {
  column: DoctorQueue;
  columns: DoctorQueue[];
  canManage: boolean;
  canRequestAdmission: boolean;
  requestByVisit: ReadonlyMap<string, AdmissionRequest>;
  onDragStateChange: (dragging: boolean) => void;
  pendingSeen: ReadonlySet<string>;
  onMarkSeen: (entryId: string) => void;
  onUndoSeen: (entryId: string) => void;
}) {
  const t = useTranslations("queue");
  const reorder = useReorderEntry();
  const [showAllSeen, setShowAllSeen] = useState(false);
  // Optional in the generated type only because the backend gives it a
  // default; the board always sends it.
  const entries = column.entries ?? [];
  const seen = column.seen ?? [];
  const seenShown = showAllSeen ? seen : seen.slice(0, SEEN_PREVIEW);

  // The big number on each row is the patient's place in the line — 1 is
  // next — counted from the order on screen, so it follows a drag the moment
  // the row lands. It used to be the token number, which stays with the
  // patient (it is printed on their slip) and so read 4, 1, 2, 3 as soon as
  // anyone re-ranked. A patient already with the doctor holds no place and
  // gets no number.
  const places = new Map<string, number>();
  for (const entry of entries) {
    if (RANKABLE.has(entry.status)) places.set(entry.id, places.size + 1);
  }

  // A small distance before a drag begins, so a tap on the handle is a tap
  // and a slightly wobbly click does not pick the row up. Touch waits a
  // beat longer, because a finger that starts moving at once is scrolling.
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    onDragStateChange(false);
    if (!over || active.id === over.id) return;

    const from = entries.findIndex((entry) => entry.id === active.id);
    const to = entries.findIndex((entry) => entry.id === over.id);
    if (from === -1 || to === -1) return;

    // The neighbour the row lands after, in the *new* order — skipping any
    // row that has no place in the line (with the doctor), because the
    // backend anchors only on rows that do. Nothing above → the top.
    const next = [...entries];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    const anchor = next
      .slice(0, to)
      .reverse()
      .find((entry) => RANKABLE.has(entry.status));

    reorder.mutate({
      doctorId: column.doctor_id,
      entryId: moved.id,
      from,
      to,
      afterEntryId: anchor?.id ?? null,
    });
  };

  // The same threshold as the row highlight and the reporting module's
  // delay alert, so the column header and the dashboard agree.
  const behind = (column.longest_wait_minutes ?? 0) >= 30;

  return (
    <section
      className={cn(
        "flex flex-col rounded-lg border",
        !column.is_accepting_appointments && "opacity-70",
      )}
      aria-labelledby={`queue-${column.doctor_id}`}
      data-testid="doctor-queue"
    >
      <header className="space-y-1 border-b p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 id={`queue-${column.doctor_id}`} className="truncate font-semibold">
              {column.doctor_name}
            </h2>
            <p className="text-muted-foreground truncate text-xs">
              {[column.specialty, column.department_name].filter(Boolean).join(" · ") || " "}
            </p>
          </div>
          {/* The number reception balances by — large, because it is read
              from across a counter. */}
          <div
            className={cn(
              "tabular shrink-0 rounded-md px-2.5 py-1 text-right",
              column.waiting === 0
                ? "bg-success/10 text-success"
                : behind
                  ? "bg-caution/15 text-caution-foreground"
                  : "bg-info/10 text-info",
            )}
          >
            <div className="text-2xl leading-none font-semibold">{column.waiting}</div>
            <div className="text-[10px] font-medium uppercase">{t("waiting")}</div>
          </div>
        </div>

        <dl className="text-muted-foreground tabular flex flex-wrap gap-x-3 text-xs">
          <div>
            <dt className="sr-only">{t("inConsultation")}</dt>
            <dd>
              {t("inConsultation")}: {column.in_consultation}
            </dd>
          </div>
          <div>
            <dt className="sr-only">{t("completed")}</dt>
            <dd>
              {t("completed")}: {column.completed}
            </dd>
          </div>
          {column.longest_wait_minutes !== null && column.longest_wait_minutes !== undefined ? (
            <div className={cn(behind && "text-caution-foreground font-medium")}>
              <dt className="sr-only">{t("longestWait")}</dt>
              <dd>
                {t("longestWait")}: {t("minutes", { count: column.longest_wait_minutes })}
              </dd>
            </div>
          ) : null}
          {!column.is_accepting_appointments ? (
            <div className="text-critical font-medium">
              <dd>{t("notAccepting")}</dd>
            </div>
          ) : null}
        </dl>
      </header>

      {entries.length === 0 ? (
        <p className="text-muted-foreground flex-1 px-3 py-6 text-center text-sm">
          {t("columnEmpty")}
        </p>
      ) : (
        // One context per column: a row can be dragged up and down its own
        // line and nowhere else. Vertical only, and clamped to the list, so
        // a drag that wanders sideways still reads as a drag in this list.
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          modifiers={[restrictToVerticalAxis, restrictToParentElement]}
          onDragStart={() => onDragStateChange(true)}
          onDragCancel={() => onDragStateChange(false)}
          onDragEnd={handleDragEnd}
          accessibility={{
            screenReaderInstructions: { draggable: t("dragInstructions") },
          }}
        >
          <SortableContext
            items={entries.map((entry) => entry.id)}
            strategy={verticalListSortingStrategy}
          >
            <ul className="divide-border flex-1 divide-y">
              {entries.map((entry) => {
                const marking = pendingSeen.has(entry.id);
                return (
                  <ColumnRow
                    key={entry.id}
                    entry={entry}
                    place={places.get(entry.id) ?? null}
                    columns={columns}
                    canManage={canManage}
                    draggable={canManage && !marking && RANKABLE.has(entry.status)}
                    marking={marking}
                    onMarkSeen={() => onMarkSeen(entry.id)}
                    onUndoSeen={() => onUndoSeen(entry.id)}
                  />
                );
              })}
            </ul>
          </SortableContext>
        </DndContext>
      )}

      {seen.length > 0 ? (
        <div className="border-t" data-testid="seen-list">
          <h3 className="text-muted-foreground px-3 pt-2 text-xs font-medium uppercase">
            {t("seenToday", { count: seen.length })}
          </h3>
          <ul className="divide-border divide-y">
            {seenShown.map((entry) => (
              <SeenRow
                key={entry.id}
                entry={entry}
                canRequestAdmission={canRequestAdmission}
                request={
                  entry.encounter_id ? (requestByVisit.get(entry.encounter_id) ?? null) : null
                }
              />
            ))}
          </ul>
          {seen.length > SEEN_PREVIEW ? (
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground h-tap w-full text-xs font-medium underline-offset-4 hover:underline"
              onClick={() => setShowAllSeen((open) => !open)}
            >
              {showAllSeen ? t("showFewerSeen") : t("showAllSeen", { count: seen.length })}
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

/**
 * A patient the doctor has already seen: kept, struck through.
 *
 * The one thing left to do with them from here is the admission hand-off.
 * Until they are sent the row offers "Send to admission"; afterwards it says
 * where they are — at the desk, or admitted — so nobody sends them twice and
 * a relative asking "where did she go?" gets an answer. A request the desk
 * turned away offers the button again.
 */
function SeenRow({
  entry,
  canRequestAdmission,
  request,
}: {
  entry: QueueBoardEntry;
  canRequestAdmission: boolean;
  /** The latest admission request for this visit today, if any. */
  request: AdmissionRequest | null;
}) {
  const t = useTranslations("queue");
  const tAdmit = useTranslations("admissions");
  const common = useTranslations("common");
  const format = useFormatter();
  const [sending, setSending] = useState(false);
  const name = entry.patient_name ?? common("notRecorded");
  // Not for a patient recorded as deceased — the server refuses it, and a
  // button that can only fail is worse than no button.
  const canSend =
    canRequestAdmission &&
    Boolean(entry.encounter_id) &&
    !entry.patient_is_deceased &&
    (request === null || request.status === "CANCELLED");

  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1 px-2 py-1.5" data-testid="seen-row">
      <div className="text-muted-foreground min-w-[10rem] flex-1 pl-1">
        <div className="truncate text-sm line-through">{name}</div>
        <div className="tabular text-xs whitespace-nowrap">
          {t("token")} {entry.token_number}
          {entry.completed_at
            ? ` · ${t("seenAt", {
                time: format.dateTime(new Date(entry.completed_at), {
                  hour: "2-digit",
                  minute: "2-digit",
                }),
              })}`
            : ""}
        </div>
      </div>

      {request?.status === "PENDING" ? (
        <span
          className="bg-caution/15 text-caution-foreground border-caution/40 shrink-0 rounded border px-1.5 py-0.5 text-xs font-medium"
          data-testid="admission-status"
        >
          {tAdmit("atDesk")}
        </span>
      ) : request?.status === "ADMITTED" ? (
        <span
          className="bg-info/10 text-info border-info/25 shrink-0 rounded border px-1.5 py-0.5 text-xs font-medium"
          data-testid="admission-status"
        >
          {tAdmit("admitted")}
        </span>
      ) : null}

      {canSend && entry.encounter_id ? (
        <>
          <Button
            size="sm"
            variant="outline"
            className="h-tap shrink-0"
            onClick={() => setSending(true)}
            aria-label={tAdmit("sendAria", { name })}
            data-testid="send-to-admission"
          >
            <BedDouble aria-hidden className="size-4" />
            {tAdmit("sendToAdmission")}
          </Button>
          <SendToAdmissionDialog
            encounterId={entry.encounter_id}
            patientName={name}
            open={sending}
            onOpenChange={setSending}
          />
        </>
      ) : null}
    </li>
  );
}

function ColumnRow({
  entry,
  place,
  columns,
  canManage,
  draggable,
  marking,
  onMarkSeen,
  onUndoSeen,
}: {
  entry: QueueBoardEntry;
  /** 1-based place in the line; `null` for a patient already with the doctor. */
  place: number | null;
  columns: DoctorQueue[];
  canManage: boolean;
  draggable: boolean;
  /** "Seen" was clicked and the undo window is still open. */
  marking: boolean;
  onMarkSeen: () => void;
  onUndoSeen: () => void;
}) {
  const t = useTranslations("queue");
  const common = useTranslations("common");
  const elapsed = useElapsed(entry.checked_in_at);
  const [moving, setMoving] = useState(false);

  // Every row is registered with the sortable list — even the ones that
  // cannot be dragged — so indexes line up with what the server sent.
  // `disabled` rows simply never pick up.
  const {
    attributes,
    listeners,
    setNodeRef,
    setActivatorNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: entry.id, disabled: !draggable });

  const overdue = elapsed >= 30 && entry.status === "WAITING";
  const prioritised = entry.priority < 30;
  const movable = canManage && !marking && MOVABLE.has(entry.status);
  const name = entry.patient_name ?? common("notRecorded");

  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={cn(
        "bg-background flex items-center gap-2 px-2 py-2",
        overdue && "bg-caution/10",
        // Lifted above its neighbours while in the air, so it reads as the
        // thing being carried rather than sliding under the others.
        isDragging && "relative z-10 shadow-md",
      )}
      data-testid="queue-row"
    >
      {draggable ? (
        <button
          ref={setActivatorNodeRef}
          type="button"
          className="text-muted-foreground hover:text-foreground h-tap -ml-1 flex w-8 shrink-0 cursor-grab touch-none items-center justify-center rounded active:cursor-grabbing"
          aria-label={t("dragAria", { name })}
          data-testid="drag-handle"
          {...attributes}
          {...listeners}
        >
          <GripVertical aria-hidden className="size-4" />
        </button>
      ) : (
        // Keep the columns aligned whether or not the row has a handle.
        <span aria-hidden className="w-8 shrink-0" />
      )}

      <span className="tabular w-7 shrink-0 text-lg font-semibold" data-testid="place">
        {place !== null ? (
          <>
            <span className="sr-only">{t("placeInLine")} </span>
            {place}
          </>
        ) : null}
      </span>

      <div className="min-w-0 flex-1">
        {/* Wraps to two lines rather than truncating: with two buttons on
            the row the column is narrow, and "Ramesh Chandra G…" is not a
            name anyone can call out. */}
        <div
          className={cn(
            "line-clamp-2 text-sm font-medium break-words",
            marking && "text-muted-foreground line-through",
          )}
          data-testid="patient-name"
        >
          {name}
        </div>
        <div className="text-muted-foreground tabular flex flex-wrap items-center gap-x-2 text-xs">
          {/* Still shown: it is on the patient's slip and it is what gets
              called out across the waiting room. */}
          <span className="text-foreground font-medium" data-testid="token">
            {t("token")} {entry.token_number}
          </span>
          <span className={cn(overdue && "text-caution-foreground font-medium")}>
            {t("minutes", { count: elapsed })}
          </span>
          <QueueStatusChip status={entry.status} />
          {prioritised ? (
            <span className="bg-caution/20 text-caution-foreground rounded px-1.5 py-0.5 text-[10px] font-medium">
              {entry.priority_reason ?? t("runningLate")}
            </span>
          ) : null}
        </div>
      </div>

      {canManage && marking ? (
        <Button
          size="sm"
          variant="outline"
          className="h-tap shrink-0"
          onClick={onUndoSeen}
          aria-label={t("undoSeenAria", { name })}
        >
          <Undo2 aria-hidden className="size-4" />
          {t("undo")}
        </Button>
      ) : null}

      {canManage && !marking ? (
        <Button
          size="sm"
          variant="outline"
          className="h-tap shrink-0"
          onClick={onMarkSeen}
          aria-label={t("markSeenAria", { name })}
          data-testid="mark-seen"
        >
          <Check aria-hidden className="size-4" />
          {t("markSeen")}
        </Button>
      ) : null}

      {movable ? (
        <>
          {/* Icon only: the row now carries two actions, and the name needs
              the width more than the word "Move" does. The label is still
              there for screen readers and on hover. */}
          <Button
            size="sm"
            variant="outline"
            className="h-tap shrink-0"
            onClick={() => setMoving(true)}
            aria-label={t("moveAria", { name })}
            title={t("moveTitle")}
          >
            <ArrowRightLeft aria-hidden className="size-4" />
          </Button>
          <ReassignDialog entry={entry} columns={columns} open={moving} onOpenChange={setMoving} />
        </>
      ) : null}
    </li>
  );
}
