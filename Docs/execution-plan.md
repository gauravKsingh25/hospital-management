# Execution Plan — Hospital Patient-Journey System

> **PLACEMENT: this file is for YOU and stakeholders — do NOT put it in the
> codebase.** Keep it in your project docs / Notion / Drive. It's the roadmap and
> rollout plan (phases, timeline, pilot, training, compliance). The technical
> spec that goes in the codebase is the separate `CLAUDE.md`.

Two tracks run in parallel: **A. Software build** (what you and Claude Code do)
and **B. Real-world execution** (getting it live in an actual hospital). Both
matter — great software that ignores hospital operations fails on day one.

---

## Track A — Software build roadmap

### Architecture in one paragraph
A **modular monolith** on **FastAPI (Python 3.13) + PostgreSQL 18** (hosted on
**Neon**, serverless), with hard module boundaries so individual modules (billing,
notifications first) can later be extracted into microservices without a rewrite.
**Multi-tenant from line one** via a `hospital_id` on every tenant-scoped table
plus Postgres Row-Level Security, so one deployment can serve many hospitals later.
**Next.js 16 + React 19 + shadcn/ui (Tailwind v4)** frontend, kept deliberately
light and optimized for speed-per-patient (rapid registration, one-click OPD,
role dashboards — full list in `CLAUDE.md` §7b). The `Encounter` state machine is
the spine that tracks every patient — including death, referral, and LAMA — and
closes OPD visits by staff action rather than by making patients return to
reception.

> All versions above are current stable majors as of mid-2026. Pin exact patches
> at build time but don't drop below these majors (Next.js 14 and earlier are EOL).

### Phase 0 — Foundation (week 1)
- Repo, Docker Compose (api + Postgres + Redis), CI skeleton.
- Config, async DB connection, base model mixins, Alembic.
- Health checks, error handling, logging.
- **Deliverable:** app boots, connects to Postgres, one migration runs.

### Phase 1 — Identity, tenancy & RBAC (weeks 2–3)
- Hospitals (tenants), users, roles, permissions (data-driven), JWT auth.
- RBAC dependency + tenant-context dependency; RLS policies.
- Append-only audit log. Seed the role set.
- **Deliverable:** users can log in; a hospital admin can create staff with roles;
  every request is tenant- and permission-scoped. This is the security backbone —
  do not rush it.

### Phase 2 — Patients & scheduling (weeks 4–5)
- Patient master, UHID generation, search, duplicate detection, consent capture.
- Doctor availability, appointments, check-in, queue/token, no-show & cancellation.
- **Deliverable:** register a patient, book them to a doctor, check them in.

### Phase 3 — Clinical core + state machine (weeks 6–8) — the heart
- Encounter model and the full state machine (all statuses + death/referral/LAMA).
- Vitals (nurse), clinical note + diagnosis + orders (doctor), "complete visit".
- Auto-timeout worker that closes stale encounters.
- Minimal doctor consultation screen (smallest possible doctor surface).
- **Deliverable:** a full OPD visit runs end-to-end and closes correctly WITHOUT
  the patient returning to reception. Edge cases transition correctly.

### Phase 4 — Diagnostics (week 9)
- Lab/radiology order lifecycle, result entry, report-ready trigger.
- **Deliverable:** doctor orders a test; lab enters result; system notifies.

### Phase 5 — Billing (weeks 10–12)
- Charges auto-captured from other modules via the event bus.
- Rate cards (cash / insurance / PMJAY / CGHS / ECHS), per-line GST.
- Invoice generation, payments (UPI-first), deceased/referred settlement flow.
- **Deliverable:** an accurate invoice assembles automatically from everything the
  patient consumed; UPI payment link works.

### Phase 6 — Notifications (week 13)
- Channel-agnostic dispatcher (WhatsApp → SMS → email), templates, event triggers.
- Hard suppression for deceased patients.
- **Deliverable:** appointment, report-ready, payment, and follow-up messages fire
  automatically; nothing ever goes to a deceased patient.

### Phase 7 — IPD (weeks 14–16)
- Admission, bed/ward occupancy, nursing notes, MAR, rounds, discharge summary.
- **Deliverable:** admit → treat → discharge with an auto-compiled summary.

### Phase 8 — Reporting & hardening (weeks 17–18)
- KPI dashboards (footfall, occupancy, revenue, ALOS, follow-up compliance).
- Security review, load test, backup/restore drill, penetration check.
- **Deliverable:** management dashboard; system is production-hardened.

> Timeline assumes focused solo/small-team effort with Claude Code. Treat weeks as
> relative effort, not calendar promises. The MVP that replaces paper is done at
> the end of Phase 5 (registration → consultation → billing).

