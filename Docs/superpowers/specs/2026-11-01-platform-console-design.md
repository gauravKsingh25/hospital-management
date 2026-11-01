# Platform owner console & invite-only hospital admins — design

Date: 2026-11-01 · Status: draft for approval

## 1. Intent

The owner of this software (the distributor, today Gaurav, holding
`PLATFORM_ADMIN`) sells it to hospitals. Today there is no screen for that job:
a hospital and its first admin are created by calling the API or running
`backend/scripts/create_admin.py`, and the admin's password is typed by
whoever creates the account. A platform admin who signs in lands on
`/reception`, which is meaningless for them.

We are building:

1. **A platform console**, an area of the app only the platform team sees, to
   run the business of onboarding and looking after hospitals.
2. **Invite-only access for hospital admins (and platform team members).** The
   platform owner never sets or sees anyone's password. An invited person gets
   an email from `gauravsingh10.vns@gmail.com`, opens a one-time link, sets
   their own password and is signed straight in.
3. **A permanent record** of every invite and every email the platform sends.

Success: Gaurav can onboard a new hospital (create it + invite its admin) in
under a minute from the console, see at a glance which hospitals are healthy
and which need a nudge, and answer "did the invite go out, and did they
accept?" without opening a database.

## 2. Goals, non-goals, anti-goals

**Goals**
- Platform console: Overview, Hospitals (list + detail), Invites, Emails sent,
  Activity log, Platform team.
- Create hospital and invite its first admin in one step; invite further admins
  to an existing hospital.
- Invite lifecycle: pending → accepted / expired / revoked; resend; revoke.
- Real email delivery via Gmail SMTP; every send logged with its outcome.
- Platform team roles with less power than the owner.
- Suspending a hospital actually stops its users from signing in or using
  existing sessions (it does not today; see §6.5).
- UI matches the existing app (same shell, shadcn/ui, Tailwind v4 tokens,
  light + dark, en + hi) and is verified visually before it is called done.

**Non-goals (this round)**
- Inviting hospital **staff** by email. The hospital admin keeps creating staff
  as today (temporary password + forced change). Can reuse this machinery later.
- Billing/subscriptions/plans for hospitals, per-hospital feature switches.
- Wiring SMTP into the patient-notification EMAIL channel. The SMTP sender is
  written so it can be, but patient email stays on the console gateway.
- Self-service signup of any kind.

**Anti-goals (would be a failure even if it "works")**
- The owner ever seeing, choosing, or receiving another person's password.
- An invite link that works twice, works after expiry/revoke, or whose raw
  token is stored in the database or written to logs.
- A platform-team member reading patient data. The console shows counts, never
  patient records.
- The Gmail app password in code, in git, or in a log line.
- A console UI that looks bolted on (different look, unreadable contrast,
  broken at phone width).

## 3. Constraints (from CLAUDE.md and the codebase)

- Modular monolith; only `identity` and `tenancy` may work across tenants.
  Invites and the platform email log therefore live in `identity`.
- Every schema change is an Alembic migration. New tables are platform tables
  (no RLS tenant policy), like `hospitals` and `users`.
- RBAC is data-driven: new permissions/roles are seeded rows, checked by
  FastAPI dependencies on every route; frontend nav filters by permission.
- Every sensitive action writes the append-only audit log.
- Every list endpoint is paginated.
- Pydantic v2 on all I/O; typed domain exceptions; thin routers.
- Frontend: Next.js 16 App Router, server components by default, shadcn/ui,
  TanStack Query, React Hook Form + Zod, next-intl. No new heavy deps.
- Secrets are env-driven; `.env.example` documents every new variable.
- Existing auth: argon2 hashing, `PASSWORD_MIN_LENGTH` policy, JWT access +
  refresh via the BFF cookie flow, 2FA required for roles in
  `TWO_FACTOR_REQUIRED_ROLES`.

## 4. Who can do what (platform roles)

| Permission (new) | PLATFORM_ADMIN (owner) | PLATFORM_SUPPORT (new role) |
|---|---|---|
| `platform:console` — open the console, Overview | ✓ | ✓ |
| `platform:hospital_read` — list/view hospitals, usage counts | ✓ | ✓ |
| `platform:hospital_manage` — create/edit/suspend/reactivate | ✓ | — |
| `platform:invite_read` — invites + email log | ✓ | ✓ |
| `platform:invite_send` — invite hospital admins, resend | ✓ | ✓ (resend only) |
| `platform:invite_revoke` | ✓ | — |
| `platform:team_manage` — invite/deactivate platform team | ✓ | — |
| `platform:audit_read` — activity log | ✓ | ✓ |

