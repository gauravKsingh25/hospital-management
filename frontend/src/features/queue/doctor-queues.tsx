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
import { useState, type ReactNode } from "react";
import {
  ArrowRightLeft,
  BedDouble,
  Check,
  ChevronDown,
  CircleCheck,
  GripVertical,
  Undo2,
  Users,
} from "lucide-react";

import { QueueStatusChip } from "@/components/status-chip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { SendToAdmissionDialog } from "@/features/admissions/send-to-admission-dialog";
import { CollectStrip } from "@/features/billing/collect-strip";
import { formatMoney, isPositive } from "@/features/billing/money";
import { useTodaysAdmissionRequests } from "@/features/admissions/use-admission-requests";
import { PriorityTags } from "@/features/queue/priority-tags";
import { ReassignDialog } from "@/features/queue/reassign-dialog";
import { useMarkSeen, useQueueBoard, useReorderEntry } from "@/features/queue/use-queue-board";
import { useElapsed } from "@/lib/hooks/use-elapsed";
import { useWaitFormatter } from "@/lib/hooks/use-wait";
import { cn } from "@/lib/utils";
import type { AdmissionRequest, DoctorQueue, QueueBoardEntry } from "@/types/api";

/**
 * Reception's queue: two doctors side by side, every doctor one tap away.
 *
 * The nurse's screen (`LiveQueue`) is one flat list because a nurse calling
 * for vitals works across every clinic. Reception's question is different —
 * not "who is next" but "which doctor is drowning, and where can I send the
 * person in front of me". That is a comparison between doctors, so:
 *
 * - **The chip row** names every active doctor with their waiting count,
 *   busiest first — including the ones with nobody waiting, because the
 *   empty clinic is the answer. It is the whole building at a glance.
 * - **Two panes** show two doctors' lines in full. One column per doctor
 *   stopped working at the third doctor and the ninetieth patient: the page
 *   became a wall. Two is as many as can be compared on a counter screen;
 *   tapping a chip swaps that doctor into the right-hand pane. Below `lg`
 *   there is room for one, and the chips switch between them — `lg`, not
 *   `md`, because the sidebar appears at `md` and leaves a portrait tablet
 *   two columns a quarter-screen wide, too narrow for a name.
 *
 * Each patient is one quiet line — place, name, token, wait, and what they
 * owe — with no buttons on it. Tapping the line opens its actions underneath,
 * one line at a time across the board. The buttons on every row were what
 * made ninety patients unreadable; the one extra tap is the price of that.
 *
 * What reception can do from an open line, all without leaving the screen:
 *
 * - **Seen** — record that the doctor has seen the patient. The line closes,
 *   the name is struck through at once, and Undo sits on the line itself for
 *   a few seconds, then the row drops into the pane's "Seen today" list.
 * - **Move** a patient to another doctor's line.
 * - **Get payment / Paid** — the money strip, for a user who can take
 *   payments. It stays on seen rows too: the test the doctor ordered is paid
 *   for after the consultation, not before.
 *
 * And from the line without opening it, **re-rank** by dragging the handle —
 * the child with the fever above the routine follow-up. Rank is stored on the
 * server and drives the doctor's own list and "how many ahead", so the drag
 * is the order, not a picture of it. Pointer, touch and keyboard (space,
 * arrows, space) all work; the handle is the only drag surface so the rest of
 * the line still behaves like a button.
 *
 * Dragging is confined to one pane. Dropping a patient on another doctor is a
 * reassignment with consequences (a new token, a different chart), and that
 * deserves the dialog's confirmation rather than a slip of the wrist.
 */
