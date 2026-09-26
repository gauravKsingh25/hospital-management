# CLAUDE.md — Hospital Patient-Journey Management System

> **PLACEMENT: this file goes in the CODEBASE.** Put it at the repository root as
> `CLAUDE.md`. Claude Code reads it automatically as the project's standing
> instructions. This is the technical spec — architecture, stack, conventions,
> build order. (The separate `execution-plan.md` is for you and stakeholders, not
> the codebase.)
>
> This file is the single source of truth for Claude Code building this system.
> Read it fully before writing any code. Follow the architecture and conventions
> exactly. When you finish a unit of work, stop and propose the next step rather
> than sprinting ahead — see "Working Agreement" at the bottom.

---

## 1. What we are building

A patient-journey management system for a **mid-size, multi-department private
hospital in India**. It tracks a patient end-to-end: registration → doctor
assignment → OPD consultation → diagnostics → (discharge OR IPD admission) →
billing/invoicing → follow-up. It must support edge cases (death, referral out,
absconding/LAMA) and be designed so **staff do most data entry, not doctors**.

**Non-negotiable design goals (in priority order):**
1. **Scalable** — handles growth in patients, staff, and eventually multiple hospitals.
2. **Multi-tenant ready** — one deployment can serve multiple hospitals in future.
3. **Low doctor involvement** — doctors only do clinical acts (notes, orders, sign-off); receptionists, nurses, cashiers, lab/pharmacy staff handle everything else.
4. **Fast & light frontend** — clean, minimal, usable by non-technical staff with little training.
5. **RBAC** — many roles, each sees only what it needs.
6. **Robust edge cases** — death, referral, LAMA, no-show, cancellation are first-class states, not afterthoughts.

---

## 2. Architecture decision: modular monolith, not microservices (yet)

**We are building a MODULAR MONOLITH, not microservices.** This is deliberate.
Do not create separate services, separate repos, or inter-service HTTP calls.

**Why:** premature microservices multiply complexity (distributed transactions,
service discovery, network failures, deployment overhead) before there is any
scale to justify it. A modular monolith gives us clean boundaries now and cheap
extraction later.

**How we stay extraction-ready:**
- Each domain lives in its own module (Python package) under `app/modules/`.
- Modules communicate through **service-layer function calls and a shared event
  bus (in-process for now)** — never by reaching into another module's database
  tables or internal classes directly.
- Each module exposes a clean `service.py` interface. Cross-module reads go
  through that interface, not raw SQL joins across module boundaries.
- When a module must scale independently later (billing and the future patient
  app are the likeliest first candidates), it can be lifted into its own FastAPI
  service with minimal change, because the boundary already exists.

**Module list (each is a package under `app/modules/`):**

| Module | Responsibility | Future extraction candidate? |
|---|---|---|
| `identity` | Auth, users, roles, RBAC, audit log | No — stays central |
| `tenancy` | Hospital/facility records, multi-tenant context | No — stays central |
| `patients` | Patient master record, UHID, demographics, ABHA link | Maybe |
| `scheduling` | Appointments, doctor availability, queue/token | Maybe |
| `clinical` | Encounters, vitals, notes, diagnosis, orders (EMR core) | No — core |
| `diagnostics` | Lab & radiology order lifecycle, results | Yes |
| `ipd` | Admission, beds/wards, nursing, rounds, discharge summary | Yes |
| `billing` | Charges, invoices, payments, insurance/scheme claims | **Yes — first** |
| `notifications` | SMS/WhatsApp/email dispatch, templates, triggers | **Yes — early** |
| `reporting` | Dashboards, KPIs, analytics (read-only views) | Yes |

---

## 3. Multi-tenancy (build it in from line one)

Every tenant-scoped table carries a `hospital_id` (FK to `tenancy.hospitals`).
Do NOT add multi-tenancy later — retrofitting it is a painful migration.

**Approach: shared database, shared schema, row-level isolation.**
- Every query is automatically filtered by the current `hospital_id`, resolved
  from the authenticated user's tenant context (a FastAPI dependency injects it).
- Enforce with **PostgreSQL Row-Level Security (RLS)** policies as a hard backstop,
  so even a buggy query cannot leak cross-tenant data.
- The `identity` and `tenancy` modules are the only ones allowed to operate
  across tenants (for platform admin).
- A single hospital today is just "one tenant." Nothing special-cased.

---

## 4. Tech stack (fixed — do not substitute)