*My call:* support can resend an existing invite (the common "I didn't get the
email" call) but cannot create hospitals or invite new people. Implemented as
`platform:invite_send` for new invites and `platform:invite_resend` for resends
if the builder finds one permission too coarse; the matrix above is the intent.

`PLATFORM_SUPPORT` is cross-tenant like `PLATFORM_ADMIN` (no `hospital_id`)
but gets **no** tenant-data permissions. The existing guard that keeps
`PLATFORM_ADMIN` off hospital-scoped users extends to every `PLATFORM_*` role.

**Invite-only rule:** a *new account* holding `HOSPITAL_ADMIN` or any
`PLATFORM_*` role can only come into existence by accepting an invite.
`POST /users` rejects those role codes (and `assign-roles` rejects granting
`PLATFORM_*`). Granting `HOSPITAL_ADMIN` to an *existing* staff member of the
same hospital by that hospital's admin stays allowed (a deputy). The rule is
enforced in `identity.service.create_user` (not just the router), with a
keyword-only escape used by exactly two callers: `accept_invite`, and the
`create_admin.py` bootstrap script for the very first `PLATFORM_ADMIN`. The
script's `--role HOSPITAL_ADMIN --hospital-code` option is removed; its
docstring points to the console invite instead.

## 5. User-facing flows

### 5.1 Onboard a hospital (owner)
Hospitals → **New hospital & invite admin** → a dialog with two short sections:
- Hospital: name, code (suggested from name/city, editable, immutable after),
  city, state, phone. Everything else (GSTIN, address, HFR ID, logo) is
  optional and editable later on the hospital page.
- First admin: full name, email, phone (optional).

Submit → hospital row created (active), invite created, email sent, dialog
closes on the new hospital's page showing the invite as *Pending · Sent*. If the
email fails, the hospital and invite still exist; the page shows *Email failed*
with the error and a **Resend** button. One transaction for hospital + invite;
the email is sent after commit.

### 5.2 Invite another admin to an existing hospital
Hospital page → Admins → **Invite admin** → name, email → same as above.

### 5.3 Accept an invite (invited person)
Email (from "<MAIL_FROM_NAME> <gauravsingh10.vns@gmail.com>"), subject
"You're invited to manage {Hospital name}", plain-text + simple HTML body, one
button/link to `{APP_PUBLIC_URL}/invite/{token}`, expiry stated.

`/invite/{token}` (public, no sign-in):
- Valid → "Welcome, {name}. You'll be the admin of {hospital}." Email shown
  read-only (it is their username). New password + confirm, with the live
  password-policy hint. **Create my account**.
- On submit → user created with the invited role(s), `must_change_password =
  false`, invite marked accepted, audit rows written, and they are **signed
  in** (same cookie/BFF path as login). If their role requires 2FA, they go
  straight into the existing 2FA enrolment step instead of the app.
- Expired / revoked / already used / unknown → one clear page: "This link is no
  longer valid. Ask {platform contact} for a new invite." No hint of which case
  for unknown tokens; expired/used may say so (they prove the token was real,
  which the holder already knows).
- Email already belongs to an active account → refused with a clear message;
  owner sees the invite fail at creation time instead (checked up front).

### 5.4 Resend / revoke (owner, support for resend)
Resend: issues a **new** token, revokes the old one, extends expiry, sends a new
email. Revoke: the link stops working immediately. Both audited.

### 5.5 Platform team
Platform team page: list of platform users (name, email, role, 2FA, last
login, active). **Invite team member** (owner only) → name, email, role
(`PLATFORM_ADMIN` or `PLATFORM_SUPPORT`) → same invite flow, no hospital.
Deactivate a team member (owner only; cannot deactivate yourself or the last
active `PLATFORM_ADMIN`).

### 5.6 Suspend / reactivate a hospital (owner)
Confirmation dialog naming the hospital and the number of active users who
will be locked out. On suspend: all of that hospital's users are refused at
login *and* on their next request with a live token, with the message "This
hospital's access is suspended. Contact {platform contact}." Refresh tokens for
that hospital are revoked. Pending invites for it can't be accepted while
suspended. Reactivate restores access (users sign in again). No data deleted.

## 6. Backend design

### 6.1 Tables (all in `identity/models.py`, platform-level, UUID PKs, timestamps)

**`invites`**
- `id`, `created_at`, `updated_at`
- `email` (normalised lowercase), `full_name`, `phone` (nullable)
- `hospital_id` (FK hospitals, nullable — NULL for platform-team invites), indexed
- `role_codes` (array/JSON of role codes granted on acceptance)
- `token_hash` (SHA-256 hex of the raw token, unique) — raw token never stored
- `status` enum: `PENDING`, `ACCEPTED`, `REVOKED`, `EXPIRED`
  (`EXPIRED` is set lazily on read/accept and by a sweep; checks always compare
  `expires_at` too, so a stale `PENDING` row can never be accepted)
- `expires_at`, `accepted_at`, `revoked_at`, `last_sent_at`, `send_count`
- `invited_by_id` (FK users), `accepted_user_id` (FK users, nullable),
  `revoked_by_id` (nullable), `superseded_by_id` (self-FK, set on resend)
- Partial unique index: one `PENDING` invite per (lower(email)) — a second
  invite to the same address must revoke/resend the first.

**`platform_emails`** (the "messages" record the user asked for)
- `id`, `created_at`
- `kind` enum: `INVITE`, `INVITE_RESEND` (extensible: `PASSWORD_RESET`, …)
- `to_email`, `from_email`, `subject`, `body_text` (stored; HTML is rendered
  from the same template and not stored — *my call*, keeps rows small and the
  text is what anyone needs to read back)
- `invite_id` (FK, nullable), `hospital_id` (FK, nullable) for filtering
- `status` enum: `SENT`, `FAILED`; `error` (text, nullable, SMTP reply code +
  message, never credentials); `provider_message_id` (Message-ID header)
- `sent_by_id` (FK users), `sent_at`
- Append-only in practice: no update/delete endpoints. The **link token is
  redacted** in the stored body (`/invite/••••`), so the log is safe to show to
  support staff.

Indexes on every FK, `status`, `created_at`, `to_email`.

### 6.2 Email sending

- New `app/core/mail.py`: `Mailer` protocol with `send(EmailMessage) ->
  MailResult` (returns failure, never raises — same contract as notification
  gateways). Implementations: `SmtpMailer` (stdlib `smtplib` + `email.message`
  run via `asyncio.to_thread`, STARTTLS on 587, timeout) and `ConsoleMailer`
  (logs the message — the token *is* printed here so local dev can click it;
  console mailer refuses to run when `ENVIRONMENT=production`). Chosen by
  `MAIL_BACKEND`. *My call:* stdlib over `aiosmtplib` — no new dependency, a
  thread is fine for a handful of emails a day.
- Sent **inline after commit**, not via ARQ. *My call, trade-off:* the owner
  sees success/failure immediately in the dialog and the worker isn't a
  dependency for onboarding; cost is ~1–2 s on the request. If volume ever
  grows (staff invites), move to an ARQ job — the `platform_emails` row is
  already the outbox.
- Templates: `identity/email_templates.py`, plain Python string templates for
  subject/text/HTML, English. *My call:* invite emails in English only this
  round (the recipient's language is unknown at invite time).

**Gmail setup (one-time, by the owner):** turn on 2-Step Verification on the
Google account, create an App Password ("Mail"), put it in `.env` as
`SMTP_PASSWORD`. Gmail personal accounts cap at ~500 recipients/day — far above
need; a domain mailbox (Google Workspace, Zoho, SES) is a later env change.

### 6.3 New settings (`core/config.py` + `.env.example`)

```
MAIL_BACKEND=console            # console | smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=gauravsingh10.vns@gmail.com
SMTP_PASSWORD=                  # Gmail App Password, never committed
SMTP_STARTTLS=true
SMTP_TIMEOUT_SECONDS=20
MAIL_FROM=gauravsingh10.vns@gmail.com
MAIL_FROM_NAME="Hospital Management"   # matches the app's display name (i18n app.name)
PLATFORM_SUPPORT_CONTACT=gauravsingh10.vns@gmail.com   # shown on "link invalid" / "suspended"
APP_PUBLIC_URL=http://localhost:3000   # base for links in emails
INVITE_TTL_HOURS=48
```
`SMTP_PASSWORD` is a `SecretStr`. Startup fails fast if `MAIL_BACKEND=smtp`
and host/user/password/from are missing.

### 6.4 Service + routes

`identity/service.py` gains: `create_invite`, `resend_invite`, `revoke_invite`,
`get_invite_by_token` (public preview), `accept_invite`, `list_invites`,
`list_platform_emails`, `list_platform_team`, `deactivate_platform_user`, and
`platform_overview` (counts). `tenancy/service.py` gains
`create_hospital_with_admin_invite` orchestration (or the router composes
tenancy + identity services in one session — builder's choice, keep routers
thin).

Routes (all under `/api/v1`, all paginated lists, all audited writes):
- `GET /platform/overview`
- `GET /platform/hospitals` (+ status/search filters, admin name, staff count,
  last admin login), `POST /platform/hospitals` (hospital + first-admin invite),
  `GET /platform/hospitals/{id}` (details, admins, usage counts)
- `POST /platform/hospitals/{id}/invites`
- `GET /platform/invites` (filter by status, hospital), `POST
  /platform/invites/{id}/resend`, `POST /platform/invites/{id}/revoke`
- `GET /platform/emails` (filter by status, kind, hospital, to)
- `GET /platform/team`, `POST /platform/team/invites`,
  `POST /platform/team/{user_id}/deactivate`
- `GET /platform/audit` (audit log, platform-relevant actions + logins)
- Public: `GET /invites/{token}` (preview: name, email, hospital name, status),
  `POST /invites/{token}/accept` (password) → token pair, rate-limited like login.

Existing `/hospitals` suspend/activate endpoints are reused by the console.

**Usage counts** on the hospital page (staff, patients, visits this week) are
computed inside that one hospital's tenant context by calling the owning
modules' service functions (count-only functions added if missing) — not by
cross-tenant SQL — so the "only identity/tenancy go cross-tenant" rule holds.
The Overview page shows only identity/tenancy totals.

### 6.5 Hospital suspension actually enforced (fix)

Today `set_hospital_active(False)` only flips the flag: login and live tokens
keep working. Fix: login refuses users whose hospital is inactive
(`hospital_suspended` code); the per-request auth dependency checks it too
(cached alongside the existing per-request user check); suspending revokes the
hospital's refresh tokens; `accept_invite` refuses for a suspended hospital.
Tests cover all four.

### 6.6 Security details
- Token: 32 bytes from `secrets.token_urlsafe`, SHA-256 stored, compared by
  hash lookup. Single use (status flip + `accepted_at` in the same transaction
  as user creation, with a row lock).
- Generic responses for unknown tokens. No per-IP rate limiter (the codebase
  has none; login relies on per-account lockout): tokens are 256-bit so
  guessing is infeasible, and every failed accept is audited.
- Password checked against the existing policy (`PASSWORD_MIN_LENGTH`, etc.)
  via the same function `change_password` uses.
- Audit actions: `invite.create`, `invite.resend`, `invite.revoke`,
  `invite.accept`, `invite.accept_failed`, `email.send` (with status),
  `platform_user.deactivate`, plus the existing hospital ones.
- Platform roles still subject to 2FA via `TWO_FACTOR_REQUIRED_ROLES`
  (recommend adding `PLATFORM_SUPPORT` to the default list).

### 6.7 Expiry sweep
A platform-level ARQ cron marks `PENDING` invites past `expires_at` as
`EXPIRED`. Purely cosmetic for lists — correctness never depends on it.

## 7. Frontend design

### 7.1 Placement
- Routes under `src/app/(app)/platform/…` so the console reuses the existing
  signed-in shell (sidebar, header, user menu, idle lock, theme, locale).
  Pages: `/platform` (overview), `/platform/hospitals`,
  `/platform/hospitals/[id]`, `/platform/invites`, `/platform/emails`,
  `/platform/activity`, `/platform/team`.
- Nav: new `NAV_ITEMS` entries gated on `platform:*` permissions. For a user
  with platform permissions and no tenant permissions, the sidebar shows only
  the console items. `homePathFor` sends `PLATFORM_ADMIN`/`PLATFORM_SUPPORT` to
  `/platform`. Universal search and the camera scan button are hidden for users
  without patient permissions (scan already is).
- Public `src/app/invite/[token]/page.tsx`, styled like `/login` (same card,
  logo area, typography).
- Code lives in `src/features/platform/…`; types regenerated from OpenAPI the
  way the project already does (`export_openapi.py`, `rbac.generated`).

### 7.2 Screens (the sketches the owner approved, rendered in the real design system)
1. **Overview:** four stat tiles (active hospitals, invites pending, staff
   accounts, emails failed in last 7 days); "Needs attention" list (invites
   expiring within 24 h, failed emails, active hospitals with no admin login in
   14+ days, hospitals with no accepted admin); recent platform activity (last
   10 audit rows). Each item links to where it's fixed.
2. **Hospitals:** search (name/code/city), status filter, TanStack Table:
   hospital, code, city, status chip (Invite pending / Active / Suspended),
   admin, staff count, last admin login. Primary button "New hospital & invite
   admin".
3. **Hospital detail:** header with name, status chip, Edit, Suspend /
   Reactivate; Details card; Admins card (status, 2FA, last login, Invite
   admin); Invites & emails table for this hospital with Resend/Revoke; Usage
   card.
4. **Invites:** all invites, filter by status; Resend/Revoke row actions;
   expiry shown relative ("in 6 h", "expired 2 d ago").
5. **Emails sent:** table of `platform_emails` (when, to, subject, kind,
   status chip, error on failure); row opens a side dialog with the stored
   body.
6. **Activity:** paginated audit log filtered to platform actions and logins,
   filter by hospital/action/actor.
7. **Platform team:** members table + "Invite team member".
8. **Accept invite page:** as §5.3.

### 7.3 "The UI must be correct" — acceptance for the frontend
- Uses existing components (`Button`, `Input`, `Table`, `Dialog`, `Alert`,
  `StatusChip`, `Pager`, `Skeleton`) and theme tokens only; no hard-coded
  colours; text contrast meets WCAG AA in light and dark.
- Readable at 1440 px, 1024 px and 390 px wide with no horizontal page scroll
  (tables scroll inside their container); tap targets ≥ 44 px.
- Loading = skeletons, empty states have one sentence + the primary action,
  errors use `LoadError`.
- All strings through next-intl (en + hi keys).
- **Verified by screenshots**: every console page and the accept page captured
  in light and dark at desktop and phone width (Playwright), reviewed before
  "done". A Playwright e2e covers: owner creates hospital + invite → (console
  mailer) link → accept with password → lands signed in as hospital admin;
  expired/revoked link shows the invalid page; suspended hospital's admin
  can't sign in.

## 8. Tests (backend, pytest)
Invite create (duplicate pending email, email already a user, suspended
hospital), resend supersedes old token, revoke, accept happy path, accept
expired / revoked / used / unknown / weak password / concurrent double-accept,
invite-only rule on `POST /users` and `assign-roles`, support role permission
matrix, cannot deactivate last platform admin, suspension blocks login + live
token + refresh + accept, SMTP failure recorded as `FAILED` with error and no
secret, token redacted in stored body, overview counts.

## 9. Delivery slices (each ends with a stop-and-check per CLAUDE.md §14)
1. Backend: models + migration, mailer, invite service + public accept,
   invite-only rule, suspension fix, tests.
2. Frontend: accept-invite page + platform shell/nav/home + Hospitals list,
   detail, create-with-invite dialog. Screenshot review.
3. Overview, Invites, Emails sent, Activity, Platform team pages. Screenshot
   review + e2e.
4. Real Gmail send smoke test with the owner's App Password.

## 10. Calls I made (please check)
- `PLATFORM_SUPPORT` role and its permission set (§4).
- Hospital *staff* stay on the current create-with-password flow (§2).
- Emails sent inline after commit, stdlib SMTP, no new dependency (§6.2).
- Invite emails English-only for now (§6.2).
- 48 h single-use links; resend issues a new link and kills the old (§5.4).
- Invite tables live in `identity`, not `notifications`, because
  notifications are tenant-scoped/patient-shaped and this is platform-level.
- Stored email body has the link token redacted (§6.1).
- Suspension enforcement fix bundled in, because a console "Suspend" button
  that doesn't suspend would be a lie (§6.5).
- Emails are sent as "Hospital Management <gauravsingh10.vns@gmail.com>",
  the app's current display name; changeable via `MAIL_FROM_NAME`.
- `create_admin.py` can no longer create hospital admins with a typed
  password; it only bootstraps the first platform owner (§4).

## 11. Left to the builder
Exact column types for `role_codes`, component breakdown inside
`features/platform`, query-key naming, exact copy wording (within the intent
above), and whether the create-hospital-with-invite orchestration sits in a
service function or the router composing two services.