export function DoctorQueues({
  canManage = false,
  canRequestAdmission = false,
  canCollect = false,
}: {
  canManage?: boolean;
  /** Offer "Send to admission" on seen patients (`admission:request`). */
  canRequestAdmission?: boolean;
  /** Offer "Get payment" / "Paid" on rows that owe money. */
  canCollect?: boolean;
}) {
  const t = useTranslations("queue");
  const [dragging, setDragging] = useState(false);
  const board = useQueueBoard({ paused: dragging });
  // Board-level so a pending "Seen" survives the pane re-rendering around it
  // on the next poll.
  const seen = useMarkSeen();
  // Board-level for the same reason, and so opening one line closes the
  // other — two open lines is two sets of buttons to tell apart.
  const [openId, setOpenId] = useState<string | null>(null);
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

  const columns = board.data ?? [];
  const panes = useShownDoctors(columns);

  if (board.isPending) {
    return (
      <div className="grid gap-4 lg:grid-cols-2" aria-busy="true">
        {[0, 1].map((pane) => (
          <Skeleton key={pane} className="h-48 w-full rounded-lg" />
        ))}
      </div>
    );
  }

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
  const toggle = (entryId: string) => setOpenId((open) => (open === entryId ? null : entryId));
  const close = () => setOpenId(null);

  return (
    <div className="space-y-3">
      <p className="text-muted-foreground text-sm">
        {t("boardSummary", { waiting: totalWaiting, doctors: columns.length })}
        {canManage || canCollect ? ` · ${t("tapHint")}` : ""}
        {canManage ? ` ${t("dragHint")}` : ""}
      </p>

      {columns.length > 1 ? (
        <div
          role="group"
          aria-label={t("doctorPicker")}
          className="flex flex-wrap gap-2"
          data-testid="doctor-chips"
        >
          {columns.map((column) => (
            <DoctorChip
              key={column.doctor_id}
              column={column}
              shown={panes.shown.includes(column.doctor_id)}
              active={panes.active === column.doctor_id}
              onPick={() => panes.pick(column.doctor_id)}
            />
          ))}
        </div>
      ) : null}

      {/* `items-start`: each pane is as tall as its own line. Stretched to the
          busier doctor's height, the quiet pane's "Seen today" list sat a
          screen away from the patients above it. */}
      <div className="grid items-start gap-4 lg:grid-cols-2" data-testid="doctor-queues">
        {panes.shown.map((doctorId) => {
          const column = columns.find((candidate) => candidate.doctor_id === doctorId);
          if (!column) return null;
          return (
            <DoctorColumn
              key={doctorId}
              column={column}
              columns={columns}
              // Below `lg` only the chosen pane is drawn; the other is a tap
              // on its chip away.
              className={cn(doctorId !== panes.active && "max-lg:hidden")}
              canManage={canManage}
              onDragStateChange={setDragging}
              canRequestAdmission={canRequestAdmission}
              canCollect={canCollect}
              requestByVisit={requestByVisit}
              pendingSeen={seen.pending}
              onMarkSeen={(entryId) => {
                close();
                seen.mark(entryId);
              }}
              onUndoSeen={seen.undo}
              openId={openId}
              onToggle={toggle}
              onClose={close}
            />
          );
        })}
      </div>
    </div>
  );
}

// How many doctors are drawn side by side.
const PANES = 2;

// Remembered per browser: a counter PC is one desk, and the desk that always
// balances Dr Rao against Dr Mehta should not have to choose them every
// morning. Doctor ids only — nothing about a patient is stored.
const SHOWN_KEY = "reception.queue.shown";

function readShown(): string[] {
  try {
    const raw = window.localStorage.getItem(SHOWN_KEY);
    const value: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string") : [];
  } catch {
    return [];
  }
}

function writeShown(ids: readonly string[]): void {
  try {
    window.localStorage.setItem(SHOWN_KEY, JSON.stringify(ids));
  } catch {
    // Private window or blocked storage: the choice lasts until reload.
  }
}

/** The kept choices that still have a column, topped up busiest first. */
function resolveShown(chosen: readonly string[], columns: readonly DoctorQueue[]): string[] {
  const present = new Set(columns.map((column) => column.doctor_id));
  const shown = chosen.filter((id) => present.has(id)).slice(0, PANES);
  for (const column of columns) {
    if (shown.length >= PANES) break;
    if (!shown.includes(column.doctor_id)) shown.push(column.doctor_id);
  }
  return shown;
}