> Version note: these are the current stable majors as of mid-2026. Pin exact
> versions in `pyproject.toml` / `package.json`, but do NOT drop below the majors
> below — older majors (e.g. Next.js 14) are end-of-life. Verify the latest patch
> at build time with `npm show next version` and `pip index versions fastapi`.

| Layer | Choice | Notes |
|---|---|---|
| Backend | **FastAPI (latest)** on **Python 3.13** (3.12 min) | async, Pydantic v2, dependency injection |
| ORM | **SQLModel** (SQLAlchemy 2.0 core + Pydantic) | typed models, async engine (asyncpg driver) |
| Migrations | **Alembic** | every schema change is a migration, no exceptions |
| Database | **PostgreSQL 18** (18.x current stable; 16/17 also fine) | hosted on **Neon** (serverless, free tier) — see §5 |
| Validation | **Pydantic v2** | all request/response schemas |
| Auth | **JWT (access + refresh)**, argon2 hashing | RBAC in `identity` module |
| Task queue | **ARQ** (async, Redis) — or Celery + Redis | notifications, timeouts, batch jobs |
| Frontend | **Next.js 16** (App Router) + **React 19** + TypeScript 5.x | server components by default; Turbopack is the default bundler |
| Runtime | **Node.js 20+** (required by Next.js 16) | LTS; 22 preferred |
| UI library | **shadcn/ui + Tailwind CSS v4** | latest, lightweight, accessible, themeable |
| Data fetching | **TanStack Query v5** | caching, background refresh |
| Forms | **React Hook Form + Zod** | Zod schemas mirror backend Pydantic |
| Tables | **TanStack Table v8** | for patient lists, queues, reports |
| Charts | **Recharts** | dashboards |
| i18n | **next-intl** | English + Hindi + regional |
| Testing | **pytest** (backend), **Playwright** (e2e) | |
| Containerization | **Docker + Docker Compose** | reproducible local + deploy |

**Frontend performance rules (goal: fast, light, clean):**
- React Server Components by default; mark client components with `"use client"`
  only where interactivity needs them.
- No heavy UI kits (no Material UI). shadcn/ui compiles to minimal CSS.
- Lazy-load routes and heavy widgets (charts, DICOM viewer).
- Skeleton loaders, optimistic updates via TanStack Query.
- Keep the initial JS bundle small; audit with `next build` output.
- Clean, high-contrast, minimal UI — large tap targets, minimal clicks per task,
  Hindi + regional-language support ready via i18n (`next-intl`).

---

## 5. Database — recommendation & rationale

**Use PostgreSQL 18. Host it free on Neon (the chosen provider).**

**Clarifying "free": PostgreSQL the software is always free (open source). But a
running database must live on an always-on machine — that's *hosting*, a separate
thing. You do NOT run a server yourself; a managed provider runs Postgres and
gives you a connection string. Hosting is genuinely a few clicks now.**

Why Postgres:
- Relational integrity is essential (invoices reference charges reference
  encounters reference patients — you want real FKs and transactions).
- JSONB columns give FHIR-style flexibility where you need loose structure
  (e.g. raw lab payloads) without giving up relational rigor elsewhere.
- Row-Level Security enforces multi-tenant isolation at the DB layer.
- Free, open-source, no vendor lock-in, huge ecosystem.

Why Neon to start (the chosen provider):
- Serverless Postgres with a free tier; deployable in minutes, gives you a
  connection string and a dashboard for inspecting real test data.
- **Database branching** — Neon can branch the database like Git, so you get an
  isolated copy per feature/PR for testing without touching main data. Genuinely
  useful during development.
- Scales compute independently; **migrate off with zero code change** — it IS
  just Postgres (move to a paid Neon tier, or managed RDS/Cloud SQL later).

**Neon specifics to code around:**
- **Auto-suspend on idle:** free-tier compute scales to zero after inactivity and
  cold-starts on the next connection (a short delay on the first request). Use a
  connection pooler and expect the first query after idle to be slower — don't
  treat a cold-start latency spike as an error.
- Use Neon's **pooled connection string** (PgBouncer) for the app, and the direct
  (unpooled) string for Alembic migrations. Put both in `.env` as
  `DATABASE_URL` (pooled) and `DIRECT_URL` (unpooled).
- Driver stays **asyncpg**; nothing app-side changes vs any other Postgres.

**Two honest caveats — free tier is for building/testing, NOT live patients:**
- Free tiers have limited/short backup retention and are **not compliance-grade**.
  Before any real patient data goes in, move to a paid tier with proper backups
  and a signed data-processing agreement.
