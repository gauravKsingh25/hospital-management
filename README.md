# Hospital Patient-Journey Management System

Patient-journey management for a mid-size, multi-department private hospital in
India: registration → doctor assignment → OPD consultation → diagnostics →
discharge or IPD admission → billing → follow-up, with death, referral-out and
LAMA as first-class terminal states.

Architecture, conventions and build order are specified in
[CLAUDE.md](CLAUDE.md) — that file is the source of truth, this one is the
operating manual.

**Status: Phase 10 — the frontend's first vertical slice is in.** The backend
build order (CLAUDE.md §13 steps 1–10) is complete: tenancy, identity and RBAC;
patients; scheduling; the clinical Encounter state machine; diagnostics;
billing; notifications; inpatient care; and KPI reporting.

§13 step 11 is now underway. Next.js 16 with a backend-for-frontend proxy, and
the first slice built end to end: **sign-in, rapid registration with one-click
OPD, and the doctor's consultation screen.** The browser never holds a JWT —
tokens live in an httpOnly cookie and are attached server-side (see
[Frontend](#frontend)). The remaining modules have working APIs and no screens
yet.

⚠️ **Before judging the speed of anything, read
[Database latency](#database-latency-read-this-before-blaming-the-ui).** The
Neon project this was developed against sits in `us-east-2`, which costs ~870ms
per SQL round trip from India and makes CLAUDE.md §7b's 30-second registration
gate unreachable for reasons no amount of frontend work can fix.

---

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI 0.141 on Python 3.13 (3.12 minimum) |
| ORM | SQLModel 0.0.39 over SQLAlchemy 2.0, async, asyncpg driver |
| Migrations | Alembic 1.18 (async env) |
| Database | PostgreSQL 18 on [Neon](https://neon.tech) |
| Queue / cache | Redis 8 + ARQ (worker: `arq app.workers.main.WorkerSettings`) |
| Frontend | Next.js 16 (App Router, Turbopack) + React 19 + TypeScript 5 |
| UI | Tailwind CSS v4 + shadcn/ui (Base UI), lucide icons |
| Data fetching | TanStack Query v5 through a server-side BFF proxy |
| Forms | React Hook Form + Zod (mirroring the Pydantic schemas) |
| QR (print) | `qrcode-generator` (zero dependencies), rendered to SVG server-side |
| QR (camera) | the browser's own `BarcodeDetector` — no decoder library |
| Charts | Recharts 3, behind `next/dynamic` — one route loads it |
| i18n | next-intl — English + Hindi, cookie-resolved |
| e2e | Playwright, against a real backend |

---

## Prerequisites

- Docker Desktop (Compose v2), **or** Python 3.12+ if running the API directly
- Node.js 20+ (22 preferred) for the frontend
- A Neon project, or the bundled local Postgres container

---

## Quick start

```bash
cp .env.example .env      # fill in DATABASE_URL, DIRECT_URL and SECRET_KEY

cd backend
python scripts/bootstrap_db_role.py          # once per environment — see below
alembic upgrade head                         # schema, RBAC seed, RLS policies
python scripts/create_admin.py \
    --email you@example.com --name "Your Name"   # the first PLATFORM_ADMIN

python scripts/seed_demo.py                  # a demo tenant, staffed and priced

cd .. && docker compose up --build
```

- App: <http://localhost:3000>
- API: <http://localhost:8000>
- Interactive docs: <http://localhost:8000/docs>
- Health: `/health` (liveness), `/health/db`, `/health/redis`, `/health/ready`

`seed_demo.py` prints the accounts it created — one per front-line role, all
sharing one password. Sign in as `reception@demo.hospital` for the counter
workflow, `doctor@demo.hospital` for the consultation screen,
`lab@demo.hospital` for the diagnostics bench, `cashier@demo.hospital` for the
cash counter. Development only: those accounts have known passwords.

It also seeds a **default cash rate card, two priced services and one orderable
blood test**, and that is not decoration. Billing is deliberately total — it
never refuses a charge for missing configuration, so an unpriced consultation
lands at zero and flagged `needs_pricing` rather than blocking clinical work.
The consequence is that a hospital with no price list is a hospital where
nothing is ever owed, nothing reaches the counter, and the clearance half of
the journey looks like it works when it has simply never been exercised.

The source directory is bind-mounted with `--reload`, so edits are live without
a rebuild.

### Running the API without Docker

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Redis health will report 503 unless a Redis instance is reachable at
`REDIS_URL`. Nothing in Phase 0 depends on it — start one with
`docker compose up redis` when you want the full picture.

---

## Database configuration

Two connection strings are required, and they are **not** interchangeable
(CLAUDE.md §5):

| Variable | Endpoint | Used by | Why |
|---|---|---|---|
| `DATABASE_URL` | pooled (host contains `-pooler`) | the running API | Neon fronts Postgres with PgBouncer; the API opens and drops connections constantly |
| `DIRECT_URL` | unpooled | Alembic only | migrations need session-scoped advisory locks and single-connection DDL transactions, which a transaction-mode pooler breaks |

Both may be pasted straight from the Neon dashboard, `?sslmode=require` and
all: `app/core/config.py` rewrites the scheme to `postgresql+asyncpg` and strips
libpq-only parameters, and `app/core/database.py` applies a verifying TLS
context (stricter than `sslmode=require`, which encrypts but validates nothing).

### The application role — do not skip this

Managed Postgres owners (Neon's `neondb_owner`, RDS's master user) carry the
`BYPASSRLS` attribute, which overrides even `FORCE ROW LEVEL SECURITY`. An
application connecting as the owner ignores every tenant-isolation policy: they
sit in `pg_policies` looking correct and protect nothing. The role cannot revoke
the attribute from itself.

So there are two roles:

| Variable | Role | Purpose |
|---|---|---|
| `DIRECT_URL` | owner | migrations; owns the tables |
| `DATABASE_URL` | `hms_app` | the running application; subject to RLS |

`python scripts/bootstrap_db_role.py` creates `hms_app` and grants it DML on
existing and future tables. It never gets DDL — the application cannot drop a
table it merely reads. The API logs `rls_enforced` at startup and **refuses to
boot in production** if the role it connected as can bypass policies.

**Neon behaviours the code already handles:**

- *Auto-suspend.* Free-tier compute scales to zero when idle. The first request
  afterwards pays a cold start — expect a few seconds, and do not read it as an
  error. `pool_pre_ping` plus `pool_recycle` keep dead sockets out of the pool.
- *Transaction-mode pooling.* Server-side prepared statements cannot survive it,
  so the statement caches are disabled automatically when the host is a pooled
  endpoint.

### Database latency — read this before blaming the UI

**Pick a Neon region near the hospital. It is the single highest-impact
configuration choice in this system, and it is invisible until you measure it.**

Measured from India against the `us-east-2` (Ohio) project this was developed
on:

```
bare SELECT 1 round trip : min 858ms   median 870ms   max 886ms
one patient registration : 9 SQL statements, 11.3s inside the service,
                           ~30s for the whole HTTP request
```

Note what that first line is. `SELECT 1` does no work: 870ms is *entirely*
the packet's flight time to Ohio and back. Every statement in a transaction
pays it again, because each one is a separate round trip — so the cost of a
workflow is roughly `statements × RTT`, and nothing in the application code
changes that number.

What it does to CLAUDE.md §7b's acceptance gates:

| Round trip | Registration, counter to confirmation | §7b gate (30s) |
|---|---|---|
| ~870ms (`us-east-2`, measured from India) | **~30s** | fails |
| ~60ms (`ap-southeast-1`, Singapore — extrapolated) | ~2s | passes comfortably |
| ~6ms (local Postgres, measured) | **0.7–0.9s** | passes with 29s to spare |

The first and last rows are both measured, by the same Playwright test driving
the same build. The application is byte-identical between them; only the
database moved. That is why the e2e speed-gate test prints a hint about
database latency when it fails — a registration screen that is instant against
local Postgres and unusable against a distant one has a deployment problem,
not a design problem.

Neon offers `ap-southeast-1` (Singapore) and `ap-south-1` (Mumbai). Moving is a
connection-string change — it is still just Postgres — but it does mean
recreating the project and re-running migrations, so do it before there is data
worth keeping.

Two consequences the code carries because of this:

- `API_TIMEOUT_MS` defaults to 45 seconds, which is not a latency budget. A
  timeout part-way through a registration still creates the patient, while the
  counter sees an error and registers them again — a duplicate UHID, and a
  split medical history. Waiting is strictly better than duplicating.
- The e2e registration budget is overridable via
  `E2E_REGISTRATION_BUDGET_SECONDS` so a deliberately distant environment can
  still run the suite. Raise it only for that reason.

### Using local Postgres instead of Neon

Point both URLs at the container and restart:

```
DATABASE_URL="postgresql://hospital:hospital@postgres:5432/hospital"
DIRECT_URL="postgresql://hospital:hospital@postgres:5432/hospital"
```

TLS is skipped automatically for local hosts.

---

## Migrations

Every schema change is a migration — never edit the database by hand
(CLAUDE.md §11).

```bash
cd backend
alembic revision --autogenerate -m "add hospitals table"
alembic upgrade head
alembic downgrade -1
alembic current
```

Autogenerate only sees models imported by [`app/registry.py`](backend/app/registry.py).
**Add each new module's `models.py` there as you build it** — a missing import
produces an empty migration, and a later autogenerate will try to drop the
tables it cannot see.

Generated revisions are auto-formatted with ruff via Alembic post-write hooks.

---

## Authentication & RBAC

`POST /api/v1/auth/login` returns a short-lived access token (15 min) and a
refresh token (7 days). Send the access token as `Authorization: Bearer <token>`.

- **Refresh tokens rotate on every use.** Only a SHA-256 of each is stored, so a
  database read cannot mint a session.
- **Reuse detection.** Replaying a token that was already rotated means two
  parties hold it, so every session for that user is revoked. Replaying a token
  that was retired by *logout* is not treated as theft — that would be a
  self-inflicted denial of service.
- **Permissions are re-read from the database on every request**, not baked into
  the token. Revoking a role takes effect immediately rather than whenever the
  access token happens to expire.
- Passwords are Argon2id. Policy is length-first (12 chars minimum), with no
  composition rules — NIST SP 800-63B dropped those because they push people
  towards `Password1!`.
- Five failed logins lock an account for 15 minutes. "No such account" and
  "wrong password" are indistinguishable in both message and timing, so the
  login form cannot be used as a staff directory.

Roles and permissions are **rows, not code**: adding a role is an INSERT.
`app/modules/identity/rbac.py` holds the codes to import at call sites, and the
seed lives in a migration. Each future module seeds the permissions it enforces
in its own migration — an unenforced permission is a lie in an access review.

Fourteen system roles ship seeded: CLAUDE.md §8's twelve, plus two added the
same way a hospital would add its own — a row in `roles`, rows in
`role_permissions`, and a constant so call sites can name them. Neither required
a change to any authorisation check, which is the point of §8 calling the set
"extensible — data-driven, not hardcoded in logic".

- **`PATHOLOGIST`** — the lab's counterpart to `RADIOLOGIST`. `diagnostics`
  separates *entering* a result from *verifying* one, and without this role the
  lab signature had nowhere clinically correct to sit. `DOCTOR` keeps it too: a
  hospital with no resident pathologist still has to release results, and a
  permission model that makes that impossible is one the hospital routes around
  by sharing the administrator password.
- **`HOUSEKEEPING`** — owns `bed:clean`, the rung of the bed lifecycle that is
  somebody's actual job. Nursing keeps it as well, because a ward at 3am with no
  housekeeping shift on should not be stuck with an unreleasable bed. It holds
  *only* `ward:read`, `bed:read` and `bed:clean` — nothing that touches a
  patient record, because a porter who can read a diagnosis is a
  data-protection finding waiting to happen.

`tests/test_rbac_seed.py` compares the two halves in both directions: every
permission the code grants must be seeded, and every seeded grant must be one
the code enforces. That guard exists because the halves have drifted before —
Phase 6 found four roles named as order fulfillers that did not hold
`order:fulfil`, so the permission gate refused before the role check ran and the
visit stranded in `PENDING_CLEARANCE`. Nothing failed loudly; it simply did not
work.

### Permission caching

The role/permission join is cached in Redis for 60 seconds. The `users` row is
**not** cached, deliberately — so "is this account still allowed to act?" is
always answered from the database and a suspended clinician loses access on
their very next request, with no invalidation step that could be missed.

Invalidation has two paths, matching the two blast radii: one user's grants
change and their key is dropped; a *role* changes and a global generation
counter is bumped, retiring every entry in one write rather than trying to
enumerate everyone who held it. If Redis is unavailable the system falls back to
querying Postgres — slower, entirely correct. A cache that can stop admissions
would be worse than no cache.

## Patients

**Identity is `name + mobile`, not email.** Most patients have no email address
and many have no reliable ID document. Uniqueness is enforced on the *pair*, so
a household sharing one mobile — the norm — registers fine, while a second
`Sunita Devi` on the same number is refused as the duplicate it almost always
is. Names are matched on a normalised form: casefolded, punctuation and
honorifics stripped, so `Dr. Sunita Devi`, `  SUNITA   DEVI ` and `Sunita Devi`
are one person with one history.

Registration asks for **four fields** and no more — name, mobile, approximate
age, gender (CLAUDE.md §7b). Age is accepted as a number, because most patients
know roughly how old they are and not their date of birth; it is stored as a
derived birth date flagged `birth_date_is_estimated` so age arithmetic works
downstream and nobody later mistakes the guess for a documented fact.

On an exact clash the API returns **409 with the existing UHID**, so the client
can offer "Use existing record" in one click. For a genuine second person on the
same name and number — a father and son — reception qualifies the name
(`Ramesh Kumar S/O Suresh`) exactly as they already do on paper.

Other pieces:

- **UHID** — `CODE-YY-NNNNNN`, allocated from a per-hospital, per-year counter
  locked with `SELECT … FOR UPDATE`. Short enough to read aloud across a counter.
- **Duplicate detection** — trigram similarity (`pg_trgm`) over the normalised
  name, plus exact mobile matching. Surfaces family members and misspellings as
  warnings; only the exact identity pair blocks.
- **Universal search** — one box resolves UHID, mobile or name, and falls back
  to fuzzy matching when a precise search finds nothing.
- **Safety banner** — patient-level alerts (drug allergy, infectious
  precaution, fall risk) with criticals sorted first. Nurses and doctors can
  record them without finding an administrator.
- **Consent** — per purpose, with the method and the language it was explained
  in. Withdrawal stamps the existing row rather than deleting it, because
  "withdrawn" and "never given" are different facts.
- **Merge** — the duplicate is pointed at the survivor, never deleted, so old
  cards and historical records keep resolving. Alerts, consents and identifiers
  move across; blanks on the survivor are filled from the duplicate.

## Scheduling & the OPD queue

**One-click OPD** is the headline (CLAUDE.md §7b). `POST /api/v1/queue/quick-opd`
takes a patient and a doctor and returns everything the printed token slip
needs — appointment number, token, department, room, and how many people are
ahead — having created the appointment, checked the patient in and issued the
token in a single call. The alternative is four screens for the most repeated
action of the day.

**Two lifecycles, deliberately separate.** An *appointment* is a promise made in
advance; a *queue entry* is what is happening in the corridor right now. Tokens
get called out of order for emergencies, patients wander off and come back, and
a doctor running late changes nothing about a booking made three weeks ago.

Neither status is ever assigned directly. `scheduling/transitions.py` holds the
legal moves as data, one module ahead of where CLAUDE.md §14 requires it, so
`clinical` inherits a settled pattern for the Encounter machine. A refused move
returns 409 naming what *would* have been allowed — reception is being told "no"
by a screen and someone has to be able to explain why.

**Availability is a rule, not rows.** A weekly session ("Tuesdays 10:00–13:00")
plus dated exceptions for leave or an extra camp; slots are derived on demand.
Materialising every slot for every doctor for every future date would be
millions of rows that are mostly never booked, and moving a clinic by half an
hour would mean rewriting all of them.

**Queue order is stored, and reception can change it by hand.** Each token has
a `position` within its doctor's day. Check-in seeds it the way the order was
always computed — emergencies and senior citizens ahead of the general queue,
strictly first-come within a tier — so an untouched line reads exactly as
before. Giving a patient priority *requires a reason*: jumping in front of
people who have been waiting is a decision someone owns by name. From there,
reception can drag a row up or down its doctor's column on the board (pointer,
touch or keyboard); `POST /queue/{entry_id}/reorder {after_entry_id}` places
the token right after a named neighbour (`null` = top) and renumbers the line
1..n under a row lock, so two counters dragging at once serialise. The rank
drives the doctor's own list and "how many ahead"; it never changes the
priority tier, which stays a fact about the patient. Audited as
`queue.reorder`. Dragging is confined to one column — moving between doctors
is the Move dialog, because it reissues a token.

**No-show and cancellation are first-class.** Cancelling records who cancelled
and why (a clinic that cancels on a patient owes them a slot; a patient who
cancels does not) and clears any live token. Rescheduling leaves a walkable
chain rather than a cancellation plus an unrelated booking, so follow-up
reporting stays honest. `mark_overdue_no_shows` writes off appointments nobody
attended after a 90-minute grace period — and never touches a patient who has
checked in, because they are sitting in the corridor whatever the clock says.

**Reception sees one queue per doctor, and can move a patient between them.**
`GET /queue/board` returns a column per active doctor — waiting / with the
doctor / seen counts, the longest current wait, and the live rows — busiest
first, with empty columns included because "Dr Mehta has nobody" is the answer
reception is looking for. The registration form's doctor picker shows the same
counts next to each name. `POST /queue/{entry_id}/reassign` moves a `WAITING`
or `SKIPPED` token to another doctor: the row is updated in place (so the
appointment and encounter stay linked and `checked_in_at` is kept — a moved
patient never goes to the back of a queue they did not choose), a fresh token
is issued from the destination's series (the old number is never reissued),
and the appointment and the encounter follow. Refused once a doctor has called
the patient or started the consultation, and audited as `queue.reassign`.

**Reception can mark a patient as seen.** The "Seen" button on each row of the
reception board calls `POST /queue/{entry_id}/seen`: the token and the booking
close as COMPLETED (walking only transitions `transitions.py` already allows),
and the encounter moves `REGISTERED → IN_CONSULTATION` through the state
machine with the receptionist as actor and "Marked as seen by reception." as
the reason — otherwise the end-of-day auto-close would file a patient who was
seen as a NO_SHOW. It does not complete the visit: the doctor can still
document, and pending orders or bills still hold it open. No start time or
consultation duration is invented. The board holds the request for five
seconds behind an Undo, then lists the patient under "Seen today", struck
through. Audited as `queue.mark_seen`.

**The seam to `clinical`.** Checking a patient in opens the Encounter in the
same transaction, and `POST /queue/quick-opd` returns the `encounter_id`
alongside the token. `PatientCheckedIn` is still published for subscribers whose
failure is survivable — see "Why check-in calls `clinical` directly" below.

## The Encounter — the spine of the system

Every visit is one `Encounter` with an explicit status, and that status is the
single answer to "where is this patient right now?". It is what lets an OPD
patient be tracked without depending on them walking back to a reception desk.

```
REGISTERED → IN_CONSULTATION → AWAITING_RESULTS ⇄ PENDING_CLEARANCE → COMPLETED
     ↓              ↓                  ↓                 ↓
  CANCELLED      ADMITTED  ————————————————————————————→ COMPLETED
  NO_SHOW
                 …and from any active state, by an authorised role:
                 REFERRED_OUT · LAMA · DECEASED
```

**`state_machine.transition()` is the only writer of `status`.** Nothing else
assigns it — not the service, not a router, not a fixture. Each call validates
the move against a table (data, not `if` statements), stamps the outcome
columns, writes an `EncounterEvent` audit row, and fires the domain events other
modules subscribe to. That is CLAUDE.md §14's first invariant made structural
rather than remembered.

**The doctor never picks a status.** `POST /encounters/{id}/complete` is the
whole "I am finished" action, and where the visit goes is decided by what is
still open against it:

| Outstanding | Result |
|---|---|
| nothing | `COMPLETED` |
| something the doctor will review today | `AWAITING_RESULTS` |
| something to be cleared at a counter | `PENDING_CLEARANCE` |

`review_in_visit` on an order is what separates the last two, and it is a real
clinical distinction: "get this done and come straight back to me" versus "get
this done before your next visit". One checkbox, and reception stops guessing.

**Clearing the last item closes the visit.** When the pharmacist, lab tech or
cashier marks their own work done, the encounter follows on its own — they never
have to know that. `service.pending_items()` is the **single place** the "is
anything outstanding?" question is answered; `diagnostics` and `billing` extend
that one function rather than each growing their own idea of done.

**The auto-close net** (`app/workers/`) closes what staff forget, after
`ENCOUNTER_AUTO_CLOSE_HOURS`. Two details make it honest rather than merely
tidy: a visit that never reached a consultation closes as **NO_SHOW**, not
COMPLETED — recording a patient as seen because a job ran at midnight would be a
fabricated clinical fact — and a visit with pending items is **left open and
counted**, because those need a person and closing them would hide the work the
net exists to surface. `ADMITTED` is skipped entirely; an inpatient on day five
is not a stale OPD visit.

```bash
arq app.workers.main.WorkerSettings    # runs the sweep hourly, per tenant
```

**Death, referral and LAMA are records, not flags.** Each demands its metadata
before the transition is allowed — a death with no time, no certifying doctor
and no cause is not a record, it is a rumour — and each still emits
`EncounterClosed` with `requires_settlement=true`, because a deceased or
referred patient usually still has a bill that must be settled rather than
quietly disappearing. Recording a death also sets `patients.is_deceased` **in
the same transaction**, which is what makes "never notify a deceased patient"
an invariant instead of a convention.

**Ending a visit takes its token off the board.** `scheduling` subscribes to
`EncounterClosed` and `EncounterAdmitted` and closes the queue entry. Until it
did, nothing closed one: queue entries were only ever closed by *cancelling* or
*no-showing* the appointment, so every visit that actually finished — completed,
admitted, deceased, referred, absconded — left its token on the live board as
`WAITING`, permanently. The OPD queue never drained. It hid for a long time
because every screen reading the queue looks for a named patient rather than at
the whole list, and it became impossible to miss the day the terminal-outcome
screens landed and a patient recorded as deceased was still first in line for a
doctor.

The closing status comes from `was_seen` on the event — whether a doctor
started the consultation, the same definition `reporting` uses. Not from the
token's own status, which was the obvious-looking choice and is wrong: in the
real flow the doctor starts the consultation on the chart rather than on the
queue board, so a token often still reads `WAITING` when the visit closes.
Deriving from that would file every patient the doctor actually saw as "left
without being seen" and corrupt the one dashboard figure that means somebody
gave up and went home.

It is an event subscription rather than a call from `clinical`, which is the
boundary §2 draws: `clinical` does not know a queue exists, and after
extraction the handler is a message consumer with no code change. Same shape
`ipd` uses to free a bed on the same event.

**One call opens the doctor's screen.** `GET /encounters/{id}/chart` returns the
encounter, the persistent safety banner (allergies, infectious precautions,
critical flags), vitals, notes, diagnoses, orders and what is pending. Six
requests instead of one is most of the difference between hitting the §7b
60-second target and missing it. It is gated on `note:read` rather than
`encounter:read`: a cashier may legitimately look up a visit to bill it, and the
chart is a different question.

**Signed notes are never edited.** An amendment is a new note pointing at the
original, and only its author may sign one — an e-signature anyone can apply is
a timestamp with someone else's name on it.

**Fulfilment is gated twice.** `order:fulfil` says a user may clear orders;
`FULFILMENT_ROLES` says which *kind*. Without the second check the permission
would let a cashier mark a blood test resulted — obvious in hindsight, invisible
in a permission matrix.

### Why check-in calls `clinical` directly

`scheduling` publishes `PatientCheckedIn`, and `clinical` could have subscribed
to it. It does not. The event bus is deliberately fire-and-forget — a failing
handler is logged and swallowed so one bad subscriber cannot undo a committed
check-in — and that is exactly wrong for the chart itself. A patient holding a
token with no encounter behind it has nowhere to record vitals, no orders and no
bill, and nothing would have told anyone.

So the spine is created by an explicit service call inside the same
transaction, and the event stays published for subscribers whose failure is
survivable. Cross-module writes still go through `scheduling.service`
(`link_encounter`), never into another module's tables.

## Diagnostics — lab and radiology

**There is no second order table.** `clinical` owns the act of ordering and the
ledger that decides when a visit may close; `diagnostics` owns fulfilment and
hangs its report off the existing `orders` row. "What was asked for" has exactly
one answer.

The chain, and the one hop that matters:

```
doctor orders          POST /encounters/{id}/orders          (clinical)
lab accessions         POST /diagnostics/reports             -> order IN_PROGRESS
nurse bleeds           POST /diagnostics/specimens/{id}/collect
lab receives           POST /diagnostics/specimens/{id}/receive
results entered        POST /diagnostics/reports/{id}/results
verified & released    POST /diagnostics/reports/{id}/verify -> order COMPLETED
                                                             -> and if it was the
                                                                last pending item,
                                                                the VISIT CLOSES
```

That last step is a **direct call** into `clinical.service`, not an event —
same reasoning as check-in in Phase 4. A swallowed handler would leave a visit
open forever with its result already filed and nobody told.

**Reference ranges are snapshotted onto every result.** A hospital that revises
its haemoglobin band next year must not silently re-interpret a result issued
today: the band that was used is part of the result, not a lookup. Bands are
chosen by specificity — a band naming the patient's sex beats one naming an age
range, which beats the catch-all — and a band for another sex is never a
candidate. The catch-all exists because registration asks for four fields and a
birth date is not one of them.

**Panic values escalate on entry, not on signature.** A potassium of 7.2 does
not wait for someone to be free to verify; `CriticalResultFlagged` fires the
moment the number is typed, criticals sort to the top of every worklist, and the
resulting phone call is recorded on the report — NABH wants to know who was told
and when, and "called the ward" is not documentation.

**Entering and verifying are different people.** `result:enter` and
`result:verify` are separate permissions, a lab technician holds only the first,
and the service additionally refuses to let one person do both on the same
laboratory report. Radiology is exempt by design: the radiologist who dictates
the report is the one who signs it. Verification is gated twice — the permission
says you may sign reports, `VERIFICATION_ROLES` says which discipline.

**A released report is amended, never edited.** The original becomes `AMENDED`,
the replacement points back at it and carries the values across so one number is
corrected rather than twelve retyped. Somebody may already have treated the
patient on the strength of the first version, and erasing it would erase why.
Whoever may sign a report may also amend and cancel one: a correction that needs
an administrator is a correction that waits.

**A rejected sample does not make the work disappear.** Haemolysed, clotted,
unlabelled — the specimen is terminal with a reason, but the report and the
clinical order stay open, because the test was still asked for and a fresh tube
is collected against the same order.

**Cancelling the order withdraws the report.** This was a known gap through
Phase 5 — a lab order cancelled from the clinical side left its report on the
bench, and somebody had to spot the stale row by hand. Closing it needed the
event bus to carry a session, which Phase 6 added; `diagnostics.handlers`
now subscribes transactionally to `OrderCancelled`. A report that has already
been *signed* is left alone: somebody may have acted on that result, and
unpicking it is an amendment with a reason, not a subscriber's side effect.

## Billing

The module CLAUDE.md §13 step 7 asks for, shaped by one sentence in §7b:
*charges flow automatically from the event bus; reception reviews the invoice,
never rebuilds it.*

```
register patient        -> consultation fee captured   (EncounterOpened)
doctor orders a CBC     -> lab charge captured         (OrderPlaced)
counter adds a dressing -> manual charge
assemble draft          -> per-line GST, rounded total
issue                   -> lines frozen, INV-2627-000001
take payment (UPI)      -> receipt RCP-2627-000001
                        -> last balance cleared -> the VISIT closes itself
```

**Charge capture is idempotent and total.** Idempotent because a partial unique
index keys a charge to (module, type, source act), so a retried request or a
re-delivered event cannot bill a patient twice for one blood test. Total because
an act with no configured price is still captured — at zero, flagged
`needs_pricing`, on an exception worklist. A visible zero is recoverable;
a missing row is revenue the hospital never learns it lost. Together these are
what let billing subscribe *transactionally* without ever being the reason a
clinical order fails.

**Rate cards, not prices** (CLAUDE.md §9). A service does not have a price; it
has one per payer — cash, an insurer's negotiated list, a PMJAY or CGHS package.
Price lives on `service_prices`, keyed by (service, rate card).

**GST is per line, never on the total.** A hospital bill mixes an exempt
consultation with a taxable pharmacy item; a blended rate on the grand total is
wrong on both. Tax is computed after the discount (CGST Act §15(3)), split
CGST/SGST intra-state and IGST inter-state, and the payable total rounds to the
nearest rupee with the difference carried as its own `round_off` line so the
printout and the ledger both reconcile.

**Invoice numbers restart on the financial year**, not the calendar year —
unlike every other counter in the system. Rule 46 of the CGST Rules wants a
serial unique for a financial year, and India's runs April to March. Getting
that wrong is a compliance defect discovered retroactively, during an audit.

**An issued invoice is a document.** From `ISSUED` onwards its lines are frozen:
the patient is holding a printout, and a bill that changes afterwards is not
evidence of anything. A correction before any money arrives is a cancellation
(which releases the charges back onto the running bill); after money arrives it
is a reversal, then a cancellation. Payments are never edited — a mistake is a
second, reversed row, because the cash drawer reconciles against what happened.

**Taking money is separated from reducing it.** `payment:record` is a cashier's;
`charge:waive`, `invoice:write_off` and `payment:reverse` are not. A cashier who
could waive a charge and pocket the cash is a cashier nobody can audit. Every
one of those actions writes the amount and the reason to the audit log.

**The settlement flow** (CLAUDE.md §6). When a visit closes as `DECEASED`,
`REFERRED_OUT` or `LAMA`, loose charges are gathered onto a draft and every open
invoice is stamped with *why* it needs a person — it lands on
`GET /billing/invoices?awaiting_settlement=true`. Nothing is issued
automatically: a bill for a family that has just been bereaved is handed over by
somebody, not generated at them. The `SettlementRequired` event carries
`notify_patient=False` for a death, so the §14 "never message a deceased
patient" rule does not depend on a dispatcher remembering to check.

### The closure gate, extended

`clinical.service.pending_items` was built in Phase 4 as an extension seam. It
is now genuinely extended: `billing` registers a provider, so an unpaid bill
holds a visit in `PENDING_CLEARANCE` and the auto-close sweep reports it as
*held for staff* rather than tidying it away. Clearing the balance — by payment,
waiver or write-off — closes the visit, exactly as verifying the last lab report
does. `clinical` never imports `billing`; the provider is registered into a
registry from `app/registry.py`.

Set `BILLING_BLOCKS_ENCOUNTER_CLOSURE=false` for a hospital on corporate credit
accounts, where patients legitimately leave and finance invoices the employer
next month. The balance still shows on the counter's screen either way.

### The event bus has two channels

The change that made Phase 6 possible, and the one most likely to be undone by
someone tidying up later.

`subscribe` is **fire-and-forget**: handlers run concurrently, get no session,
and their failures are logged and swallowed. Right for a subscriber with its own
failure domain — a flaky WhatsApp gateway must not fail a patient registration.

`subscribe_transactional` gives the handler the publisher's session, runs it
inside the publisher's transaction, sequentially, and lets its exceptions
propagate. The reasoning matters because "swallow the error" *looks* like the
safe choice: a handler writing a row to the same database in the same
transaction has **no independent failure domain**. If that INSERT fails, the
publisher's own INSERT was going to fail too — so swallowing buys nothing and
loses a charge for care that was delivered, silently, because nobody gets an
error.

The contract this places on such a handler is strict: it must never fail for a
reason the *hospital* can cause. Missing rate card, unmapped service, no price —
none of these may raise. That is why `capture_charge` is total.

## Notifications

The module CLAUDE.md §13 step 8 asks for: a channel-agnostic dispatcher
(**WhatsApp → SMS → email**, §9), templates, triggers wired to domain events,
and a hard suppression rule for deceased patients.

```
appointment booked      -> confirmation           (AppointmentBooked)
report signed off       -> "your report is ready" (ReportReady)
panic value entered     -> alert to the DOCTOR    (CriticalResultFlagged)
sample rejected         -> "please come back"     (SpecimenRejected)
payment taken           -> receipt                (PaymentReceived)
visit closes COMPLETED  -> follow-up nudge        (EncounterClosed)
death recorded          -> everything above stops, permanently
```

Nothing in those workflows mentions messaging. Every line is a subscriber.

### Never message a deceased patient — enforced three times

CLAUDE.md §14 names this an invariant to guard actively. The naive reading is
"check `is_deceased` before sending", and that check exists — but on its own it
is not enough, for the reason that actually happens:

```
Monday     a visit closes; a follow-up reminder is queued
Wednesday  the patient dies
Friday     the retry sweep picks the message up and sends it
```

A check at enqueue passes on Monday and is irrelevant by Friday. So:

1. **`enqueue`** refuses and records the message as `SUPPRESSED` with its reason
   — an auditor asks *why* nothing was sent, and an absent row does not answer.
2. **`dispatch`** re-checks immediately before the first gateway call, reading
   the patient afresh. This is the layer that catches the case above.
3. **The database** refuses the `notification_attempts` INSERT outright, via a
   `SECURITY DEFINER` trigger that fails closed. Same philosophy as row-level
   security in §3: the application is expected to be correct, and the database
   makes incorrectness impossible. It covers callers that do not exist yet.

Recording a death also **cancels everything already queued** for that patient.
Suppression would stop those at dispatch anyway, but a pending "book your
follow-up" row against a deceased patient is a loaded gun for the next person
who writes a bulk-send script. The safest row is one that does not exist.

The distinction that makes all of this workable is `recipient_type`. A message
*about* a deceased patient addressed to *staff* — "this bill still has to be
settled with the family", which §6 requires — goes out normally. The same fact
sent to the family's mobile never does. Suppression keys on
`recipient_type = PATIENT`, and the guardian's phone counts as patient-directed.

**A death suppression can never be lifted.** The refusal is in the service, not
in RBAC, deliberately: permissions are data-driven rows an administrator can
edit (§8), which is a feature everywhere except here. There is no permission
that lifts a death because there is no code path that does.

`/messages/blocked` is where that stops being a claim in a README. Until it
existed nobody in the hospital could see the rule working, and an invariant
nobody can inspect is one people quietly stop believing in and then work
around. A death row on that screen carries **no Release button at all** — not a
disabled one, because a control that can never be used is still a control
somebody asks why they cannot use — and it says why in a sentence. The reason
picker for recording a block leaves `DECEASED` out for the same reason the
schema refuses it: two ways to record a death would mean two answers to whether
somebody is dead.

### Templates: shipped copy, hospital overrides

English and Hindi copy for every trigger ships in
`notifications/templates.py`. A `notification_templates` row **overrides** it.
Nothing is seeded per tenant, which is the point: a hospital onboarded next year
sends working messages on day one instead of bare fallback text nobody notices.
`preferred_language` on the patient picks the language, falling back to English.

`needs_template` therefore means something sharp — neither an override nor
shipped copy exists — and the flag is the direct analogue of billing's
`needs_pricing`. Rendering, like `capture_charge`, is **total**: a missing
placeholder or an untranslated code degrades the message and raises a flag; it
never blocks the hospital.

Substitution is `{{double_braces}}` against a flat string dictionary, not
`str.format`. Template bodies are hospital-authored data editable through the
admin API, and `format` traverses attributes —
`{patient.__class__.__init__.__globals__[SECRET_KEY]}` is a published attack,
and there is a test asserting it renders as literal text.

### The ladder, and the gateway seam

Channels are tried in `NOTIFICATION_CHANNEL_PRIORITY` order. A channel the
recipient has no address for, or the provider cannot serve, is **skipped rather
than failed** — a patient with no email should not accumulate an email failure
on every message they are ever sent. Each rung tried is a
`notification_attempts` row, because "we sent it" is not an answer to a patient
who received nothing, and "WhatsApp rejected it at 14:31, the SMS was accepted
at 14:31" is.

`NOTIFICATION_GATEWAY` selects the adapter. `console` (the default) logs the
rendered message and reports success — no third-party account, no cost, and the
whole pipeline is still exercised end to end. A real adapter is a class in
`notifications/gateways.py` and one environment variable; that integration is
procurement rather than programming (a Meta WABA account with approved
templates, and DLT registration for Indian SMS), and it should not have blocked
the hospital's own logic from being finished and tested.

Gateways **return** failure rather than raising it: a provider being down is an
expected Tuesday, and WhatsApp refusing is the normal path to SMS succeeding.
An adapter that raises anyway is caught, so one broken provider cannot end the
walk before the next channel is tried.

### Delivery: fire-and-forget, sent inline

Outbound handlers subscribe with `subscribe`, open their own session, and send
in the same call. A flaky gateway therefore cannot fail a clinical write. Two
costs come with that and are accepted rather than hidden:

- a handler exception is logged and swallowed *by design*, so a message can be
  lost — mitigated by writing the row before touching a gateway, after which
  `retry_failed` can sweep it up;
- the publisher's transaction has not committed yet, so a rollback means a
  message went out about something that did not happen. Handlers therefore
  render from the **event payload**, never by re-reading the row the publisher
  just wrote.

The alternative — writing the row transactionally and dispatching from the
worker — removes both at the cost of a second moving part. `enqueue` and
`dispatch` are already separate functions, so that change stays small if the
phantom-message case ever bites.

**`suppress_on_death` is the one exception, and subscribes transactionally.** It
calls no gateway; it writes one row to the same database in the same transaction
as the death record, so it has no independent failure domain — the exact test
`core/events.py` sets for which channel to use. What it writes *is* the
invariant, and a swallowed exception there would lose the rule that stops every
future message.

### Retry

The worker walks failed messages down the ladder again, up to
`NOTIFICATION_MAX_ATTEMPTS`. That budget counts **complete walks**, not
individual rungs: one walk of a three-channel ladder already tries everything,
so budgeting in rungs would retire a message after a single provider outage.
Past the budget, the honest answer is that somebody picks up a phone —
retrying a number that does not exist costs money and burns provider reputation.

This is not a transactional outbox. Those rows already exist and already failed,
so it is resilience on an existing record rather than a second delivery path.
What makes it safe for a message queued days ago to go out now is that dispatch
re-checks suppression first.

## Inpatient care — wards, beds, the chart, the summary

The module that turns an `ADMITTED` encounter into a stay: physical capacity,
who is in which bed and when, what drugs are due and whether they were given,
and the document a patient cannot safely leave without.

Two of the six things CLAUDE.md §13 step 9 names are deliberately **not** tables
here. Nursing notes are `clinical.ClinicalNote` with `NoteType.NURSING`, and
ward rounds are the same model with `PROGRESS` — an admission *is* an encounter,
and a parallel set of note tables would mean the record lived in two places and
the discharge summary had to read both. What `ipd` adds for rounds is the
worklist, and a worklist is a query.

### The bed lifecycle, and the rung everybody leaves out

```
AVAILABLE ──assign──> OCCUPIED ──release──> CLEANING ──cleaned──> AVAILABLE
    │                     │                     │
    └──reserve──> RESERVED└──condemn──> OUT_OF_SERVICE ──> CLEANING
```

`OCCUPIED → AVAILABLE` is **not** a legal transition, and the refusal says so in
words a nursing station can act on. This is the whole point of CLAUDE.md §7b's
bed cleaning lifecycle: a board that frees the bed the instant a patient leaves
is a board that sends the next patient to an unmade bed at 2am. Making the
shortcut unrepresentable is cheaper than remembering not to take it.

`beds.status` is never assigned outside `_move_bed`, on the same principle that
`Encounter.status` is never assigned outside `state_machine.transition`.

Occupancy excludes out-of-service beds from the denominator — a ward closed for
renovation has not made the hospital 100% full.

### Bed occupancy is a separate table from the bed

`beds.status` says whether a bed can be filled right now. `bed_assignments` says
who was in it, from when to when. The history answers "which bed was this
patient in on day three", which infection control asks in earnest and a patient
disputing a private-room charge asks in anger. It is also what the room tariff
is computed from: an ICU night and a ward night are different money, and a
single `bed_id` on the admission would bill the whole stay at whichever bed they
happened to end in.

Two partial unique indexes are the hard guarantee — one live assignment per bed,
one per admission — with a friendly `ConflictError` that normally gets there
first.

### The medication chart materialises doses in advance

A `MedicationOrder` of "1 g TDS" generates `MedicationAdministration` rows at
the due times **before** anyone gives anything. That is what makes this a
medication administration record rather than a log of things that happened: the
clinically significant event is a dose that was *not* given, and an absence
cannot be a row in a table that only records administrations.

A nightly sweep turns silence into a record — a slot past its window with
nothing written becomes `MISSED` with `auto_missed` set. The flag matters: a
nurse recording a miss is documented care, the sweep noticing one is a process
failure worth escalating. `IPD_DOSE_GRACE_HOURS` (default 2) is the tolerance,
long enough that a busy drug round running late is not defamed.

The horizon is short on purpose (`IPD_CHART_HORIZON_DAYS`, default 2). A chart
built weeks ahead is full of doses for orders that will be stopped tomorrow, and
every one would age into a false "missed".

Stopping a drug **cancels** future slots rather than deleting them: "scheduled
then stopped" and "never prescribed" are different facts. Discharge stops the
chart but leaves an unrecorded *past* dose outstanding — going home at noon does
not unmake a missed 6am antibiotic.

RBAC keeps the second pair of eyes: `medication:prescribe` is a doctor's,
`medication:administer` is a nurse's, and neither role holds both.

### Bed-days bill themselves

A nightly sweep emits one `BedDayAccrued` per admission per night; `billing`
subscribes and captures a `ROOM` charge. Nobody types a room rate, and the
running bill is live — a family asking on day four what the stay has cost gets
an answer.

Idempotency comes from `accrual_key`, a UUIDv5 of `(admission_id, date)`. A
sweep that restarts mid-pass or runs twice after a deploy keys onto the same
charge instead of billing the patient twice for one bed. It needs no table to
remember what was already billed.

The class charged for a night is the one the patient was in at the **end** of
it, which is the common convention and does not reward a late transfer.

### The discharge summary is compiled, never invented

`summary.py` assembles a draft from rows the hospital already has — diagnoses
and notes from `clinical`, reports from `diagnostics`, drugs from the chart —
so the consultant's surface is *review and sign*, not *write*. It is compiled
automatically when discharge is initiated, so by the time anybody asks for it, a
draft is waiting to be edited.

Where nothing was recorded, the section comes out **empty**. Filling it with
"unremarkable" would produce a document that reads finished while saying
something nobody wrote, and the blank version is the one that gets noticed by
the doctor signing it. `compiled["_provenance"]` records what the compiler had
to work with, so "blank because nobody wrote a note?" stays answerable.

The course of the stay carries author names and dates: a consultant signing is
putting their name to other people's observations. Discharge medications are
only what was still running — a course that finished on day three is treatment
given, not something to keep taking.

Signing freezes the document and snapshots the signatory's name and registration
number, and needs both `summary:sign` **and** a clinical role.

### Death and LAMA: one way to record them, not two

A death or a self-discharge on the ward is **not** entered through the discharge
endpoint — the schema refuses those discharge types outright. Both are recorded
against the Encounter by `clinical.record_death` / `record_lama`, which capture
the metadata CLAUDE.md §6 requires: the certifying doctor, the cause, whether
the LAMA form was signed.

`ipd` then *follows*, via a transactional subscriber on `EncounterClosed`: it
frees the bed, stops the chart, closes the admission and compiles the summary. A
second, thinner path through an IPD endpoint would inevitably be the one used on
a busy night, and it would have no certifying doctor attached.

`ipd` registers **no** pending-item provider. The obvious candidate — "the bed
has not been released" — could never fire, because the closure gate is consulted
by `complete_consultation` and the auto-close sweep, and neither runs against an
`ADMITTED` encounter. A provider that cannot fire is worse than none, because it
reads like a guarantee. Unsigned summaries surface as a worklist instead:
`GET /ipd/summaries?status=DRAFT`.

### The admission desk — OPD hands a patient to IPD

Reception marks a patient seen, then **Send to admission** on that seen row
(`POST /api/v1/ipd/admission-requests`, `admission:request`). The patient lands
on the **Admission desk** (`/admissions`, `admission:desk`), oldest first.
**Admit** opens the same bed picker the consultation screen uses and calls
`POST /ipd/admission-requests/{id}/admit` (needs `admission:desk` *and*
`admission:create`), which runs the ordinary `admit_patient` — bed, admission
number, encounter → `ADMITTED` — and the IPD flow carries on from the
admission's page. **Turn away** (`/cancel`) needs a reason.

- **A waiting patient holds their OPD visit open.** `ipd` registers a pending
  provider, so the doctor's "complete visit" leaves it in `PENDING_CLEARANCE`
  and the night's auto-close holds it; turning the request away lets it close.
- **A visit the doctor already closed still admits:** the stay opens a new IPD
  visit instead of reopening a terminal one. IPD visits carry no consultation
  charge, so nothing is billed twice.
- **Any admission clears the request** — including a doctor admitting straight
  from the consultation screen — so nobody stays on the desk's list in a bed.
- **Roles.** `ADMISSION_DESK` is a new system role (demo login
  `admission@demo.hospital`). Reception and doctors gain only
  `admission:request`; reception keeps `admission:create` exactly as before.
  Hospital and platform admins hold everything. A hospital where one person
  runs both counters gives them both roles.
- One pending request per patient, enforced by a partial unique index as well
  as the service check. Every send, admit and turn-away is audited.

## Reporting — KPIs, and a module that stores nothing

CLAUDE.md §13 step 10: footfall, occupancy, revenue, ALOS and follow-up
compliance, plus §7b's queue analytics and doctor delay alerts.

`reporting` owns **no tables**. A KPI is a question about facts that already
exist, and a reporting module keeping its own copy of them is a module that can
disagree with the system it reports on — the first time the dashboard says
₹4,80,000 and the invoice ledger says ₹4,79,200, nobody trusts either number
again.

### The boundary, traded for a different one

§2 says a module never reaches into another's tables. §5 says *"no raw SQL
except in `reporting` read-only views where performance demands it."* Those pull
opposite ways, and revenue for a quarter genuinely cannot be computed by paging
through `billing.service.list_charges`.

So §5's carve-out is taken, and the §2 boundary is replaced by one just as
explicit: **every cross-module read goes through a named view**, declared in
this module's migration and listed in `app/modules/reporting/models.py`. Never
an inline join at a call site. That gives one place to fix when `billing`
renames a column, a greppable dependency list, and — since reporting is an
extraction candidate — the natural API of a read replica later.

### Two properties that are not optional

**`security_invoker = true` on every view.** A Postgres view runs with the
*owner's* privileges by default, and the owner here is the migration role, which
carries `BYPASSRLS` for the same reason the application does not. A view without
this flag reads its base tables with row-level security switched off, and one
unqualified `SELECT` returns every hospital's data. Nothing about that failure
is visible in the application code — no missing `WHERE`, no wrong join.
`tests/test_reporting_isolation.py` asserts it two ways: structurally against
`pg_class`, so a view added later cannot forget it even while it holds no rows,
and behaviourally by querying every view as a bound tenant.

**Local dates, not UTC dates.** Every view exposes
`(ts AT TIME ZONE h.timezone)::date`. For `Asia/Kolkata` the hospital's day ends
at 18:30 UTC, so grouping by UTC date files every evening's last five and a half
hours under tomorrow — footfall wrong daily, by a margin that looks entirely
plausible on a chart.

Reporting was careful about this from the start; `scheduling` was not, and
building the dashboard is what found it. `check_in` stored `queue_date =
utc_now().date()`, so from 18:30 IST every patient who checked in was filed
under yesterday: invisible to the live queue board, continuing yesterday's token
series instead of starting today's, and — once the dashboard existed to show it
— an evening OPD that reported nobody waiting. `local_today` now lives in
`tenancy.service`, because what day it is for a hospital is a fact about the
hospital, and both modules call the same one. Any module that reaches for
`utc_now().date()` on tenant data is making the same mistake.

### Definitions worth arguing about

These are the lines where a dashboard becomes trustworthy or becomes decoration:

- **"Seen" means a doctor started the consultation**, not that the visit closed.
  Deriving it from status would let the auto-close sweep manufacture patients
  nobody examined.
- **ALOS floors at one day** and counts discharged stays only. A same-day case
  still occupied a bed; and including patients still in one would drag the
  average down every morning as their stay lengthens.
- **Earned, collected and outstanding are three numbers, never summed.** Charges
  raised is not money in the bank, and collections include payments against
  invoices raised months ago. Outstanding is deliberately *not* windowed — the
  oldest debt is the one that matters, and a date filter would hide it.
- **Follow-ups not yet due are `pending`, not failures.** Counting them as
  misses would make the rate depend on when the report was run.
- **Waived charges stay in the view** with `billable_amount` zeroed, because
  "how much did we waive last month" is a question management asks.
- **Empty days are zero-filled; averages with no denominator are `null`.** A
  chart that drops empty days draws a line through the gap, and an average wait
  of "0 minutes" when nobody was seen is a number somebody quotes in a meeting.

### Three permissions, not one

`report:operational` (volumes, occupancy, queue), `report:clinical` (outcomes,
follow-up) and `report:revenue` (money). A single `report:read` would mean that
giving a nurse the queue screen also hands her the hospital's collections.
`GET /reports/dashboard` composes whatever the caller's permissions allow into
one round trip, with unavailable sections returned as `null` rather than omitted
so the frontend layout does not reflow by role.

Plain views rather than materialised ones: materialised would need a refresh
schedule, a staleness policy and an answer to "the dashboard is an hour behind
during a board meeting". Revisit when a single tenant's `charges` passes a few
million rows — at which point the honest move is a rollup table refreshed by the
existing ARQ worker, not a bigger query.

## Audit log

Every sensitive action writes a row: who, what, when, which tenant, and a JSONB
before/after. `UPDATE` and `DELETE` on `audit_logs` are refused by a database
trigger, and so is `TRUNCATE` — row-level triggers do not fire for it, which
would otherwise leave "wipe the whole trail in one statement" available.

Audit rows are written in the same transaction as the action they describe. An
entry that can be lost while the action commits is worse than no entry at all,
which is also why this is a direct call rather than a domain event.

`actor_user_id` is `ON DELETE RESTRICT`, and that is the only referential action
consistent with the trigger. It was `SET NULL` until it was corrected, which was
unreachable: `SET NULL` is implemented as an **UPDATE** of the audit row, and
the trigger refuses UPDATE. Deleting a staff account would not have anonymised
the trail — it would have failed with `audit_logs is append-only` during
offboarding. RESTRICT states the real rule: an account that has done anything
cannot be erased, because an audit trail whose actor can be deleted is not an
audit trail. Offboarding sets `users.deleted_at`, and the snapshotted
`actor_email` keeps the history readable afterwards.

## Frontend

Next.js 16 (App Router) + React 19 + Tailwind v4 + shadcn/ui, in `frontend/`.

```bash
cd frontend
npm install
npm run dev                 # http://localhost:3000

npm run verify              # codegen check + lint + typecheck + build
npm run test:e2e            # Playwright, against a real backend
```

Sign in with an account from `python backend/scripts/seed_demo.py`, which
creates one hospital and one of each front-line role.

### The browser never holds a token

This is the load-bearing decision, and everything else follows from it.

The JWTs live in a single **httpOnly** cookie. The browser cannot read them —
not from `document.cookie`, not from `localStorage`, not from a JS variable.
Every API call goes to this app's own `/bff/[...path]` route, which attaches
the token server-side and forwards to FastAPI.

```
browser ──/bff/patients──▶ Next.js (attaches token from httpOnly cookie)
                             │
                             └──/api/v1/patients──▶ FastAPI
```

The cost is one extra hop, a couple of milliseconds when both run in the same
region. What it buys: an XSS in any of the ~390 npm packages still cannot
exfiltrate a credential that keeps working after the tab closes. For a system
under the DPDP Act that is the difference between an incident and a reportable
breach. Two e2e tests assert it — one proves no JWT is reachable from page
JavaScript, the other proves no request ever bypasses the proxy.

Token rotation lives in exactly one place. The backend revokes an entire token
family when a refresh token is reused (a theft signal), so two tabs refreshing
at once would look like an attack and sign the user out mid-consultation.
`src/lib/api/refresh.ts` collapses concurrent refreshes into one call, and
`src/proxy.ts` refreshes *ahead of* a navigation because server components
cannot set cookies during a render.

> Next.js 16 renamed Middleware to **Proxy**. It is `src/proxy.ts`; the
> mechanism is unchanged.

### API types are generated, not written

```bash
npm run codegen         # regenerate from the backend
npm run codegen:check   # fails if what is committed is stale
```

248 schemas and 111 permission codes come out of the backend itself —
`openapi.d.ts` from FastAPI's OpenAPI document, `rbac.generated.ts` from each
module's `rbac.py`. Neither needs a running server. Hand-written frontend types
are a second declaration of one truth, and second declarations drift: a field
goes optional in Pydantic, the TypeScript still says required, and nobody finds
out until a screen renders `undefined` in front of a patient. `codegen:check`
belongs in CI, alongside `alembic check`.

Permissions deserve their own note: they are rows, not schema, so they never
appear in OpenAPI. Generating the union anyway means a mistyped
`"encouter:complete"` is a build error rather than a button that silently never
appears for anyone.

### What is built

| Screen | Route | Notes |
|---|---|---|
| Sign in | `/login` | server action, forced password change supported |
| Reception | `/reception` | live queue, 15s refresh |
| Register | `/reception/register` | 4 fields, duplicate detection, one-click OPD |
| Doctor's list | `/doctor` | the signed-in doctor's own queue |
| Consultation | `/consultation/[id]` | whole chart in one request |
| Queue | `/queue` | hospital-wide, for nursing; one-click vitals per row |
| Patient | `/patients/[id]` | identity, alerts, visit history |
| Patient card | `/patients/[id]/card` | printable card with the lookup QR |
| Diagnostics bench | `/lab` | one worklist, lab and radiology interleaved |
| Report | `/lab/reports/[id]` | result entry, server-side flagging, verify |
| Cash counter | `/billing` | visits with money outstanding, oldest debt first |
| Visit account | `/billing/[encounterId]` | assemble, issue, take payment |
| Bed board | `/wards` | every ward, every bed, who is in it |
| Admission | `/admissions/[id]` | transfer, fit-for-discharge, discharge |
| Medication chart | `/admissions/[id]/chart` | prescribe, stop, sign for doses |
| Discharge summary | `/admissions/[id]/summary` | compiled, edited, signed |
| Drug round | `/rounds` | every dose due across the wards |
| Patient index | `/patients` | browsable, paged, searchable |
| Messages | `/messages` | the outbox, and every channel tried per message |
| Blocked | `/messages/blocked` | who the system will not message, and why |
| Reports | `/reports` | one dashboard, shaped by permission |
| Administration | `/admin` | hub, permission-filtered |
| — staff | `/admin/staff` | accounts, roles, deactivation |
| — doctors | `/admin/doctors` | clinic profiles |
| — departments | `/admin/departments` | |
| — services | `/admin/services` | rate cards, services, prices |
| — catalogue | `/admin/catalogue` | investigations and reference ranges |
| — wards | `/admin/wards` | wards and beds |
| — templates | `/admin/templates` | message wording, with a preview |

Every backend module now has a screen, so `nav-items.ts`'s `PLANNED` list is
empty. Keep the mechanism: through the build it held modules whose API was
finished and whose screens were not, because a sidebar entry that leads to a 404
is worse than a missing one — staff stop trusting the navigation to tell them
what the software can do.

Every backend capability now has a screen, including CLAUDE.md §6's three
terminal outcomes — see below.

### Scanning a card

CLAUDE.md §7b's QR-based patient lookup. **The QR contains the UHID as plain
text** — `DEMO-26-000042`, exactly the string printed in ink beside it.

That one choice is what makes the feature nearly free, because **a counter
barcode scanner is a keyboard**: it types the payload into whatever has focus
and presses Enter. So a scan into the existing search box needs no client code
at all — press F3, scan, open. Measured at 0.3 s against fourteen typed
characters.

It is also what makes it safe. The QR adds no exposure a lost card did not
already have, and it is not a credential: scanning produces a *search term*,
and every route behind it still demands a JWT and is scoped by row-level
security to the caller's own hospital. An opaque token would have put a new
durable secret on a losable piece of paper, plus a column and a backfill, to
buy nothing. A URL would land a phone camera on a login page and write the
hostname into camera history.

`GET /patients/by-uhid/{uhid}` is the resolution point — exact rather than the
search box's substring match, since a scanned identifier should mean only
itself. It is declared **above** `/{patient_id}`: FastAPI matches in order, and
a literal segment registered after a UUID parameter is unreachable. Audited as
`patient.lookup.uhid`, including when it finds nothing — an unrecognised card
is exactly the event somebody investigates later.

A scan also answers the question a counter needs before anything else:
**is this patient already in the middle of a visit?** Registering somebody
twice is the error a card makes easy — the patient is sitting in a corridor
with a token while reception opens them a second visit, and a split visit is a
split bill and a chart in two halves. So an open visit lands in two places
depending on what the caller can do: on the patient's own row as a status,
always, and as a separate hit that opens the chart only for callers holding
`note:read`. Reception does not hold it, and a hit that leads to a 403 is worse
than a missing one. Either way the record stays the first hit, so pressing
Enter is safe for whoever is holding the scanner.

**Scan anywhere.** `useScanner` recognises a scanner by its typing speed, so
there is no box to focus first — scan from any screen and the patient opens.
The benefit is small and the risk of a global key handler is not, so there are
two independent guards in `lib/scanner.ts`, either of which would be enough on
its own. **Where:** completely inert while any text control has focus, which
makes "it ate part of a clinical note" impossible rather than unlikely.
**How fast:** a median inter-key gap under 30 ms, against roughly 100 ms for
sustained professional typing — an order of magnitude, so no tuning. Keystrokes
are suppressed once a burst is recognised and *before* its terminator arrives,
because an `Enter` reaching a focused button is a click nobody asked for.

Fourteen unit tests pin every threshold with a passing and a failing case, and
they run without a browser — Playwright as the runner rather than a second test
framework for one pure module.

**Camera scan.** The third way in, for a device with no scanner attached —
a nurse doing a ward round with a tablet. `CameraScanButton` on `/reception`
and `/queue` uses the browser's own `BarcodeDetector`, so it ships **no decoder
library**: the usual choice costs 40–90 kB on every page that imports it and
does nothing the platform does not already do natively.

The price is that the API is not everywhere, and the button **renders nothing
where it cannot work** — which includes Windows desktop Chrome, every Firefox,
and any deployment without TLS, since `getUserMedia` does not exist outside a
secure context. That is the right failure: a counter PC has a USB scanner and
needs none of this, and a button that opens a dialog to explain its own
impossibility teaches staff to ignore buttons. Camera denial, a missing camera
and any other failure each get their own sentence, and all three end by saying
the counter scanner and typing the UHID still work. There is no dead end.

Both scan paths resolve through one `useResolveScan`, and one `normaliseUhid`
decides what counts as a card, so the keyboard and the camera cannot drift into
disagreeing about the same piece of paper.

Three things decide whether a printed code actually scans, and all three are in
`PatientQr`: a four-module quiet zone (the most common omission — without it a
scanner cannot find the code's edge), `shape-rendering="crispEdges"` (anti-
aliasing blurs module boundaries on a 203 dpi thermal head), and a physical
size set in millimetres with a 20 mm floor. **None of that is provable in an
automated test.** Printing one and scanning it is the only check that counts,
and it is the single step on this feature's critical path that is not code —
which is equally true of the camera path: headless Chromium has neither the API
nor a lens, so its tests stub exactly those two platform pieces and genuinely
exercise everything between them, including releasing the camera afterwards.

### How a visit ended, when it did not end well

Death, referral out and LAMA were the last part of the state machine with no
screen — recordable only by an API call, which in practice means not recordable.
A hospital that cannot record a death in its own system keeps a paper register
next to it, and then the two disagree about who is alive.

They sit behind **one subdued control** on the consultation screen rather than
three buttons beside "Complete visit". These are rare, irreversible and
consequential, and three peers would put "record a death" one mis-click from
the button a doctor presses thirty times a day. Choosing which outcome happened
is a deliberate act inside the dialog.

The friction after that point is the metadata `REQUIRED_METADATA` already
demands, and none of it is ceremony: a death needs a time, a **named**
certifying doctor and a cause; a referral needs a destination and a reason the
receiving hospital will read before the patient arrives; a LAMA records whether
the form was signed, either way, because an unsigned one is the hospital's
exposure and a field that is quietly always "yes" helps nobody the day it is
examined. The screen disables the button until they are there rather than
letting the doctor press it and be refused.

Two details worth keeping:

- **`death_certified_by_name`.** `EncounterRead` carried only the id, which
  makes the record unreadable by everyone who needs it — the records officer,
  the registrar, the family. The router attaches the name through
  `identity.service`, and only on the rare encounters that have a certifier, so
  the busiest read in the system pays nothing for it.
- **The time of death is converted, not passed through.** A `datetime-local`
  field holds local wall time with no zone; sending it raw would have it read
  as UTC and put an IST death five and a half hours late on a certificate.
  Exactly the bug the queue's `queue_date` had.

### The outbox answers a sentence

"I never got the message." It is said out loud, at the counter, by a named
person — which is why `/messages` leads every row with the patient's name and
UHID rather than an id, and why `NotificationSummary` and `SuppressionRead`
gained those fields to make it possible. The same bulk-lookup shape as the
queue, the bench, the counter and the ward round: one patient query per page,
pinned by a statement-counting test.

Opening a row shows the **attempt ladder** — WhatsApp, then SMS, then email
(§9), each with what the gateway said and when. "We sent it" is not an answer
to somebody who received nothing; "WhatsApp rejected the number at 14:31, the
SMS was accepted at 14:31" is, and it tells reception which of the two numbers
on the record is wrong.

The filters are the three questions actually asked, and the third is the one
most systems cannot answer: **a message not sent is not a message that failed.**
`SUPPRESSED` and `FAILED` get different colours and different words, because
"we chose not to" and "we tried and could not" answer different questions and
an auditor asks the first.

### Message wording, and the syntax that must be shown correctly

`/admin/templates` is where a hospital overrides the shipped English copy or
adds a language. An empty list is **not** an error state and the screen says so:
messages still go out in the shipped wording, so an administrator who never
opens this screen still has a working hospital.

Preview matters more than it sounds. A placeholder the renderer does not
recognise is substituted with *nothing* — the message goes out with a hole in
it rather than with visible gibberish — so the failure is silent all the way to
the patient's phone. The screen renders the saved template server-side and
reports what nothing will fill. It also shows the expected placeholders in the
exact `{{double brace}}` syntax `PLACEHOLDER_PATTERN` matches; the first version
of that hint showed single braces, which would have taught every administrator
to write templates that substitute nothing.

### One dashboard, six shapes

`/reports` is a single request. `GET /reports/dashboard` returns every section
the caller's permissions allow and `null` for the rest, so a nurse and a cashier
open the same link and get different screens without the frontend deciding
anything. That is CLAUDE.md §7b's role-based dashboards falling out of RBAC
rather than being a second thing to maintain — and it is why the permission is
split three ways (§8): a single `report:read` would have meant that giving a
nurse the bed board also handed her the hospital's monthly collections.

| Section | Needs | What it answers |
|---|---|---|
| Today | `report:operational` | who is waiting, and which clinic is behind |
| Patients | `report:operational` | footfall by day, department and doctor |
| Beds | `report:operational` | occupancy now, per ward |
| Inpatients | `report:clinical` | admissions, ALOS, how stays ended |
| Follow-up | `report:clinical` | whether patients advised to return did |
| Money | `report:revenue` | earned, collected, outstanding |

Two rules the screen inherits from the API and does not get to soften:

**A number that does not exist is not zero.** The schemas return `None` for
every average with no denominator, and the dashboard renders those as an em
dash. An average length of stay of "0.0 days" when nobody was discharged is a
figure somebody repeats in a meeting.

**Earned, collected and outstanding are never summed.** They are three
different questions — what was charged, what money arrived, what is owed right
now — and outstanding is not windowed at all, because a debt is a fact about
today rather than about a date range. The screen shows three tiles, each with
its own sentence, and offers no total.

Only one panel polls. The dashboard aggregates are expensive — a ninety-day
footfall query, an ALOS over every discharge — and re-running all of that every
thirty seconds to move a waiting count would be a lot of database work for one
number. The report body is fetched once per window; today's queue polls
`/reports/queue`, which reads a single day, and shares its cache with the delay
alert on reception's screen so the two can never disagree.

### The delay alert is on reception's screen, not the dashboard

§7b asks for doctor delay alerts *to reception* when a clinic runs behind. It
sits above the queue on `/reception` and **renders nothing at all when nothing
is wrong** — no "all clinics on time" strip. A banner that is always present is
a banner people stop seeing, and this one has to be noticed on the day it
appears. What counts as behind is `REPORTING_QUEUE_DELAY_MINUTES`, so the
definition belongs to the hospital rather than to a component.

### Recharts, loaded lazily

CLAUDE.md §4 fixes Recharts and also says to lazy-load charts. Both apply here:
the chart is behind `next/dynamic`, in its own 362 KB chunk that no other route
pulls in. Everything else on the dashboard — ward occupancy, revenue by
category, doctor workload — is a *proportion*, and a proportion reads fine as a
labelled bar, which is a `div` with a width. The one genuine time series,
footfall by day, is the exception that needs an axis.

### The medication chart is a second pair of eyes

CLAUDE.md §13 step 9 states the separation and the RBAC enforces it: **a doctor
prescribes and does not sign for a dose at the bedside; a nurse gives the drug
and does not prescribe it.** Both people see the whole chart — a nurse has to
be able to read the prescription — and each is offered only the actions their
role holds. Two e2e tests assert it by name.

Three smaller decisions carry weight:

- **"Given" is one tap; everything else is a dialog.** Giving the drug is the
  common case and is held to §7b's fifteen seconds. Not giving one is rare and
  consequential, and the backend refuses it without a reason — so the friction
  sits exactly where the information is needed and nowhere else.
- **The three ways of not giving stay apart.** Refused, held, or the ward ran
  out. Collapsing them into "not given" loses the only thing that decides what
  happens next.
- **Every dose row names the patient and the bed.** `DoseRead` carries them
  because `/ipd/doses` without a filter *is* the ward round, and checking the
  patient against the chart before giving a drug is the check. A round of
  UUIDs does not support it.

### The discharge summary is compiled, never invented

It is assembled from what was already recorded — diagnoses and notes from
`clinical`, reports from `diagnostics`, drugs from the chart — and the doctor
edits what is wrong instead of writing from a blank page. A summary typed from
memory at the end of a shift is the least reliable record in the hospital, and
it is the one the patient takes home.

The draft is compiled **at admission**, not at discharge, so it accumulates as
the stay goes on. The screen therefore offers *Re-read the record*: by day five
the compiler has more to work with than it did on day one, and without that
button the doctor would be transcribing by hand exactly the work this module
exists to remove.

Signing freezes it, and is refused without a diagnosis — a discharge document
that does not say what was wrong is not a document. The screen disables the
button and says so, rather than letting the doctor press it and be told no.

### The bed board, and the two rungs everybody leaves out

The board is the one screen in the system that is **not paginated**, and the
endpoint says so: a bed board you have to page through is a bed board nobody
can read at a glance, which is its only purpose. The response is bounded by the
hospital's physical bed count.

Two states carry the weight, and skipping either is what makes a bed board lie:

- **`CLEANING`.** Without it a bed looks free the instant a patient is
  discharged, admissions sends somebody to it, and they arrive to find it
  unmade. Within a week the ward is back on a whiteboard. Housekeeping gets a
  one-click action of its own, gated on `bed:clean` — which a nurse holds and a
  consultant does not, because a consultant "can see the board and does not run
  it". Coming back from maintenance lands in `CLEANING` too: a repaired bed is
  made up before anyone is put in it.
- **`DISCHARGE_INITIATED`.** The doctor writing the discharge does *not* free
  the bed. The patient is still in it, settling the bill and waiting for
  medicines, and a board that frees the bed at the signature double-books it.

Admitting posts to `/ipd/admissions`, never to the encounter's own `admit`
transition. The two are not interchangeable: the IPD endpoint takes the bed
*and* moves the visit, in that order and in one transaction, so no subscriber
ever sees an admitted patient with nowhere to be.

Two discharge outcomes are deliberately missing from the picker — death, and
leaving against medical advice. Both are recorded against the **visit** through
`clinical`, which captures what §6 requires (who certified, the cause, whether
the LAMA form was signed), and the admission follows automatically. The
backend's schema refuses them on the discharge endpoint; offering them on this
screen would create a second, thinner way to record a death.

### Administration exists because nothing worked without it

Every one of those `/admin` screens replaces running a Python script against
the database. Until they existed a hospital could not set its own prices, add
an investigation to the catalogue, create a staff account, give somebody a
clinic profile, or make a bed — the entire system assumed configuration that
only `seed_demo.py` provided.

Two of them are worth reading the code of, because what they guard against is
invisible from the screen:

- **Services and prices.** Billing never refuses a charge for missing
  configuration: an unpriced service is captured at zero and flagged
  `needs_pricing`, deliberately, so a half-configured hospital can still order
  a blood test. The failure that creates is silent — a hospital with no default
  rate card bills nobody, nothing reaches the cash counter, and every screen
  looks like it is working. So the screen leads with whether a default card
  exists and counts the services that have no price.
- **Doctors.** A doctor is two records: an account that can sign in
  (`identity`) and a profile that can hold a clinic (`scheduling`). A newly
  created account with the DOCTOR role still cannot be picked at the counter
  until it has a profile, so the screen counts the accounts in that state and
  says so.

### One box, all seven identifiers

`/search` resolves everything CLAUDE.md §7b names — UHID, name, mobile, token,
visit number, booking number, doctor — in **one request**, and it had to,
because three of those live in `patients`, three in `scheduling` and one in
`clinical`. The alternative was the search component calling three endpoints
and merging, which re-ranks by string distance and can bury an exact match on a
printed number under a near miss on a name.

It lives in `scheduling` for a structural reason rather than a semantic one:
that module already depends on both `patients` and `clinical`, so composing
them there adds no edge the module graph did not already have. Putting it in
`patients` — the obvious home — would have `patients` importing two modules
downstream of it.

One detail is load-bearing and was wrong first time: **a single digit is a
valid query.** The two-character minimum that stops a lone letter matching most
of the hospital would also refuse token 4, and the first nine patients of every
clinic hold a single-digit token. The rule is about the shape of the term, not
its length.

### The clearance desks close the loop

Worth stating plainly, because it is the difference between a demo and a
system. The consultation screen can put a visit into `PENDING_CLEARANCE`, and
until the bench and the counter existed **nothing in the browser could get it
out again** — the happy path dead-ended one click after the doctor finished.

Both boards are built from the record that exists from the very first moment,
which is not the record you would reach for:

- **The bench worklist is built from *orders*, not reports.** An order nobody
  has accessioned has no report at all, and it is precisely the one most likely
  to be forgotten. A worklist assembled from reports only ever shows work
  somebody already remembered to start.
- **The counter's board is built from *charges*, not invoices.** A visit whose
  charges nobody has assembled owes money just as surely as one with an unpaid
  invoice — and it is the more common way a patient leaves without paying,
  because there is no document whose absence anyone notices.

Each row carries patient identity, loaded in bulk: one query for the page, not
one per row. Two tests count SQL statements to keep it that way, because the
regression would surface in production as "the lab screen got slow" months
after the change that caused it.

The bench row also carries a single derived `stage` — `AWAITING_ACCESSION`,
`AWAITING_COLLECTION`, `AWAITING_RECEIPT`, `AWAITING_RESULTS`,
`AWAITING_VERIFICATION` — computed on the server from an order's status, its
sample's status and its report's status. Three enums combined once, where they
can be tested, instead of in the head of whoever is at the bench.

Taking the last rupee closes the visit, and no screen decides that. The payment
route calls `reevaluate_closure`, which asks every module what it is still
holding. `e2e/clearance.spec.ts` walks one patient across four staff sessions
and asserts exactly that: the visit reaches `COMPLETED` because the last pending
item was cleared, not because anybody chose it.

### Design rules that are not preferences

- **Server components by default.** The shell, nav and page frames render as
  HTML. Only search, the shortcut bindings, the user menu and the mutating
  panels are client components.
- **44px minimum tap target** (`h-tap`), 16px minimum body text, high contrast.
  These are for a counter touchscreen under fluorescent light, not for taste.
- **Status is a colour defined once.** `status-chip.tsx` maps every encounter
  state to a tone, and every chip carries its label too — colour is never the
  only signal.
- **The safety banner never collapses.** Drug allergies and infectious
  precautions are not behind a toggle, and they arrive in the same response as
  the chart so there is no moment where the screen has data and no warning.
- **The doctor never picks a status.** They say they are finished; the state
  machine decides. There is no status dropdown on the consultation screen, and
  an e2e test asserts its absence.
- **i18n from the first screen.** English and Hindi, resolved from a cookie
  with no locale in the URL — language is a property of the staff member, not
  of the page.

---

## Testing

```bash
cd backend
docker compose up -d postgres redis   # both, not just postgres — see below
pytest                                # 874 tests
pytest -m "not integration"           # offline: 223 tests, ~5s
```

Integration tests need a local Postgres and run against `TEST_DATABASE_URL` —
never against Neon. The harness creates the test database, runs the migrations,
and creates the same restricted `hms_app` role used in production. That last
part matters: the container's `POSTGRES_USER` is a superuser, so without it
every row-level-security test would pass vacuously.

**Start Redis too.** The RBAC permission cache reads it on every authenticated
request. With Redis down each of those requests waits out the client's connect
retry before falling back to the database — about four seconds — so the suite
still passes but takes hours instead of minutes. If the tests seem to hang, that
is almost always why. It is worth knowing for production as well: a Redis outage
degrades this system's latency far more than it degrades its correctness.

### End-to-end

```bash
cd backend
python scripts/reset_demo_data.py --yes    # see below — do this periodically
python scripts/seed_demo.py && uvicorn app.main:app                 # terminal 1
cd frontend && npm run dev                                          # terminal 2
cd frontend && npm run test:e2e                                     # terminal 3
```

**Reset the data every so often.** Every spec registers a patient and opens a
visit, and the ones that deliberately stop halfway leave it open. Nothing
cleans up, so after a few dozen runs the cash counter and the diagnostics bench
are hundreds of abandoned test visits deep. Both boards are ordered
oldest-first — correct, because the person waiting longest goes first — so a
visit created two seconds ago ends up on the last page and specs begin failing
for reasons that have nothing to do with the code.

**Beds are the exception, and they get a lifecycle of their own.** Visits are a
list that grows; beds are a fixed pool that empties. `wards` and `chart` used to
take ten beds a run and give one back, so with twelve in General Ward the suite
passed twice and then failed on the third run — with a locator timeout *inside
admissions* that reads exactly like a regression in the code under test. "Reset
more often" is not a fix for that: a suite that only passes on freshly reset
data fails at the worst possible moment and blames the wrong thing.

So `support.ts` tracks every bed a spec takes and hands it back in an
`afterEach` — discharged and marked clean, as the hospital admin, the one role
holding both `admission:discharge` and `bed:clean`. It releases in `afterEach`
rather than at the end of each test body so that a test failing *after* it
admits still gives the bed up, and it clears cookies rather than driving the
sign-out menu, because teardown that needs the UI in a good state stops working
exactly when it is needed. Two full runs back to back now leave the pool
untouched: every bed `AVAILABLE`, none occupied, none in cleaning.

`reset_demo_data.py` clears patients, visits, orders, reports and invoices
while keeping staff, roles, prices, wards and the catalogue. It refuses a
non-local database, and it does **not** clear the audit log: that table is
append-only, enforced by a trigger that blocks TRUNCATE as well as DELETE
(§12), and a trail a maintenance script can erase is not a trail.

It connects on `DIRECT_URL`, not `DATABASE_URL`. Truncating thirty-three tables
in one statement is schema-level work, and the application role deliberately
holds no TRUNCATE privilege and cannot bypass row-level security — both worth
keeping, and neither worth relaxing so a maintenance script can run. Deleting
row by row as the application role is not the alternative it looks like:
`insurance_claims` references `invoices` `ON DELETE RESTRICT`, so a teardown
would have to be ordered against every foreign key in the system and re-ordered
whenever one is added.

Two guards, because this script once silently destroyed every bed in the
hospital — `beds.reserved_for_patient_id` references `patients`, so
`TRUNCATE patients CASCADE` took the beds with it while the script printed that
configuration was untouched. `tests/test_reset_scope.py` reads the real foreign
keys out of SQLModel's metadata and fails if any operational table can cascade
into a configuration one; the script itself counts beds before and after and
rolls back if the number moved.

130 Playwright specs, run against a **real backend and a real database** — no
mocks anywhere. That is deliberate: the unit tests already prove each service in
isolation, and what remains untested is precisely the seam between browser, BFF
proxy, FastAPI and Postgres. A mocked e2e suite would pass on the morning the
session cookie stopped being set.

They run serially against one shared tenant, because parallel specs would see
each other's patients in the queue — flakiness that looks like a queue bug.

The suite is also where CLAUDE.md §7b's acceptance gates are actually measured
rather than asserted in prose. If the registration timing test fails, read
[Database latency](#database-latency-read-this-before-blaming-the-ui) before
touching a component.

---

## Layout

```
backend/
├── app/
│   ├── main.py            FastAPI factory, lifespan, health routes
│   ├── registry.py        the one import that populates SQLModel.metadata
│   ├── core/
│   │   ├── config.py      pydantic-settings; URL normalisation
│   │   ├── database.py    async engine, sessions, RLS tenant context
│   │   ├── models.py      id / hospital_id / timestamps / soft-delete mixins
│   │   ├── ids.py         UUIDv7 primary keys
│   │   ├── events.py      in-process domain event bus (extraction seam)
│   │   ├── exceptions.py  domain errors + central HTTP mapping
│   │   ├── security.py    Argon2 hashing, JWT issue/verify
│   │   ├── deps.py        auth, tenant binding and RBAC dependencies
│   │   ├── pagination.py  Page / PageParams used by every list endpoint
│   │   ├── logging.py     console locally, JSON when deployed
│   │   └── redis.py       Redis client + health probe
│   ├── modules/
│   │   ├── tenancy/       hospitals — the tenant record
│   │   ├── identity/      users, roles, permissions, tokens, audit, RBAC cache
│   │   ├── patients/      patient master, UHID, duplicates, alerts, consent
│   │   ├── scheduling/    doctors, availability, appointments, OPD queue
│   │   ├── clinical/      the Encounter + state_machine.py, vitals, notes,
│   │   │                  templates, diagnoses, orders — the core
│   │   ├── diagnostics/   test catalogue, reference ranges, specimens,
│   │   │                  reports, results — lab and radiology
│   │   ├── billing/       rate cards, charges, tax.py (GST), invoices,
│   │   │                  payments, claims, settlement
│   │   ├── notifications/ templates.py (copy + safe rendering),
│   │   │                  gateways.py (the provider seam), the dispatcher,
│   │   │                  suppressions — WhatsApp → SMS → email
│   │   ├── ipd/           wards, beds, admissions, bed history,
│   │   │                  transitions.py (the bed lifecycle), the medication
│   │   │                  chart, summary.py (discharge summary compiler)
│   │   ├── reporting/     KPI queries over six SQL views it owns — no tables;
│   │   │                  models.py is the view contract, not a schema
│   │   └── …              one package per domain (CLAUDE.md §2)
│   └── workers/
│       ├── main.py        ARQ WorkerSettings (the cron schedule)
│       └── tasks.py       encounter auto-close, overdue no-shows,
│                          failed-message retry, bed-day accrual,
│                          missed-dose marking, chart top-up
├── alembic/               migration environment (uses DIRECT_URL)
├── scripts/
│   ├── bootstrap_db_role.py   create the least-privilege app role
│   ├── create_admin.py        create the first PLATFORM_ADMIN
│   ├── seed_demo.py           a working tenant: one of each front-line role
│   ├── export_openapi.py      the frontend's type source (no server needed)
│   └── export_rbac.py         permission + role unions for TypeScript
└── tests/

frontend/
├── src/
│   ├── proxy.ts           Next 16's Middleware: session guard + CSP + refresh
│   ├── app/
│   │   ├── bff/[...path]/ THE proxy — the only route to FastAPI
│   │   ├── auth/refresh/  token rotation ahead of a navigation
│   │   ├── login/         · change-password/
│   │   └── (app)/         the signed-in shell: reception, doctor, queue,
│   │                      consultation, patients
│   ├── features/          one folder per backend domain, plus reports/
│   ├── components/        ui/ (shadcn + link-button), app-shell/, status-chip, safety-banner
│   ├── lib/
│   │   ├── session.ts     the httpOnly cookie — read the header comment
│   │   ├── api/           fetch.ts (server) · client.ts (browser) ·
│   │   │                  refresh.ts (rotation, deduplicated) · server.ts (DAL)
│   │   ├── permissions.ts what to *render* — never an access decision
│   │   └── shortcuts.ts   keyboard map, as data (§7b: configurable)
│   ├── i18n/              en + hi, resolved from a cookie, no locale in the URL
│   └── types/             GENERATED — openapi.d.ts, rbac.generated.ts
├── e2e/                   Playwright, against a real backend
└── scripts/codegen.mjs    regenerate + `--check` for CI
```

Each module exposes `service.py` as its only public interface. Cross-module
access goes through it — never another module's tables, models or router. That
boundary is what makes `billing` and `notifications` extractable into their own
services later without a rewrite.

The frontend mirrors it: `features/` maps to backend domains, and nothing in
`src/types/` is hand-written.

---

## Conventions worth knowing before you write code

- **Async everywhere.** Async routes, async sessions, async migrations.
- **UUIDv7 primary keys** via `app.core.ids.new_id` — time-ordered, so inserts
  stay index-adjacent as the encounter tables grow.
- **Every tenant-scoped table carries `hospital_id`** and inherits from a mixin
  in `app/core/models.py`. Multi-tenancy is never retrofitted.
- **Clinical and financial records are soft-deleted**, never removed.
- **Business logic lives in `service.py`.** Routers stay thin; models hold none.
- **Errors are typed domain exceptions** mapped to HTTP centrally in
  `app/core/exceptions.py`. Services never build responses.
- **Every list endpoint is paginated.**

Three invariants to actively guard (CLAUDE.md §14, plus one Phase 6 added):

1. An `Encounter.status` is only ever changed by the state machine.
2. A notification is never sent **to** a deceased patient. Checked at enqueue,
   re-checked at dispatch, and refused outright by a database trigger on
   `notification_attempts`; recording a death also cancels whatever is already
   queued. `patients.is_deceased` is denormalised onto the patient record
   precisely so the dispatcher can answer from one row — an invariant that
   depends on remembering to join is not an invariant. Note the preposition: a
   message *about* a deceased patient addressed to *staff* is legitimate and
   §6 requires it. See `tests/test_notifications_suppression.py`, which names
   each layer.
3. A billable act is captured exactly once, and never lost. Once, because the
   partial unique index on `charges` refuses a second row for the same source
   act. Never lost, because `capture_charge` cannot fail on configuration — it
   records at zero and flags rather than raising. Both properties have tests
   named after them in `tests/test_billing_service.py`; if you change
   `capture_charge`, keep them passing.

---

## Security notes

- `.env` is gitignored. `.env.example` documents every variable; secrets never
  enter the repo.
- TLS certificates are verified on every database connection.
- Both containers run as a non-root user.
- OpenAPI docs are disabled when `ENVIRONMENT=production`.
- Unhandled exceptions return a generic message; details go to the log only.

Frontend-specific:

- **No JWT is reachable from page JavaScript.** httpOnly cookie, server-side
  attachment, asserted by an e2e test.
- **A nonce-based CSP** is set per request in `src/proxy.ts`. `connect-src
  'self'` is the one worth noticing: a compromised dependency cannot post
  patient data anywhere, and it costs nothing because the browser only ever
  talks to this origin.
- **`style-src` carries no `'unsafe-inline'`, and that is not an oversight.**
  It used to, as an intended fallback, and it did nothing whatsoever: the CSP
  spec says a nonce in a directive makes the browser *ignore* `'unsafe-inline'`
  in that same directive. The policy therefore described a permission it was
  never granting — and that cost something real. Sonner injects its stylesheet
  at runtime through a bare `createElement("style")` with no nonce, CSP refused
  it, and **every toast in the application rendered unstyled and
  `position: static`** — in the document flow instead of floating in the
  corner. It went unnoticed because a toast's *text* is in the DOM either way,
  so the e2e suite stayed green while confirmations were visibly broken.
  `globals.css` now imports the same stylesheet through the bundler, where it
  is served from `self` and trusted. `e2e/security.spec.ts` asserts both halves
  — that the policy says what we think, and that a toast actually computes to
  `position: fixed` — plus a budget on blocked inline styles so a *new* one
  fails the suite instead of hiding in the noise. Next's development overlay
  contributes about thirty violations a page load and is filtered out of that
  budget; a production build reports exactly two, both Sonner's own now-
  redundant attempt.
- `frame-ancestors 'none'` — clickjacking a "record death" confirmation is not
  a risk worth carrying.
- **A link that looks like a button is still a link** (`components/ui/link-button.tsx`).
  These were `<Button render={<Link />}>`, which Base UI warned about on every
  render, correctly: it renders an `<a>` while treating it as a native button,
  putting a meaningless `type="button"` on the anchor. The warning's own
  suggested remedy — `nativeButton={false}` — is worse, because it silences the
  warning by adding `role="button"`: these controls navigate, so that announces
  a link as a button and drops it out of the browser's link list. Both
  behaviours were verified by rendering them and reading the DOM, not inferred.
  `LinkButton` takes the third option, button styling on a plain anchor with
  Base UI not involved, and `e2e/security.spec.ts` asserts no anchor carries
  either attribute.
- `Referrer-Policy: no-referrer`, because patient identifiers appear in URLs.
- The `next` parameter on `/login` and `/auth/refresh` is validated against
  protocol-relative URLs, so neither becomes an open redirect — a convincing
  phishing page reached through a genuine hospital link.
- Sign-out revokes the refresh token server-side, not just locally. On a shared
  counter machine, forgetting a token is not signing out.
- No `maxAge` on the session cookie: it dies with the browser, so a machine
  left open overnight does not stay signed in.

The Neon free tier is for building and testing. Before real patient data goes
in, move to a paid tier with proper backup retention and a signed data-
processing agreement (CLAUDE.md §5).