/**
 * Which doctors the panes show, and which one a narrow screen shows.
 *
 * The default is the two busiest, but only once: the board arrives busiest
 * first and re-ranks on every poll, and a pane that swaps doctors under the
 * receptionist's hand is worse than one that has drifted from "busiest". So
 * the first resolution is pinned, and only a tap on a chip changes it.
 */
function useShownDoctors(columns: readonly DoctorQueue[]) {
  // The board is fetched on the client, so nothing that reads this is in the
  // server-rendered HTML — reading storage here cannot cause a mismatch.
  const [chosen, setChosen] = useState<string[]>(() =>
    typeof window === "undefined" ? [] : readShown(),
  );
  const [focus, setFocus] = useState<string | null>(null);

  const shown = resolveShown(chosen, columns);
  // Pin a resolution that differs from what is kept (first load, or a doctor
  // who left the board). Set during render, React's pattern for state that
  // follows props; it converges because resolving `shown` again yields itself.
  if (columns.length > 0 && shown.join() !== chosen.join()) {
    setChosen(shown);
  }

  const active = focus !== null && shown.includes(focus) ? focus : (shown[0] ?? null);

  const pick = (doctorId: string) => {
    setFocus(doctorId);
    if (shown.includes(doctorId)) return;
    // Into the right-hand pane: the left one is the doctor being balanced
    // against, and the right one is the one being compared in turn.
    const next = shown.length < PANES ? [...shown, doctorId] : [shown[0], doctorId];
    setChosen(next);
    writeShown(next);
  };

  return { shown, active, pick };
}

function DoctorChip({
  column,
  shown,
  active,
  onPick,
}: {
  column: DoctorQueue;
  /** In one of the two panes. */
  shown: boolean;
  /** The pane a narrow screen draws. */
  active: boolean;
  onPick: () => void;
}) {
  const t = useTranslations("queue");
  // The same threshold and colours as the pane header, so the chip and the
  // pane it opens say the same thing.
  const behind = (column.longest_wait_minutes ?? 0) >= 30;

  return (
    <button
      type="button"
      onClick={onPick}
      aria-pressed={shown}
      aria-label={`${column.doctor_name}, ${t("waitingCount", { count: column.waiting })}${
        column.is_accepting_appointments ? "" : `, ${t("notAccepting")}`
      }`}
      className={cn(
        "h-tap bg-background flex items-center gap-2 rounded-full border px-3 text-sm font-medium transition-colors",
        "hover:border-primary/60 focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none",
        // Selected where it is visible: both panes on a wide screen, only the
        // active one on a narrow screen.
        shown && active && "border-primary bg-primary/10",
        shown && !active && "lg:border-primary lg:bg-primary/10",
        !column.is_accepting_appointments && "opacity-60",
      )}
      data-testid="doctor-chip"
    >
      <span className="max-w-[12rem] truncate">{column.doctor_name}</span>
      <span
        aria-hidden
        className={cn(
          "tabular rounded-full px-2 text-xs font-semibold",
          column.waiting === 0
            ? "bg-success/15 text-success"
            : behind
              ? "bg-caution/20 text-caution-foreground"
              : "bg-info/15 text-info",
        )}
      >
        {column.waiting}
      </span>
    </button>
  );
}

// The statuses that hold a place in the line and so can be re-ranked. Mirrors
// the backend's rule so the handle is absent rather than present-and-failing;
// the backend still decides.
const RANKABLE = new Set<QueueBoardEntry["status"]>(["WAITING", "CALLED", "SKIPPED"]);

// The statuses a token can be moved to another doctor from. Narrower: a
// patient the doctor has just called is expected to walk in.
const MOVABLE = new Set<QueueBoardEntry["status"]>(["WAITING", "SKIPPED"]);

// How many of today's seen patients a pane shows before "Show all". A busy
// clinic sees sixty; the line still has to be visible above them.
const SEEN_PREVIEW = 5;

/** Whether the money strip has anything to take — the same test it makes. */
function owes(entry: QueueBoardEntry): boolean {
  return (
    Boolean(entry.encounter_id) &&
    !entry.patient_is_deceased &&
    isPositive(entry.balance_due ?? null)
  );
}