- Keep the migration path open: everything is standard Postgres, so Neon → RDS/
  Cloud SQL is a connection-string change, not a rewrite.

**Deployment path:** Docker Compose (local Postgres) → **Neon** free Postgres for
shared test data → containerized backend on Railway/Render/Fly.io → paid managed
Postgres (Neon paid, RDS, or Cloud SQL) with backups when real patients arrive.

**DB integration rules:**
- Async SQLAlchemy engine + connection pooling (asyncpg driver).
- All access through the ORM/service layer; no raw SQL except in `reporting`
  read-only views where performance demands it.
- Every table: `id` (UUID), `hospital_id` (except platform tables),
  `created_at`, `updated_at`, soft-delete `deleted_at` where records must never
  be hard-deleted (all clinical & financial records).
- Indexes on every FK and on high-cardinality lookup columns (UHID, phone).

---

## 6. The patient journey as a STATE MACHINE (critical)

The `Encounter` is the spine of the system. Every patient visit is an Encounter
with an explicit `status`. Transitions are the ONLY way status changes, and each
transition is triggered by a specific role's action. This is how we track OPD
patients correctly without depending on them returning to reception.

**Encounter statuses:**
```
REGISTERED        → patient checked in, UHID assigned
IN_CONSULTATION   → assigned to doctor / vitals being taken
AWAITING_RESULTS  → doctor ordered lab/radiology, waiting
PENDING_CLEARANCE → charges/meds/tests pending at counter/lab/pharmacy
ADMITTED          → converted to IPD (see ipd module)
COMPLETED         → visit closed cleanly (OPD discharge)
CANCELLED         → visit cancelled before consultation
NO_SHOW           → patient never arrived for a booked slot
```
**Edge-case terminal statuses (first-class, not afterthoughts):**
```
REFERRED_OUT      → sent to another facility; capture reason + destination
LAMA              → Left Against Medical Advice / absconded; capture who recorded it
DECEASED          → death recorded; capture datetime, certifying doctor, cause
```

**Key rule — OPD closure is triggered by STAFF ACTION, not patient location:**
- When the **doctor saves & marks the consultation complete**, if there are NO
  pending items → Encounter auto-transitions to `COMPLETED`. The patient does not
  need to return to the single reception desk.
- If the doctor ordered something billable/dispensable → status goes to
  `PENDING_CLEARANCE`; it becomes `COMPLETED` only when the last pending item is
  cleared by the relevant staff (cashier / lab tech / pharmacist).
- **Auto-timeout safety net:** a background job closes any Encounter left in a
  non-terminal state with no pending items after a configurable buffer (default
  end-of-day), so forgotten clicks don't leave encounters open forever.