### How to work with Claude Code
- Feed it `CLAUDE.md` as the project's root instruction file.
- Build **one vertical slice at a time** (model → migration → service → route →
  test → UI), not all models first.
- After each phase, ask it: *"is there a more optimal approach before continuing?"*
  — this is baked into the CLAUDE.md working agreement.
- Keep migrations and tests green before moving on.

---

## Track B — Real-world execution roadmap

Software is ~half the battle. These run alongside the build.

### B1. Discovery & mapping (before/with Phase 0–1)
- Shadow the actual hospital for a few days. Map how registration, OPD, billing,
  and IPD *really* work today — not how they're supposed to.
- Identify the single-reception constraint and every point where paper is used.
- List every role and who currently does what (you'll find people wear multiple
  hats — this validates the multi-role RBAC design).
- Nail down the OPD closure reality (patients leaving straight from the doctor).

### B2. Compliance & legal groundwork (start early, runs long)
- Clinical Establishments Act registration status (state-specific).
- DPDP Act 2023: consent flow, data retention policy, breach process.
- Decide ABDM participation (HFR facility ID, ABHA) — even if phased in later.
- If pursuing NABH: align audit trails, incident reporting, consent docs now.
- Payment gateway KYC (Razorpay/Cashfree) and, if applicable, TPA/PMJAY empanelment.

### B3. Data migration (with Phase 2)
- Existing patient records (registers/Excel/old software) → cleaned → imported.
- Doctor list, department list, service catalogue with prices (rate cards).
- This is often messier and slower than expected — budget real time for cleaning.

### B4. Pilot rollout (after Phase 5 — the MVP)
- Go live with ONE department or OPD only, running **parallel with paper** for a
  short period as a safety net.
- Train the front desk and 2–3 friendly doctors first. Sit with them live.
- Collect friction points daily; fix the top three each day.
- Do NOT roll out all departments at once.

### B5. Staff training & change management (ongoing)
- Doctors resist data entry — this is why the design minimizes their input. Show
  them the screen is 30 seconds, not 5 minutes.
- Train staff on their role's screens only (RBAC keeps it simple per role).
- Provide a one-page cheat-sheet per role. Record short screen-capture videos.
- Appoint an internal "champion" per shift who others can ask.

### B6. Full rollout & stabilization
- Expand department by department once the pilot is stable.
- Retire paper only after a department has run clean on the system for a set period.
- Monitor: encounters stuck in non-terminal states, failed notifications,
  invoice disputes, login/permission issues.

### B7. Operations & iteration
- **Move off the free DB tier before real patients.** Neon's free tier is perfect
  for building and testing (branching, instant provisioning), but free tiers have
  limited backup retention and aren't compliance-grade — not acceptable for live
  patient records. Switch to a paid managed Postgres (Neon paid, or RDS/Cloud SQL)
  with backups and a signed data-processing agreement before go-live. Because it's
  all standard Postgres, this is a connection-string change, not a rewrite.
- Daily backups (test a restore — an untested backup is not a backup).
- Uptime monitoring + on-call for the first months.
- Monthly review of KPIs and staff feedback; prioritize the next features.
- Roadmap the deferred pieces: patient app/portal, telemedicine, full ABDM/FHIR
  interoperability, insurance API integrations, pharmacy inventory depth.

---

## Cross-cutting risks & how the design handles them

| Risk | Mitigation baked into the plan |
|---|---|
| Over-engineering with premature microservices | Modular monolith with clean seams; extract only when needed |
| Multi-tenancy retrofit pain | `hospital_id` + RLS from line one |
| OPD patients "lost" after consultation | State machine closes visits by staff action + auto-timeout |
| Doctors refusing to use it | Smallest-possible doctor surface; staff do the rest |
| Notifications to deceased patients | Hard suppression invariant in notifications module |
| Data leaks across hospitals | Postgres Row-Level Security as a hard backstop |
| Billing inaccuracies | Charges auto-captured via events; rate cards + per-line GST |
| Big-bang rollout failure | Single-department pilot, parallel-with-paper, phased expansion |
| Lost data | Soft-delete on clinical/financial records; tested backups |

---

## What to hand Claude Code first
1. `CLAUDE.md` (the spec) at the repo root.
2. Ask it to execute **Phase 0 only**, then stop and confirm.
3. Proceed phase by phase, reviewing at each stop point.

The first vertical slice that proves the whole architecture is:
**register a patient → book a doctor → run an OPD consultation → close it by staff
action → generate an invoice.** Once that works end-to-end, everything else is
filling in modules against a proven backbone.