function DoctorColumn({
  column,
  columns,
  className,
  canManage,
  canRequestAdmission,
  canCollect,
  requestByVisit,
  onDragStateChange,
  pendingSeen,
  onMarkSeen,
  onUndoSeen,
  openId,
  onToggle,
  onClose,
}: {
  column: DoctorQueue;
  columns: DoctorQueue[];
  className?: string;
  canManage: boolean;
  canRequestAdmission: boolean;
  canCollect: boolean;
  requestByVisit: ReadonlyMap<string, AdmissionRequest>;
  onDragStateChange: (dragging: boolean) => void;
  pendingSeen: ReadonlySet<string>;
  onMarkSeen: (entryId: string) => void;
  onUndoSeen: (entryId: string) => void;
  openId: string | null;
  onToggle: (entryId: string) => void;
  onClose: () => void;
}) {
  const t = useTranslations("queue");
  const wait = useWaitFormatter();
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
    useSensor(TouchSensor, {
      activationConstraint: { delay: 200, tolerance: 8 },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
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
  // delay alert, so the pane header and the dashboard agree.
  const behind = (column.longest_wait_minutes ?? 0) >= 30;

  return (
    <section
      className={cn(
        "bg-background flex flex-col overflow-hidden rounded-lg border",
        !column.is_accepting_appointments && "opacity-70",
        className,
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
            <div className="text-xs font-medium uppercase">{t("waiting")}</div>
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
                {t("longestWait")}: {wait(column.longest_wait_minutes)}
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
        <p className="text-muted-foreground px-3 py-6 text-center text-sm">{t("columnEmpty")}</p>
      ) : (
        // The line scrolls inside its pane rather than stretching the page:
        // ninety patients made the page ninety rows tall, and the other pane,
        // the seen list and the header all scrolled away with them. Every row
        // is still drawn, so a drag can reach any place and Find (Ctrl+F)
        // still finds a name.
        <div
          className="max-h-[min(40rem,65vh)] overflow-y-auto overscroll-contain"
          data-testid="queue-scroll"
        >
          {/* One context per pane: a row can be dragged up and down its own
              line and nowhere else. Vertical only, and clamped to the list,
              so a drag that wanders sideways still reads as a drag in this
              list. */}
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            modifiers={[restrictToVerticalAxis, restrictToParentElement]}
            onDragStart={() => {
              onClose();
              onDragStateChange(true);
            }}
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
              <ul className="divide-border divide-y">
                {entries.map((entry) => {
                  const marking = pendingSeen.has(entry.id);
                  return (
                    <ColumnRow
                      key={entry.id}
                      entry={entry}
                      place={places.get(entry.id) ?? null}
                      columns={columns}
                      canManage={canManage}
                      canCollect={canCollect}
                      draggable={canManage && !marking && RANKABLE.has(entry.status)}
                      marking={marking}
                      open={openId === entry.id}
                      onToggle={() => onToggle(entry.id)}
                      onClose={onClose}
                      onMarkSeen={() => onMarkSeen(entry.id)}
                      onUndoSeen={() => onUndoSeen(entry.id)}
                    />
                  );
                })}
              </ul>
            </SortableContext>
          </DndContext>
        </div>
      )}

      {seen.length > 0 ? (
        <div className="border-t" data-testid="seen-list">
          <h3 className="text-muted-foreground bg-muted/40 px-3 py-2 text-xs font-medium uppercase">
            {t("seenToday", { count: seen.length })}
          </h3>
          <ul className="divide-border divide-y">
            {seenShown.map((entry) => (
              <SeenRow
                key={entry.id}
                entry={entry}
                canRequestAdmission={canRequestAdmission}
                canCollect={canCollect}
                request={
                  entry.encounter_id ? (requestByVisit.get(entry.encounter_id) ?? null) : null
                }
                open={openId === entry.id}
                onToggle={() => onToggle(entry.id)}
                onClose={onClose}
              />
            ))}
          </ul>
          {seen.length > SEEN_PREVIEW ? (
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground h-tap w-full border-t text-xs font-medium underline-offset-4 hover:underline"
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
 * The tappable body of a line: a button that opens the line's actions when
 * it has any, plain text when it has none (a read-only role), so nothing
 * looks pressable that does nothing.
 */
function RowToggle({
  actionable,
  open,
  panelId,
  onToggle,
  children,
}: {
  actionable: boolean;
  open: boolean;
  panelId: string;
  onToggle: () => void;
  children: ReactNode;
}) {
  const layout = "flex min-w-0 flex-1 items-center gap-2 py-2 text-left";
  if (!actionable) return <div className={layout}>{children}</div>;
  return (
    <button
      type="button"
      className={cn(
        layout,
        "focus-visible:ring-ring cursor-pointer rounded focus-visible:ring-2 focus-visible:outline-none",
      )}
      aria-expanded={open}
      aria-controls={open ? panelId : undefined}
      onClick={onToggle}
      data-testid="row-toggle"
    >
      {children}
      <ChevronDown
        aria-hidden
        className={cn(
          "text-muted-foreground size-4 shrink-0 transition-transform",
          open && "rotate-180",
        )}
      />
    </button>
  );
}

/**
 * What the line owes, without opening it: amber "₹500 due", or green "Paid
 * ₹500" once settled. Nothing for a deceased patient — that bill is settled
 * with the family on the billing screen (CLAUDE.md §6).
 */
function MoneyTag({ entry }: { entry: QueueBoardEntry }) {
  const t = useTranslations("queue");
  const tBilling = useTranslations("billing");
  if (!entry.encounter_id || entry.patient_is_deceased) return null;

  if (isPositive(entry.balance_due ?? null)) {
    return (
      <span
        className="bg-caution/15 text-caution-foreground tabular shrink-0 rounded-md px-2 py-0.5 text-xs font-semibold whitespace-nowrap"
        data-testid="due-tag"
      >
        {t("dueTag", { amount: formatMoney(entry.balance_due) })}
      </span>
    );
  }
  if (isPositive(entry.amount_paid ?? null)) {
    return (
      <span
        className="bg-success/10 text-success tabular flex shrink-0 items-center gap-1 rounded-md px-2 py-0.5 text-xs font-semibold whitespace-nowrap"
        data-testid="paid-chip"
      >
        <CircleCheck aria-hidden className="size-3.5" />
        {tBilling("paidAmount", { amount: formatMoney(entry.amount_paid) })}
      </span>
    );
  }
  return null;
}

/**
 * A patient the doctor has already seen: kept, struck through.
 *
 * The one thing left to do with them from here is the admission hand-off.
 * Until they are sent the open line offers "Send to admission"; afterwards
 * the line says where they are — at the desk, or admitted — so nobody sends
 * them twice and a relative asking "where did she go?" gets an answer. A
 * request the desk turned away offers the button again.
 */
function SeenRow({
  entry,
  canRequestAdmission,
  canCollect,
  request,
  open,
  onToggle,
  onClose,
}: {
  entry: QueueBoardEntry;
  canRequestAdmission: boolean;
  canCollect: boolean;
  /** The latest admission request for this visit today, if any. */
  request: AdmissionRequest | null;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
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
  const collectable = canCollect && owes(entry);
  const actionable = canSend || collectable;
  const expanded = open && actionable;
  const panelId = `seen-actions-${entry.id}`;

  return (
    <li className={cn(expanded && "bg-primary/5")} data-testid="seen-row">
      <div className="flex items-center gap-2 pr-2 pl-3">
        <RowToggle actionable={actionable} open={expanded} panelId={panelId} onToggle={onToggle}>
          <div className="text-muted-foreground min-w-0 flex-1">
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
        </RowToggle>

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

        {canCollect ? <MoneyTag entry={entry} /> : null}
      </div>

      {expanded ? (
        <div
          id={panelId}
          className="flex flex-wrap items-center gap-2 px-3 pb-2"
          data-testid="row-actions"
        >
          {collectable ? (
            <CollectStrip
              entry={entry}
              className="min-w-[min(20rem,100%)] flex-1"
              onCollected={onClose}
            />
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
        </div>
      ) : null}
    </li>
  );
}

function ColumnRow({
  entry,
  place,
  columns,
  canManage,
  canCollect,
  draggable,
  marking,
  open,
  onToggle,
  onClose,
  onMarkSeen,
  onUndoSeen,
}: {
  entry: QueueBoardEntry;
  /** 1-based place in the line; `null` for a patient already with the doctor. */
  place: number | null;
  columns: DoctorQueue[];
  canManage: boolean;
  canCollect: boolean;
  draggable: boolean;
  /** "Seen" was clicked and the undo window is still open. */
  marking: boolean;
  /** This line's actions are showing. */
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  onMarkSeen: () => void;
  onUndoSeen: () => void;
}) {
  const t = useTranslations("queue");
  const wait = useWaitFormatter();
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
  const movable = canManage && !marking && MOVABLE.has(entry.status);
  const collectable = canCollect && owes(entry);
  // A line mid-"Seen" has one action, Undo, and it sits on the line itself.
  const actionable = !marking && (canManage || collectable);
  const expanded = open && actionable;
  const name = entry.patient_name ?? common("notRecorded");
  const panelId = `queue-actions-${entry.id}`;

  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={cn(
        "bg-background",
        overdue && "bg-caution/10",
        expanded && "bg-primary/5 shadow-[inset_3px_0_0_var(--color-primary)]",
        // Lifted above its neighbours while in the air, so it reads as the
        // thing being carried rather than sliding under the others.
        isDragging && "relative z-10 shadow-md",
      )}
      data-testid="queue-row"
    >
      <div className="flex items-center gap-2 pr-2 pl-1">
        {draggable ? (
          <button
            ref={setActivatorNodeRef}
            type="button"
            className="text-muted-foreground hover:text-foreground h-tap flex w-11 shrink-0 cursor-grab touch-none items-center justify-center rounded active:cursor-grabbing"
            aria-label={t("dragAria", { name })}
            data-testid="drag-handle"
            {...attributes}
            {...listeners}
          >
            <GripVertical aria-hidden className="size-4" />
          </button>
        ) : (
          // Keep the lines aligned whether or not the row has a handle.
          <span aria-hidden className="w-11 shrink-0" />
        )}

        <RowToggle actionable={actionable} open={expanded} panelId={panelId} onToggle={onToggle}>
          <span className="tabular w-7 shrink-0 text-lg font-semibold" data-testid="place">
            {place !== null ? (
              <>
                <span className="sr-only">{t("placeInLine")} </span>
                {place}
              </>
            ) : null}
          </span>

          <div className="min-w-0 flex-1">
            {/* Wraps to two lines rather than truncating: in a half-screen
                pane "Ramesh Chandra G…" is not a name anyone can call out. */}
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
                {wait(elapsed)}
              </span>
              <QueueStatusChip status={entry.status} />
              <PriorityTags entry={entry} />
            </div>
          </div>
        </RowToggle>

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
        ) : canCollect ? (
          <MoneyTag entry={entry} />
        ) : null}
      </div>

      {expanded ? (
        // Indented to the name, so the buttons read as belonging to it.
        <div
          id={panelId}
          className="flex flex-wrap items-center gap-2 pr-2 pb-2 pl-10"
          data-testid="row-actions"
        >
          {collectable ? (
            <CollectStrip
              entry={entry}
              className="min-w-[min(20rem,100%)] flex-1"
              onCollected={onClose}
            />
          ) : null}

          {/* Seen and Move wrap as a pair: Move alone on a second line read
              as belonging to the next patient. */}
          <div className="flex gap-2">
            {canManage ? (
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
                <Button
                  size="sm"
                  variant="outline"
                  className="h-tap shrink-0"
                  onClick={() => setMoving(true)}
                  aria-label={t("moveAria", { name })}
                  title={t("moveTitle")}
                >
                  <ArrowRightLeft aria-hidden className="size-4" />
                  {t("move")}
                </Button>
                <ReassignDialog
                  entry={entry}
                  columns={columns}
                  open={moving}
                  onOpenChange={setMoving}
                />
              </>
            ) : null}
          </div>
        </div>
      ) : null}
    </li>
  );
}