**Death & referral handling:**
- `DECEASED` and `REFERRED_OUT` can be entered from most active states by an
  authorized role. They are terminal. They must:
  - record structured metadata (see schema),
  - still trigger a final billing settlement flow (a deceased/referred patient
    usually still has an outstanding bill),
  - suppress follow-up/recall notifications (never send a "book your follow-up"
    SMS to a deceased patient's family — this is a hard rule, enforce it).

Implement transitions in `clinical/state_machine.py` with a single
`transition(encounter, to_status, actor, metadata)` function that validates the
transition is legal, writes an `EncounterEvent` audit row, and fires domain
events. NO status field is ever set directly anywhere else.

---

## 7. "Less doctor, more staff" — how it shapes the design

Doctors are expensive and time-poor. The system minimizes what they must do:
- **Doctors do only:** review vitals, write/dictate the clinical note, select
  diagnosis, place orders, e-sign, mark consultation complete. One screen, few clicks.
- **Receptionist/front desk does:** registration, appointment booking, check-in,
  cashier duties, printing, discharge paperwork handoff.
- **Nurse does:** vitals entry, queue management, IPD nursing notes, medication
  administration recording.
- **Lab/radiology tech does:** result entry, marking reports ready.
- **Pharmacist does:** dispensing, stock.
- **Billing staff does:** invoices, claims, payments.

Design every workflow so the doctor's surface is the smallest. Pre-fill
everything possible. Use templates and defaults. The doctor should never type an
address, a phone number, or a price.

---

## 7b. Workflow optimization requirements (speed & adoption)

The system is judged on **time saved per patient**, not architecture elegance.
Small savings (10–30s per patient) compound to hours across a busy OPD. Build
these in — most fall out of the architecture already described and cost little.

**Build into the MVP (low effort, high value):**
- **Rapid registration:** only 4 required fields — name, mobile, approximate age,
  gender. Everything else (address, insurance, guardian) is optional and captured
  later. This IS the registration form; don't gate the queue on more.
- **One-click OPD:** Quick OPD → select doctor → generate token → patient joins
  queue. The appointment, encounter, and queue record are created automatically in
  one action — the receptionist takes one path, not five.
- **Role-based dashboards:** each role sees only its screens (falls out of RBAC).
  Reception: register, search, today's queue, billing. Doctor: waiting patients,
  current consultation, recently seen, completed today. Nurse: assigned patients,
  vitals, medication, shift notes.
- **Touch-friendly + progressive disclosure:** large buttons, high contrast, large
  fonts, minimal scrolling. Show only fields needed for the current task; reveal
  the rest on demand. (Design rules — enforce in `frontend-design`.)
- **Keyboard-first:** shortcuts for power users — e.g. F2 new patient, F3 search,
  F4 billing, Ctrl+Enter save, Alt+N next patient. Configurable, not hardcoded.
- **Universal search:** one box resolves UHID, name, mobile, token, doctor, or
  appointment number.
- **Color-coded patient status:** the Encounter status drives a color chip
  (waiting / with doctor / billing / lab / closed). This is just the state machine
  rendered — no new data.
- **Duplicate detection:** suggest existing records while typing name/mobile
  (already planned in `patients`).
- **Patient timeline:** the Encounter event feed rendered chronologically
  (registration → OPD → lab → admission → discharge → follow-up).
- **Doctor templates:** reusable diagnosis / prescription / advice / follow-up
  templates — central to the low-doctor-effort goal.
- **Nurse quick actions:** one-click record vitals, medication given, sample
  collected, shift notes, discharge-ready.
- **Automatic billing:** charges flow automatically from consultation, lab,
  radiology, pharmacy via the event bus; reception reviews the invoice, never
  rebuilds it (already core to `billing`).
- **Patient safety banner:** a persistent banner across the patient's workflow for
  drug allergies, high-risk conditions, infectious precautions, fall risk. High
  value, low cost — implement as a always-visible header on patient screens.

**Post-MVP (medium effort — wire in with the relevant module):**
- QR-based patient lookup (card QR opens the profile) — post-MVP.
- Auto-save registration drafts (resume an interrupted registration) — post-MVP.
- Queue analytics: live waiting count, average wait, doctor workload, department
  congestion — build with `reporting`.
- Doctor delay alerts to reception when a clinic runs behind — with `reporting`.
- Bed cleaning lifecycle (discharged → cleaning → ready → occupied) — with `ipd`,
  so bed availability is never wrong.
- Staff workload dashboard, one-click shift handover, smart follow-up interval
  suggestions (diagnosis → recommended review interval) — post-MVP.

**Extra modules (roadmap, NOT MVP):** patient-flow analytics, standalone queue
management, housekeeping, biomedical equipment maintenance, internal comms,
digital multilingual consent, staff productivity, inventory forecasting, hospital
command center, patient feedback. Note them on the roadmap; do not build in MVP.

**Explicitly OUT of scope (do NOT build):**
- **Offline queue / offline-first sync** — high complexity (local storage,
  conflict resolution, sync reconciliation) with real risk of corrupting patient
  and billing data, and a poor fit against a cloud database. Rely on a reliable
  connection plus auto-save drafts instead. Do not attempt.
- **Voice dictation** — depends on a third-party speech API with accuracy, cost,
  and clinical-audio privacy concerns under the DPDP Act. Not in scope.

**UX acceptance gate (these are pass/fail targets, see §15):**
- Reception completes a registration in **under 30 seconds**.
- Doctor completes routine documentation in **under 60 seconds**.
- Nurse performs a common action in **under 15 seconds**.
- A new staff member learns their role's workflow in **under 30 minutes**.
- The software takes **fewer clicks than the paper process** it replaces.

If any of these fail for a feature, simplify the workflow before adding anything.

---

## 8. RBAC (role-based access control)

Implement in the `identity` module. Model: Users have Roles; Roles have
Permissions; Permissions gate actions. Support **multiple roles per user** (a
person can be both nurse and shift-in-charge).

**Seed roles (extensible — data-driven, not hardcoded in logic):**
```
PLATFORM_ADMIN   — cross-tenant, manages hospitals (the only cross-tenant role)
HOSPITAL_ADMIN   — full access within one hospital
DOCTOR           — clinical actions on own/assigned patients
NURSE            — vitals, nursing notes, queue, MAR
RECEPTIONIST     — registration, scheduling, check-in
CASHIER          — payments, invoices (can be merged with receptionist)
LAB_TECH         — diagnostics results
RADIOLOGIST      — radiology reports
PHARMACIST       — dispensing, pharmacy stock
BILLING_STAFF    — invoices, insurance/scheme claims
RECORDS_OFFICER  — can record death/referral/LAMA, manage medical records
AUDITOR          — read-only access to audit logs and reports
```

**Rules:**
- Permissions checked via a FastAPI dependency on every protected route.
- RBAC is **data-driven**: roles and permissions live in tables, so a new role
  needs no code deploy.
- Every sensitive action writes to the **audit log** (who, what, when, tenant,
  before/after) — required for NABH/DPDP compliance. Audit log is append-only.
- Enforce tenant isolation AND role permission on every request.

---

## 9. India-specific requirements to bake in

- **ABHA / ABDM ready:** `patients` table reserves `abha_id`; `hospitals` table
  reserves `hfr_facility_id`. FHIR-shaped models so ABDM APIs slot in later.
- **Multi-rate billing:** a service can have multiple prices (cash, insurance-
  negotiated, PMJAY/CGHS/ECHS package rate). Model rate cards, not single prices.
- **GST per line item:** most clinical services are GST-exempt but diagnostics,
  pharmacy, and room rent may not be. Tax is a per-line-item attribute.
- **UPI-first payments:** payment model supports UPI/QR, card, cash, insurance.
- **WhatsApp-first notifications:** notification channel priority is
  WhatsApp → SMS → email. Design the dispatcher channel-agnostic.
- **Schedule H/H1/X drugs:** pharmacy dispensing validates prescription presence
  for controlled categories (hard validation).
- **Multilingual UI:** i18n from the start (English + Hindi + regional).

---

## 10. Project structure

```
hospital-system/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI app factory, router registration
│   │   ├── core/
│   │   │   ├── config.py           # settings (pydantic-settings, env-driven)
│   │   │   ├── database.py         # async engine, session, RLS context
│   │   │   ├── security.py         # JWT, hashing, password policy
│   │   │   ├── deps.py             # shared dependencies (current_user, tenant)
│   │   │   ├── events.py           # in-process event bus (extraction seam)
│   │   │   └── exceptions.py       # domain exceptions + handlers
│   │   ├── modules/
│   │   │   ├── identity/           # models, schemas, service, router, rbac
│   │   │   ├── tenancy/
│   │   │   ├── patients/
│   │   │   ├── scheduling/
│   │   │   ├── clinical/           # includes state_machine.py
│   │   │   ├── diagnostics/
│   │   │   ├── ipd/
│   │   │   ├── billing/
│   │   │   ├── notifications/
│   │   │   └── reporting/
│   │   └── workers/                # ARQ/Celery tasks (timeouts, dispatch, batch)
│   ├── alembic/                    # migrations
│   ├── tests/
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── app/                    # Next.js App Router (route groups per role area)
│   │   ├── components/             # shadcn/ui-based components
│   │   ├── features/               # feature modules mirroring backend domains
│   │   ├── lib/                    # api client, query hooks, auth, i18n
│   │   └── types/                  # shared TS types (mirror backend schemas)
│   ├── package.json
│   └── Dockerfile
├── docker-compose.yml
└── README.md
```

**Each backend module folder contains exactly:**
```
__init__.py
models.py      # SQLModel tables (the only place tables are defined)
schemas.py     # Pydantic request/response DTOs
service.py     # business logic — the module's PUBLIC interface
router.py      # FastAPI routes, thin — delegates to service
events.py      # domain events this module emits/handles (optional)
rbac.py        # permission definitions for this module (optional)
```
Routers are thin. Business logic lives in `service.py`. Cross-module calls go
through another module's `service.py`, never its `models.py` or `router.py`.

---

## 11. Coding conventions

- **Type everything.** Full type hints backend; strict TS frontend. No `any`.
- **Async all the way** on the backend (async routes, async DB).
- **Pydantic v2** for all boundaries; never return ORM objects raw from routes.
- **UUID primary keys** everywhere.
- **No business logic in routers or models** — it lives in services.
- **Every schema change = an Alembic migration.** Never edit the DB by hand.
- **Every state change = a transition through the state machine + audit row.**
- **Every list endpoint is paginated** (limit/offset or cursor) — never return
  unbounded lists (patient/encounter tables will grow large).
- **Env-driven config** (pydantic-settings). No secrets in code. `.env.example`
  documents every variable.
- **Errors are typed domain exceptions** mapped to HTTP responses centrally.
- **Tests** for every service function and every state transition, especially
  the edge cases (death, referral, LAMA, timeout auto-close).
- Conventional commits (`feat:`, `fix:`, `refactor:`…).

---

## 12. Security & compliance (DPDP Act 2023 / NABH)

- Passwords hashed (argon2/bcrypt); JWT with short-lived access + refresh rotation.
- 2FA available for admin/doctor roles.
- Encryption in transit (TLS) and at rest (DB-level).
- **Append-only audit log** on every access/modification of patient data.
- Explicit patient **consent capture** before data collection/sharing (a real
  record, not a buried checkbox).
- Patient data access rights (view/export) supported.
- Row-Level Security enforced at DB.
- Session timeouts on all roles.

---

## 13. Build order (implement in this sequence)

Do these in order. After each numbered item, STOP and confirm before proceeding
(see Working Agreement).

1. **Foundation:** project scaffold, Docker Compose, Postgres connection, config,
   base model mixins (id/hospital_id/timestamps/soft-delete), Alembic setup.
2. **`tenancy` + `identity`:** hospitals table, users, roles, permissions, JWT
   auth, RBAC dependency, audit log, RLS policies. Seed roles.
3. **`patients`:** patient master, UHID generation, search, duplicate detection,
   consent capture. ABHA field reserved.
4. **`scheduling`:** doctor availability, appointments, check-in, queue/token,
   no-show & cancellation states.
5. **`clinical` (the core):** Encounter model + **state machine** with ALL
   statuses including death/referral/LAMA, vitals, clinical notes, diagnosis,
   order placement, doctor "complete visit" action, auto-timeout worker.
6. **`diagnostics`:** lab/radiology order lifecycle, result entry, report-ready
   notification trigger.
7. **`billing`:** charges auto-captured from other modules via events, rate cards
   (cash/insurance/scheme), per-line GST, invoice generation, payments (UPI-first),
   settlement flow for deceased/referred patients.
8. **`notifications`:** channel-agnostic dispatcher (WhatsApp→SMS→email), templates,
   triggers wired to domain events, hard suppression rule for deceased patients.
9. **`ipd`:** admission, bed/ward management, nursing notes, MAR, rounds,
   discharge summary auto-compile.
10. **`reporting`:** KPI dashboards (footfall, occupancy, revenue, ALOS,
    follow-up compliance), read-only views.
11. **Frontend** is built per-module alongside each backend module, starting with
    auth + patient registration + the doctor consultation screen (smallest doctor
    surface) as the first vertical slice.

---

## 14. Working Agreement (IMPORTANT — how you should operate)

- **Work in small vertical slices.** Prefer "registration works end-to-end
  (model → migration → service → route → test → minimal UI)" over "all models
  for everything first."
- **After completing each build-order item, STOP.** Summarize what you built,
  then ask: *"Is there a more optimal approach before I continue, or shall I
  proceed to the next step?"* Do not barrel through all 11 steps unprompted.
- **When you make a non-obvious design choice, state the trade-off** and offer
  the alternative, so the human can course-correct cheaply.
- **When something could be done more optimally, say so** — propose the better
  way before implementing the naive one.
- **Never leave an Encounter status settable outside the state machine.**
- **Never send a notification to a deceased patient.** Treat these two as
  invariants you actively guard.
- Keep the frontend light: question any dependency that bloats the bundle.

---

## 15. Definition of done (per module)

- Models + Alembic migration committed.
- Service layer with full business logic + type hints.
- Thin router with RBAC + tenant isolation on every route.
- Pydantic schemas for all I/O.
- Pagination on all list endpoints.
- Unit tests for services + state transitions (including edge cases).
- Audit logging on sensitive actions.
- Minimal, clean frontend slice wired to the endpoints.
- `.env.example` updated; README section updated.
- **UX speed gate met** (where the module has a user-facing workflow): the
  relevant §7b target holds — registration <30s, doctor documentation <60s, nurse
  action <15s, fewer clicks than paper. If a workflow misses its target, simplify
  it before the module is considered done.
