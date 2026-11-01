# Platform Console & Invite-Only Admins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A platform-owner console (hospitals, invites, email log, activity, platform team) plus invite-only onboarding of hospital admins and platform staff by email over Gmail SMTP.

**Architecture:** Invites and the platform email log are platform-level tables in the `identity` module (the only modules allowed cross-tenant are `identity`/`tenancy`). A small `core/mail.py` mailer (SMTP or console) sends after commit and every send is recorded. `/platform/*` routes compose identity + tenancy services; per-hospital usage comes from `reporting` inside that hospital's tenant context. The frontend adds `/platform/*` pages inside the existing `(app)` shell and a public `/invite/[token]` page that signs the person in through a server action, like `/login`.

**Tech Stack:** FastAPI, SQLModel, Alembic, stdlib `smtplib`/`email`, pytest; Next.js 16, React 19, shadcn/ui, TanStack Query/Table, React Hook Form + Zod, next-intl, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-platform-console-design.md`

## Global Constraints

- No new runtime dependency on either side (SMTP via stdlib `smtplib` in `asyncio.to_thread`).
- Every schema change is an Alembic migration; new permissions/roles are seeded in that migration AND mirrored in `identity/rbac.py` (`tests/test_rbac_seed.py` enforces agreement).
- Raw invite token: `secrets.token_urlsafe(32)`; only `hashlib.sha256(token).hexdigest()` is stored. The raw token never appears in the DB, the audit log, or a log line — except `ConsoleMailer` output when `ENVIRONMENT != "production"`.
- Invite TTL: `INVITE_TTL_HOURS` default `48`. Single use. Resend = new token, old invite `REVOKED` with `superseded_by_id` set.
- Email sender: `"{MAIL_FROM_NAME} <{MAIL_FROM}>"`, defaults `Hospital Management` / `gauravsingh10.vns@gmail.com`. `SMTP_PASSWORD` is `SecretStr`, never logged or stored in `platform_emails.error`.
- Stored email body has the link token redacted: the URL is stored as `{APP_PUBLIC_URL}/invite/••••`.
- Invite emails English only.
- New accounts holding `HOSPITAL_ADMIN` or any `PLATFORM_*` role only via `accept_invite` (plus the two bootstrap scripts).
- Every list endpoint returns `Page[...]` via the existing `page_params`.
- Every write audited through `identity.service.record_audit`.
- Frontend: existing `components/ui/*`, theme tokens only (no hex colours), all strings in `src/i18n/messages/{en,hi}.json`, server components by default.
- Conventional commits, **no `Co-Authored-By` trailer** (user preference).
- Python files written by scripts use LF line endings.

## Review Focus

1. **Same email invited twice / invited email already has an account** — expect a clear 409 at invite time, never a second live link. (Task 4 tests.)
2. **Owner clicks Resend while the person is mid-way through the old link** — old link must now show "no longer valid", new one works. (Task 4 test `test_resend_kills_old_token`.)
3. **Two tabs submit the same invite at once** — exactly one account, the other gets "already used". (Task 4 test `test_concurrent_accept_creates_one_user`.)
4. **Suspended hospital's admin with a live 15-minute access token** — next API call refused, not just next login. (Task 6 test `test_suspended_hospital_live_token_refused`.)
5. **Gmail rejects the send (bad app password, no network)** — invite still exists, owner sees `FAILED` with a readable reason that contains no secret, and Resend works. (Task 4 test `test_smtp_failure_recorded_without_secret`.)

---

## File map

Backend (`backend/`):
- Create `app/core/mail.py` — `MailMessage`, `MailResult`, `Mailer` protocol, `SmtpMailer`, `ConsoleMailer`, `get_mailer()`.
- Modify `app/core/config.py` — mail/invite settings + validator.
- Modify `app/modules/identity/models.py` — `InviteStatus`, `Invite`, `PlatformEmailKind`, `PlatformEmailStatus`, `PlatformEmail`.
- Modify `app/modules/identity/rbac.py` — platform permissions, `PLATFORM_SUPPORT`, `PLATFORM_ROLES`, `PRIVILEGED_ROLES`.
- Create `app/modules/identity/invites.py` — invite service functions (keeps `service.py`, already 1,450 lines, from growing further; re-exported from `service.py`).
- Create `app/modules/identity/email_templates.py` — invite subject/text/html renderers.
- Create `app/modules/identity/platform_router.py` — `/platform/*` and public `/invites/*` routes.
- Modify `app/modules/identity/schemas.py` — invite/email/platform DTOs.
- Modify `app/modules/identity/service.py` — `AuthContext.is_platform_user`, invite-only guard, suspension checks.
- Modify `app/core/deps.py` — RLS bypass for platform users.
- Modify `app/modules/tenancy/service.py` — `is_hospital_active`.
- Modify `app/modules/reporting/service.py` — `hospital_usage`.
- Modify `app/main.py` (or `app/registry.py`, wherever routers are registered) — include the platform routers.
- Modify `app/workers/main.py`, `app/workers/tasks.py` — `expire_invites` cron.
- Create `alembic/versions/20261101_1100-<rev>_platform_invites_and_emails.py`.
- Modify `scripts/create_admin.py`, `scripts/seed_demo.py`.
- Tests: `tests/test_mail.py`, `tests/test_invites_service.py`, `tests/test_api_platform.py`, `tests/test_api_invites.py`, `tests/test_hospital_suspension.py`; update `tests/test_api_identity.py`, `tests/test_rbac_seed.py` if needed.

Frontend (`frontend/`):
- Regenerate `src/types/openapi.d.ts`, `src/types/rbac.generated.ts` (`npm run codegen`).
- Modify `src/components/app-shell/nav-items.ts`, `src/lib/permissions.ts`, `src/app/(app)/layout.tsx`, `src/proxy.ts`.
- Create `src/app/invite/[token]/page.tsx`, `src/features/auth/accept-invite-form.tsx`, add `acceptInvite` to `src/features/auth/actions.ts`.
- Create `src/app/(app)/platform/{page,hospitals/page,hospitals/[id]/page,invites/page,emails/page,activity/page,team/page}.tsx`.
- Create `src/features/platform/` — `overview-screen.tsx`, `hospitals-screen.tsx`, `new-hospital-dialog.tsx`, `hospital-detail-screen.tsx`, `invite-dialog.tsx`, `invites-table.tsx`, `invite-status.tsx`, `emails-screen.tsx`, `activity-screen.tsx`, `team-screen.tsx`, `use-platform.ts`.
- Modify `src/i18n/messages/en.json`, `hi.json` — `platform` and `invite` namespaces, nav labels.
- Create `e2e/platform.spec.ts`, `e2e/platform-screenshots.spec.ts`; modify `e2e/support.ts`.

Docs: `.env.example`, `README.md`, `docs/runbook.md`.

---

### Task 0: Branch

- [ ] Create `feat/platform-console` from the current `version3` HEAD: `git switch -c feat/platform-console`.

### Task 1: Mail settings and mailer

**Files:**
- Create: `backend/app/core/mail.py`
- Modify: `backend/app/core/config.py`, `.env.example`
- Test: `backend/tests/test_mail.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, kw_only=True, slots=True) class MailMessage: to: str; subject: str; text: str; html: str | None = None`
  - `@dataclass(frozen=True, kw_only=True, slots=True) class MailResult: ok: bool; message_id: str | None = None; error: str | None = None`
  - `class Mailer(Protocol): async def send(self, message: MailMessage) -> MailResult` — never raises.
  - `def get_mailer() -> Mailer` — per `settings.MAIL_BACKEND`.
  - `settings.APP_PUBLIC_URL`, `settings.INVITE_TTL_HOURS`, `settings.MAIL_FROM`, `settings.MAIL_FROM_NAME`, `settings.PLATFORM_SUPPORT_CONTACT`, `settings.MAIL_OUTBOX_DIR`.

Settings (add to `Settings`, document every one in `.env.example` with the Gmail App Password instructions as a comment):
```
MAIL_BACKEND: Literal["console", "smtp"] = "console"
SMTP_HOST: str = "smtp.gmail.com"
SMTP_PORT: int = 587
SMTP_USERNAME: str = ""
SMTP_PASSWORD: SecretStr = SecretStr("")
SMTP_STARTTLS: bool = True
SMTP_TIMEOUT_SECONDS: Annotated[int, Field(ge=1, le=120)] = 20
MAIL_FROM: str = "gauravsingh10.vns@gmail.com"
MAIL_FROM_NAME: str = "Hospital Management"
MAIL_OUTBOX_DIR: str = ""          # console only: also write each message as <ts>-<to>.eml here
PLATFORM_SUPPORT_CONTACT: str = "gauravsingh10.vns@gmail.com"
APP_PUBLIC_URL: str = "http://localhost:3000"
INVITE_TTL_HOURS: Annotated[int, Field(ge=1, le=336)] = 48
```
In `.env.example` set `MAIL_BACKEND=console`, `MAIL_OUTBOX_DIR=.mail-outbox` and add `.mail-outbox/` to `.gitignore`.

- [ ] **Step 1: Write failing tests** in `tests/test_mail.py`:
  - `test_smtp_backend_requires_credentials`: `Settings(MAIL_BACKEND="smtp", SMTP_USERNAME="", ...)` raises `ValidationError` mentioning `SMTP_PASSWORD`.
  - `test_console_backend_refused_in_production`: `Settings(ENVIRONMENT="production", MAIL_BACKEND="console", ...)` raises `ValidationError`.
  - `test_console_mailer_writes_outbox(tmp_path)`: with `MAIL_OUTBOX_DIR=tmp_path`, `await ConsoleMailer().send(MailMessage(to="a@b.in", subject="S", text="hello http://x/invite/T"))` → `result.ok is True`, one `.eml` file in `tmp_path` containing `"hello http://x/invite/T"` and `"Subject: S"`.
  - `test_smtp_failure_returns_result_without_secret(monkeypatch)`: monkeypatch `smtplib.SMTP` to raise `smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")`; settings with `SMTP_PASSWORD="app-pass-123"` → `result.ok is False`, `"535" in result.error`, `"app-pass-123" not in result.error`.
  - `test_smtp_mailer_sends_starttls(monkeypatch)`: fake SMTP class records calls → `starttls`, `login(SMTP_USERNAME, "app-pass-123")`, `send_message` called once; message `From` is `"Hospital Management <gauravsingh10.vns@gmail.com>"`; `result.message_id` equals the message's `Message-ID` header.
- [ ] **Step 2:** `cd backend && pytest tests/test_mail.py -v` → FAIL (import error).
- [ ] **Step 3:** Implement settings + `model_validator` in `config.py`; implement `mail.py`. `SmtpMailer.send` builds an `email.message.EmailMessage` (text part, plus `add_alternative(html, subtype="html")` when given; `Message-ID` via `email.utils.make_msgid(domain=MAIL_FROM domain)`), runs the blocking send in `asyncio.to_thread`, catches `(smtplib.SMTPException, OSError)` and returns `MailResult(ok=False, error=f"{type(e).__name__}: {e}"[:500])`. `ConsoleMailer` logs subject/to/text at INFO and writes the `.eml` when `MAIL_OUTBOX_DIR` is set.
- [ ] **Step 4:** `pytest tests/test_mail.py tests/test_config.py -v` → PASS.
- [ ] **Step 5:** Commit `feat(mail): SMTP and console mailers with env-driven settings`.

### Task 2: Models, permissions, PLATFORM_SUPPORT role, migration

**Files:**
- Modify: `backend/app/modules/identity/models.py`, `backend/app/modules/identity/rbac.py`, `backend/app/modules/identity/service.py` (`AuthContext`, `assign_roles` guard), `backend/app/core/deps.py`
- Create: `backend/alembic/versions/20261101_1100-<rev>_platform_invites_and_emails.py` (down_revision `f2b5d8e0a3c1`)
- Test: `backend/tests/test_rbac_seed.py` (existing, must stay green), `backend/tests/test_identity_service.py`

**Interfaces:**
- Produces:
  - `class InviteStatus(StrEnum): PENDING, ACCEPTED, REVOKED, EXPIRED`
  - `class Invite(IdentifiedModel, table=True)`, table `invites`: `email: str (indexed)`, `full_name: str`, `phone: str | None`, `hospital_id: UUID | None` (FK `hospitals.id`, RESTRICT, indexed), `role_codes: list[str]` (Postgres `ARRAY(String)`), `token_hash: str` (unique, 64), `status: InviteStatus` (indexed, default PENDING), `expires_at`, `accepted_at | None`, `revoked_at | None`, `last_sent_at | None`, `send_count: int = 0`, `invited_by_id` (FK users), `accepted_user_id | None`, `revoked_by_id | None`, `superseded_by_id | None` (FK invites). Partial unique index `uq_invites_pending_email` on `lower(email)` WHERE `status = 'PENDING'`.
  - `class PlatformEmailKind(StrEnum): INVITE, INVITE_RESEND`; `class PlatformEmailStatus(StrEnum): SENT, FAILED`
  - `class PlatformEmail(UUIDMixin, table=True)`, table `platform_emails`: `created_at`, `kind`, `to_email (indexed)`, `from_email`, `subject`, `body_text`, `invite_id | None` (FK, indexed), `hospital_id | None` (FK, indexed), `status (indexed)`, `error | None`, `provider_message_id | None`, `sent_by_id | None` (FK users), `sent_at`.
  - Neither table gets an RLS policy (platform tables, like `users`).
  - `Permissions.PLATFORM_CONSOLE = "platform:console"`, `PLATFORM_HOSPITAL_READ = "platform:hospital_read"`, `PLATFORM_HOSPITAL_MANAGE = "platform:hospital_manage"`, `PLATFORM_INVITE_READ = "platform:invite_read"`, `PLATFORM_INVITE_SEND = "platform:invite_send"`, `PLATFORM_INVITE_RESEND = "platform:invite_resend"`, `PLATFORM_INVITE_REVOKE = "platform:invite_revoke"`, `PLATFORM_TEAM_MANAGE = "platform:team_manage"`, `PLATFORM_AUDIT_READ = "platform:audit_read"` (module `"identity"`).
  - `Roles.PLATFORM_SUPPORT = "PLATFORM_SUPPORT"` ("Platform Support", "Looks after hospitals on the platform; read-only plus resending invites.").
  - `PLATFORM_ROLES: Final = frozenset({Roles.PLATFORM_ADMIN, Roles.PLATFORM_SUPPORT})`; `PRIVILEGED_ROLES: Final = PLATFORM_ROLES | {Roles.HOSPITAL_ADMIN}`.
  - `SYSTEM_ROLE_PERMISSIONS[PLATFORM_SUPPORT] = (PLATFORM_CONSOLE, PLATFORM_HOSPITAL_READ, PLATFORM_INVITE_READ, PLATFORM_INVITE_RESEND, PLATFORM_AUDIT_READ)`; PLATFORM_ADMIN gets all nine (already true via catalogue).
  - `AuthContext.is_platform_user -> bool` (any `PLATFORM_ROLES`). `deps.get_auth_context` bypasses RLS when `is_platform_user`. `is_platform_admin` and `resolve_target_hospital` unchanged.
  - `assign_roles` refuses any `PLATFORM_ROLES` code for a user with `hospital_id is not None` (generalises the existing PLATFORM_ADMIN check, same error code).
  - Default `two_factor_roles` (non-local) adds `"PLATFORM_SUPPORT"`.

- [ ] **Step 1: Write failing tests** in `tests/test_identity_service.py`:
  - `test_platform_support_has_only_console_permissions`: create user with `PLATFORM_SUPPORT` (via `_make_user`), `build_auth_context` → `permissions == {the five codes}`, `is_platform_user is True`, `is_platform_admin is False`.
  - `test_platform_support_cannot_be_granted_to_hospital_user`: `assign_roles(session, receptionist, [Roles.PLATFORM_SUPPORT])` raises the same error type as the existing PLATFORM_ADMIN case (line ~349).
- [ ] **Step 2:** `pytest tests/test_identity_service.py -k platform_support -v` → FAIL.
- [ ] **Step 3:** Implement models, rbac, guard, deps; write the migration (create both tables + indexes + partial unique index; insert 9 permissions, `PLATFORM_SUPPORT` role, role_permissions for PLATFORM_ADMIN (all 9) and PLATFORM_SUPPORT (5); downgrade reverses, following `20261019_1000-a1c4e7b9d2f0_accountant_role_and_ipd_billing.py`).
- [ ] **Step 4:** `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` against the local DB → no errors. `pytest tests/test_identity_service.py tests/test_rbac_seed.py tests/test_models.py -v` → PASS.
- [ ] **Step 5:** `python scripts/export_rbac.py` (regenerates the frontend RBAC list if that is what it does; check its docstring) and commit `feat(identity): invite and platform email tables, platform permissions and PLATFORM_SUPPORT role`.

### Task 3: Invite-only rule for privileged roles

**Files:**
- Modify: `backend/app/modules/identity/service.py` (`create_user`, `assign_roles`), `backend/scripts/create_admin.py`, `backend/scripts/seed_demo.py`
- Test: `backend/tests/test_api_identity.py`, `backend/tests/test_identity_service.py`

**Interfaces:**
- Produces: `create_user(session, payload, *, actor=None, hospital_id=None, must_change_password=True, allow_privileged_roles: bool = False) -> User`. Without the flag, any `payload.role_codes ∩ PRIVILEGED_ROLES` raises `PermissionDeniedError("Administrator accounts are created by invitation.", code="invite_required")`.
- `assign_roles` raises the same for any `PLATFORM_ROLES` code always (platform roles only via invite), and for `HOSPITAL_ADMIN` only when the target user has `hospital_id is None`. Granting `HOSPITAL_ADMIN` to an existing same-hospital staff member stays allowed.
- `create_admin.py`: remove `--role`/`--hospital-code`; always `PLATFORM_ADMIN`, `allow_privileged_roles=True`; docstring points hospital admins to the console invite.
- `seed_demo.py`: pass `allow_privileged_roles=True`; add `("platform@demo.hospital", "Gaurav Singh", Roles.PLATFORM_ADMIN)` created with `hospital_id=None` (demo platform owner for e2e).

- [ ] **Step 1: Write failing tests:**
  - `test_api_identity.py::test_create_user_refuses_hospital_admin_role`: hospital admin POSTs `/api/v1/users` with `role_codes=["HOSPITAL_ADMIN"]` → 403, `body["code"] == "invite_required"`. (Update the existing test at line ~111 that expects a HOSPITAL_ADMIN to be created this way: it now asserts this 403.)
  - `test_create_user_refuses_platform_role_for_platform_admin`: platform admin POSTs with `["PLATFORM_SUPPORT"]` → 403 `invite_required`.
  - `test_identity_service.py::test_existing_staff_can_be_promoted_to_hospital_admin`: `assign_roles(session, receptionist, [Roles.RECEPTIONIST, Roles.HOSPITAL_ADMIN], actor=hospital_admin)` succeeds.
- [ ] **Step 2:** run them → FAIL.
- [ ] **Step 3:** Implement; update both scripts.
- [ ] **Step 4:** `pytest tests/test_api_identity.py tests/test_identity_service.py -v` → PASS; `python scripts/seed_demo.py --help` (or a dry run per its docstring) runs without import errors.
- [ ] **Step 5:** Commit `feat(identity): administrator accounts only by invitation`.

### Task 4: Invite service, email templates, email log

**Files:**
- Create: `backend/app/modules/identity/invites.py`, `backend/app/modules/identity/email_templates.py`
- Modify: `backend/app/modules/identity/service.py` (re-export), `backend/app/modules/tenancy/service.py` (`is_hospital_active`)
- Test: `backend/tests/test_invites_service.py`

**Interfaces:**
- Consumes: `get_mailer()`, `MailMessage`, `MailResult` (Task 1); `Invite`, `PlatformEmail`, enums, `PRIVILEGED_ROLES` (Task 2); `create_user(..., allow_privileged_roles=True)` (Task 3); `login(...)` (existing).
- Produces (all in `invites.py`; every function that writes calls `record_audit`):
  - `async def create_invite(session, *, email: str, full_name: str, phone: str | None, hospital_id: UUID | None, role_codes: list[str], actor: User) -> tuple[Invite, str]` — returns raw token; validates role set (hospital invite ⇒ `["HOSPITAL_ADMIN"]` and `hospital_id` required and active; team invite ⇒ exactly one of `PLATFORM_ROLES` and `hospital_id is None`); 409 `email_taken` if a user exists; 409 `invite_pending` if a pending, unexpired invite exists for that email. Audit `invite.create`.
  - `async def send_invite_email(session, invite: Invite, raw_token: str, *, kind: PlatformEmailKind, actor: User) -> PlatformEmail` — renders, sends via mailer, writes `PlatformEmail` (redacted body), sets `last_sent_at`, `send_count += 1`. Audit `email.send` with `{"status": ...}`. Called by routers **after** the invite commit.
  - `async def resend_invite(session, invite_id: UUID, *, actor: User) -> tuple[Invite, str]` — only from PENDING/EXPIRED; creates a new invite (same email/name/hospital/roles, fresh expiry), marks old `REVOKED` with `superseded_by_id`. Audit `invite.resend`.
  - `async def revoke_invite(session, invite_id: UUID, *, actor: User) -> Invite` — only from PENDING. Audit `invite.revoke`.
  - `async def get_invite_by_token(session, raw_token: str) -> Invite` — `NotFoundError(code="invite_invalid")` for unknown; lazily sets EXPIRED when `expires_at <= now`.
  - `@dataclass class InvitePreview: full_name: str; email: str; hospital_name: str | None; status: InviteStatus; expires_at: datetime`
  - `async def preview_invite(session, raw_token: str) -> InvitePreview`
  - `async def accept_invite(session, raw_token: str, *, password: str, ip_address: str | None, user_agent: str | None) -> tuple[User, str, str, int]` — `SELECT ... FOR UPDATE` on the invite; refuses non-PENDING / expired (`ConflictError(code="invite_used" | "invite_revoked" | "invite_expired")`), suspended hospital (`PermissionDeniedError(code="hospital_suspended")`); `create_user(..., must_change_password=False, allow_privileged_roles=True)`; marks ACCEPTED + `accepted_user_id`; audit `invite.accept`; then returns `await login(session, email=..., password=password, ...)` (so `TwoFactorChallenge` propagates exactly as for login). Failed attempts audit `invite.accept_failed` with the reason.
  - `async def list_invites(session, *, status: InviteStatus | None, hospital_id: UUID | None, params: PageParams) -> Page[Invite]`; `async def list_platform_emails(session, *, status, kind, hospital_id, to: str | None, params) -> Page[PlatformEmail]`.
  - `async def expire_due_invites(session) -> int`.
  - `email_templates.render_invite(*, full_name: str, hospital_name: str | None, role_label: str, url: str, expires_at: datetime, support_contact: str) -> tuple[str, str, str]` (subject, text, html). Subject: `You're invited to manage {hospital_name}` for hospital invites, `You're invited to the Hospital Management platform team` for team invites. Expiry rendered in Asia/Kolkata, e.g. `3 Nov 2026, 2:15 pm IST`. HTML: one table-based block, inline styles, a single button link; no images.
  - `tenancy.service.is_hospital_active(session, hospital_id: UUID) -> bool`.

- [ ] **Step 1: Write failing tests** in `tests/test_invites_service.py` (monkeypatch `get_mailer` to a fake recording messages and returning `MailResult(ok=True, message_id="<m1>")` unless a test says otherwise):
  - `test_create_invite_stores_hash_not_token`: `invite.token_hash == sha256(token)`; `token not in` any column of the row; `invite.expires_at - now ≈ 48h`.
  - `test_duplicate_pending_invite_conflicts`: second `create_invite` same email (different case) → `ConflictError` code `invite_pending`.
  - `test_invite_for_existing_user_conflicts` → code `email_taken`.
  - `test_invite_to_suspended_hospital_refused` → code `hospital_suspended`.
  - `test_send_records_email_with_redacted_link`: one `PlatformEmail`, `status == SENT`, `provider_message_id == "<m1>"`, raw token in the fake mailer's text but `token not in email.body_text` and `"/invite/••••" in email.body_text`.
  - `test_smtp_failure_recorded_without_secret`: fake returns `MailResult(ok=False, error="SMTPAuthenticationError: (535, ...)")` → row `status == FAILED`, `error` starts with `SMTPAuthenticationError`, invite still `PENDING`.
  - `test_accept_creates_signed_in_hospital_admin`: accept with a valid password → user has `hospital_id == hospital.id`, roles `["HOSPITAL_ADMIN"]`, `must_change_password is False`; invite `ACCEPTED`; returns a non-empty access token.
  - `test_accept_twice_refused` → second call code `invite_used`.
  - `test_accept_expired_refused`: set `expires_at` to past → code `invite_expired`, invite now `EXPIRED`.
  - `test_accept_revoked_refused` → code `invite_revoked`.
  - `test_accept_refused_when_hospital_suspended`: suspend after invite → code `hospital_suspended`, no user created.
  - `test_accept_weak_password_refused`: `"short"` → the existing password-policy error; invite still `PENDING`, no user.
  - `test_unknown_token_is_invalid` → `NotFoundError` code `invite_invalid`.
  - `test_resend_kills_old_token`: `resend_invite` → old `REVOKED`, `old.superseded_by_id == new.id`; accepting old token → `invite_revoked`; new token works.
  - `test_concurrent_accept_creates_one_user`: two sessions, `asyncio.gather` two `accept_invite` with the same token → exactly one succeeds, one raises `ConflictError`; `users` with that email count == 1.
  - `test_team_invite_requires_platform_role_and_no_hospital`: `role_codes=["DOCTOR"]` with `hospital_id=None` → `ValidationError`-type domain error.
  - `test_accept_with_two_factor_required_raises_challenge(monkeypatch)`: `settings.TWO_FACTOR_REQUIRED_ROLES=["HOSPITAL_ADMIN"]` → `TwoFactorChallenge` raised, user created, invite `ACCEPTED`.
- [ ] **Step 2:** `pytest tests/test_invites_service.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest tests/test_invites_service.py -v` → PASS.
- [ ] **Step 5:** Commit `feat(identity): invite lifecycle with logged email delivery`.

### Task 5: Public invite routes and platform invite routes

**Files:**
- Create: `backend/app/modules/identity/platform_router.py`
- Modify: `backend/app/modules/identity/schemas.py`, router registration (`app/main.py` / `app/registry.py`)
- Test: `backend/tests/test_api_invites.py`

**Interfaces:**
- Consumes: Task 4 functions.
- Produces routes and schemas:
  - `GET /api/v1/invites/{token}` → `InvitePreviewRead {full_name, email, hospital_name, status, expires_at}`; unknown → 404 `invite_invalid`.
  - `POST /api/v1/invites/{token}/accept` body `InviteAccept {password: str}` → `TokenPair | TwoFactorChallengeRead` (same shapes as `/auth/login`).
  - `POST /api/v1/platform/hospitals/{hospital_id}/invites` body `HospitalAdminInviteCreate {full_name, email, phone?}` → 201 `InviteRead`; requires `platform:invite_send`.
  - `GET /api/v1/platform/invites?status=&hospital_id=` → `Page[InviteRead]`; `platform:invite_read`.
  - `POST /api/v1/platform/invites/{id}/resend` → `InviteRead` (the new one); `platform:invite_resend` **or** `platform:invite_send`.
  - `POST /api/v1/platform/invites/{id}/revoke` → `InviteRead`; `platform:invite_revoke`.
  - `GET /api/v1/platform/emails?status=&kind=&hospital_id=&to=` → `Page[PlatformEmailRead]`; `platform:invite_read`.
  - `InviteRead {id, email, full_name, phone, hospital_id, hospital_name, role_codes, status, expires_at, accepted_at, revoked_at, last_sent_at, send_count, invited_by_name, last_email_status: PlatformEmailStatus | None, last_email_error: str | None, created_at}`.
  - `PlatformEmailRead {id, kind, to_email, from_email, subject, body_text, hospital_id, hospital_name, invite_id, status, error, provider_message_id, sent_by_name, sent_at}`.
  - Write routes commit the invite, then call `send_invite_email`, then commit again — so a mail failure never rolls back the invite.

- [ ] **Step 1: Write failing tests** in `tests/test_api_invites.py` (fake mailer fixture captures the raw token from the text):
  - `test_owner_invites_admin_then_admin_signs_in`: platform admin POSTs invite → 201, `last_email_status == "SENT"`; `GET /invites/{token}` → 200 with `hospital_name`; `POST .../accept` → 200 with `access_token`; `GET /auth/me` with it → roles `["HOSPITAL_ADMIN"]`.
  - `test_preview_unknown_token_404`.
  - `test_support_can_resend_but_not_invite_or_revoke`: support user: POST invite → 403; resend → 200; revoke → 403.
  - `test_hospital_admin_cannot_reach_platform_routes`: `GET /platform/invites` → 403.
  - `test_emails_list_filters_failed`: one failed send → `GET /platform/emails?status=FAILED` returns it with `error` set.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest tests/test_api_invites.py -v` → PASS.
- [ ] **Step 5:** Commit `feat(identity): invite and platform email API`.

### Task 6: Suspending a hospital actually suspends it

**Files:**
- Modify: `backend/app/modules/identity/service.py` (`authenticate`, `user_from_access_token`, `rotate_refresh_token`), `backend/app/modules/tenancy/router.py` (deactivate route revokes refresh tokens via identity service)
- Test: `backend/tests/test_hospital_suspension.py`

**Interfaces:**
- Consumes: `tenancy.service.is_hospital_active` (Task 4).
- Produces: `AuthenticationError("This hospital's access is suspended. Contact {PLATFORM_SUPPORT_CONTACT}.", code="hospital_suspended")` from login (after the password check, audited with reason `hospital_suspended`), from `user_from_access_token`, and from refresh; `identity.service.revoke_hospital_sessions(session, hospital_id: UUID) -> int` called by the deactivate route.

- [ ] **Step 1: Write failing tests:**
  - `test_suspended_hospital_login_refused`: deactivate hospital → login hospital admin → 401, code `hospital_suspended`.
  - `test_suspended_hospital_live_token_refused`: login first, then deactivate via API as platform admin → `GET /auth/me` with old token → 401 `hospital_suspended`.
  - `test_suspended_hospital_refresh_revoked`: refresh with pre-suspension refresh token → 401.
  - `test_reactivated_hospital_can_sign_in`.
  - `test_platform_admin_unaffected_by_hospital_suspension`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement (cheap: one primary-key lookup on `hospitals`; reuse whatever per-request cache `identity/cache.py` offers for the user/permission lookup if it fits, otherwise a plain lookup).
- [ ] **Step 4:** `pytest tests/test_hospital_suspension.py tests/test_api_identity.py tests/test_api_lockout.py -v` → PASS.
- [ ] **Step 5:** Commit `fix(tenancy): suspending a hospital blocks its sign-ins and live sessions`.

### Task 7: Platform read & management API

**Files:**
- Modify: `backend/app/modules/identity/platform_router.py`, `backend/app/modules/identity/schemas.py`, `backend/app/modules/identity/invites.py` (or a sibling `platform.py` for the overview/team queries), `backend/app/modules/reporting/service.py`
- Test: `backend/tests/test_api_platform.py`

**Interfaces:**
- Produces:
  - `GET /platform/overview` (`platform:console`) → `PlatformOverview {active_hospitals, suspended_hospitals, pending_invites, staff_accounts, emails_failed_7d, attention: list[AttentionItem], recent_activity: list[AuditLogRead]}`; `AttentionItem {kind: Literal["invite_expiring","email_failed","no_admin_login","no_admin"], hospital_id, hospital_name, invite_id?, detail: str, at: datetime | None}`. Rules: invite expiring within 24 h; failed email in last 7 d whose invite is still PENDING; active hospital whose admins' max `last_login_at` is > 14 days ago; active hospital with no active HOSPITAL_ADMIN user. `recent_activity` = last 10 audit rows with action prefix `invite.`, `hospital.`, `platform_user.`.
  - `GET /platform/hospitals?q=&status=active|suspended|invite_pending` (`platform:hospital_read`) → `Page[PlatformHospitalRow {id, code, name, city, is_active, status: Literal["active","suspended","invite_pending"], admin_name, staff_count, last_admin_login_at}]`. `invite_pending` = active hospital with no active HOSPITAL_ADMIN and ≥1 pending invite.
  - `POST /platform/hospitals` (`platform:hospital_manage`) body `HospitalWithAdminCreate {hospital: HospitalCreate, admin: HospitalAdminInviteCreate}` → 201 `PlatformHospitalDetail`; hospital + invite in one transaction, email after commit. Code conflict → existing 409.
  - `GET /platform/hospitals/{id}` (`platform:hospital_read`) → `PlatformHospitalDetail {hospital: HospitalRead, admins: list[PlatformAdminRow {id, full_name, email, is_active, two_factor_enabled, last_login_at}], invites: list[InviteRead] (latest 20), usage: HospitalUsage}`.
  - `reporting.service.hospital_usage(session, hospital_id) -> HospitalUsage {staff: int, patients: int, visits_last_7_days: int}` — run by the router inside `set_tenant_context(session, hospital_id)` (then restore bypass), counting `users` (via identity), `patients`, `encounters` with `created_at >= now - 7d`.
  - `GET /platform/team` (`platform:console`) → `Page[PlatformTeamRow {id, full_name, email, roles, is_active, two_factor_enabled, last_login_at}]` (users with any `PLATFORM_ROLES`).
  - `POST /platform/team/invites` (`platform:team_manage`) body `TeamInviteCreate {full_name, email, role: Literal["PLATFORM_ADMIN","PLATFORM_SUPPORT"]}` → 201 `InviteRead`.
  - `POST /platform/team/{user_id}/deactivate` (`platform:team_manage`) → `PlatformTeamRow`; 409 `cannot_deactivate_self`, 409 `last_platform_admin`. Audit `platform_user.deactivate`.
  - `GET /platform/audit?hospital_id=&action=&actor_id=` (`platform:audit_read`) → `Page[AuditLogRead]` (reuse `list_audit_logs`, unscoped by tenant).

- [ ] **Step 1: Write failing tests** in `tests/test_api_platform.py`:
  - `test_create_hospital_with_admin_invite`: POST → 201; hospital exists; one PENDING invite for it; one SENT email; list shows `status == "invite_pending"`.
  - `test_create_hospital_rolls_back_when_invite_invalid`: admin email already a user → 409 `email_taken`, and the hospital code does **not** exist afterwards.
  - `test_overview_counts_and_attention`: fixtures for one active hospital w/ admin last login 20 d ago and one invite expiring in 2 h → `attention` kinds include `no_admin_login` and `invite_expiring`.
  - `test_hospital_detail_usage_counts`: seed 2 patients + 1 encounter in that hospital → `usage.patients == 2`, `usage.visits_last_7_days == 1`.
  - `test_support_cannot_create_hospital` → 403.
  - `test_cannot_deactivate_last_platform_admin` → 409 `last_platform_admin`; `test_cannot_deactivate_self` → 409.
  - `test_team_invite_support_role`: POST team invite role `PLATFORM_SUPPORT` → 201, `hospital_id is None`.
  - `test_platform_lists_are_paginated`: `GET /platform/hospitals?limit=1` → `len(items) == 1`, `total >= 2`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest tests/test_api_platform.py -v` → PASS.
- [ ] **Step 5:** Commit `feat(platform): overview, hospitals, team and audit API`.

### Task 8: Invite expiry sweep

**Files:** Modify `backend/app/workers/tasks.py`, `backend/app/workers/main.py`; Test `backend/tests/test_invites_service.py`.

**Interfaces:** `async def expire_invites(ctx: dict[str, Any] | None = None) -> int` in `tasks.py`, calling `expire_due_invites` under `system_context`; registered as an hourly `cron` (minute 5) gated by `WORKER_SWEEPS_ENABLED`.

- [ ] **Step 1:** Test `test_expire_due_invites_marks_only_past_pending`: one past PENDING, one future PENDING, one past ACCEPTED → returns `1`, only the first is `EXPIRED`.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS; then full backend suite `pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat(workers): expire stale invites hourly`.

### Task 9: Frontend shell — permissions, nav, home, codegen, i18n

**Files:** `frontend/src/types/*` (codegen), `src/components/app-shell/nav-items.ts`, `src/lib/permissions.ts`, `src/app/(app)/layout.tsx`, `src/proxy.ts`, `src/i18n/messages/{en,hi}.json`.

**Interfaces:**
- Produces: nav items (icons from `lucide-react`): `/platform` "Overview" `LayoutDashboard` [`platform:console`]; `/platform/hospitals` "Hospitals" `Building2` [`platform:hospital_read`], `prefix: true`; `/platform/invites` "Invites" `MailPlus` [`platform:invite_read`]; `/platform/emails` "Emails sent" `Mail` [`platform:invite_read`]; `/platform/activity` "Activity log" `History` [`platform:audit_read`]; `/platform/team` "Platform team" `ShieldCheck` [`platform:console`]. Placed first in `NAV_ITEMS`.
- `homePathFor`: `PLATFORM_ADMIN` / `PLATFORM_SUPPORT` → `/platform` (before the HOSPITAL_ADMIN line, and only when the user has no `hospital_id`).
- `(app)/layout.tsx`: render `<UniversalSearch />` only when `can(user, "patient:read")`.
- `proxy.ts`: add `"/invite"` to `PUBLIC_PATHS`.
- i18n: `nav.platformOverview`, `nav.platformHospitals`, `nav.platformInvites`, `nav.platformEmails`, `nav.platformActivity`, `nav.platformTeam`; namespaces `platform.*` and `invite.*` filled as screens are built (en + hi in the same commit as each screen).

- [ ] **Step 1:** With the backend running, `npm run codegen`; `npm run codegen:check` → clean.
- [ ] **Step 2:** Implement the changes above; add `src/app/(app)/platform/page.tsx` as a placeholder heading so the nav target exists.
- [ ] **Step 3:** `npm run lint && npm run typecheck && npm run test:unit` → PASS.
- [ ] **Step 4:** Sign in as `platform@demo.hospital` (reseed demo) → lands on `/platform`, sidebar shows only the six console items, no search box. Sign in as `admin@demo.hospital` → unchanged sidebar, no console items.
- [ ] **Step 5:** Commit `feat(web): platform console navigation and home`.

### Task 10: Public accept-invite page

**Files:** Create `src/app/invite/[token]/page.tsx`, `src/features/auth/accept-invite-form.tsx`; modify `src/features/auth/actions.ts`, i18n.

**Interfaces:**
- Consumes: `GET /invites/{token}`, `POST /invites/{token}/accept` (Task 5).
- Produces: server action `acceptInvite(token: string, previous: AuthFormState, formData: FormData): Promise<AuthFormState>` in `actions.ts` — Zod: `password` min `PASSWORD_MIN_LENGTH` (12, mirror backend), `confirmPassword` equal; calls the accept endpoint via `callApi`; on `TokenPair` → `writeSession` + delete lock cookie + `redirect("/")`; on challenge → same 2FA cookie + redirect as `signIn` (extract the shared tail of `signIn` into a local helper `finishSignIn(store, result, next)` and use it from both). Error codes `invite_used|invite_revoked|invite_expired|invite_invalid|hospital_suspended` → localized messages.
- Page (server component): fetches preview with `callApi`; valid → card identical in structure to `/login` (logo tile, app name, `bg-card rounded-xl border p-6 shadow-sm`), heading `Welcome, {full_name}`, line `You'll be the administrator of {hospital_name}` (team: `You're joining the platform team`), read-only email field, password + confirm with the policy hint, primary button `Create my account`. Invalid/expired/used → same card with an `Alert` and `Ask {PLATFORM_SUPPORT_CONTACT} for a new invite.` (contact comes from the preview error payload or a public config value; do not hard-code it in the frontend).

- [ ] **Step 1:** Implement; `npm run lint && npm run typecheck`.
- [ ] **Step 2:** Manual: create an invite via `/docs` or curl as platform admin; open the link from `backend/.mail-outbox/*.eml`; set password → lands signed in as hospital admin on `/reception`. Reopen the link → "no longer valid".
- [ ] **Step 3:** Commit `feat(web): accept-invite page signs the new admin in`.

### Task 11: Hospitals list, create dialog, hospital detail

**Files:** `src/app/(app)/platform/hospitals/page.tsx`, `.../hospitals/[id]/page.tsx`; `src/features/platform/{use-platform.ts,hospitals-screen.tsx,new-hospital-dialog.tsx,hospital-detail-screen.tsx,invite-dialog.tsx,invites-table.tsx,invite-status.tsx}`; i18n.

**Interfaces:**
- `use-platform.ts`: query keys `["platform", "hospitals" | "hospital" | "invites" | "emails" | "overview" | "team" | "audit"]`; thin wrappers over `useAdminList`/`useAdminMutation` from `features/admin/use-admin-resource.ts` (reuse, don't copy).
- `invite-status.tsx`: `<InviteStatus status expiresAt />` and `<HospitalStatus status />` built on `components/status-chip.tsx` tones (pending = warning, active = success, suspended/failed = destructive, expired/revoked = muted).
- `invites-table.tsx`: `<InvitesTable invites canResend canRevoke />` reused by hospital detail and the Invites page; row actions Resend / Revoke (revoke behind a `Dialog` confirm).
- Hospitals screen: matches approved sketch 2 — search input + status `Select` + primary `Button` "New hospital & invite admin"; TanStack Table columns Hospital, Code, City, Status, Admin, Staff, Last login (relative, via existing `lib/duration.ts` if it formats relative time); row click → detail; `Pager`; `Skeleton` rows while loading; empty state sentence + the primary button.
- New hospital dialog: React Hook Form + Zod; section "Hospital" (name, code — suggested as first 3 letters of city + 2 digits, uppercase, editable; city; state; phone) and "First administrator" (full name, email, phone optional); submit → `POST /platform/hospitals` → toast "Invite sent to {email}" or, if `last_email_status === "FAILED"`, a warning toast "Hospital created, but the email failed" → navigate to detail.
- Detail screen: matches sketch 3 — header (name, `HospitalStatus`, Edit → existing hospital PATCH in a dialog, Suspend/Reactivate behind a confirm `Dialog` stating "{n} active users will be signed out"); two-column cards Details / Admins (+ "Invite admin" → `invite-dialog.tsx`); Invites & emails (`InvitesTable`); Usage. Buttons hidden when the user lacks the permission (`can()`).

- [ ] **Step 1:** Implement; `npm run lint && npm run typecheck`.
- [ ] **Step 2:** Manual at 1440 px and 390 px, light and dark: create a hospital, see it on the list as Invite pending, open detail, resend, revoke, suspend, reactivate. Fix anything that wraps badly or scrolls the page horizontally.
- [ ] **Step 3:** Commit `feat(web): platform hospitals list, onboarding dialog and hospital page`.

### Task 12: Overview, Invites, Emails sent, Activity, Platform team

**Files:** `src/app/(app)/platform/{page,invites/page,emails/page,activity/page,team/page}.tsx`; `src/features/platform/{overview-screen,emails-screen,activity-screen,team-screen}.tsx`; i18n.

**Interfaces:**
- Overview: sketch 1 — four stat tiles in a `grid sm:grid-cols-2 lg:grid-cols-4` (Active hospitals, Invites pending, Staff accounts, Emails failed · 7 days — red text only when > 0); "Needs attention" list, each item a link (to hospital detail or Invites filtered); "Recent activity" list; empty attention state "Nothing needs you right now."
- Invites page: status filter tabs (Pending default, Accepted, Expired, Revoked, All) + `InvitesTable`.
- Emails sent: table When, To, Subject, Kind, Status (+error text under the chip when FAILED); row opens a `Dialog` showing headers and `body_text` in a `whitespace-pre-wrap` block; filters status + search by recipient.
- Activity: table When, Who, Action (humanised via an i18n map of action codes, fallback raw), Hospital; filters hospital + action.
- Team: table Name, Email, Role, 2FA, Last login, Status; "Invite team member" dialog (name, email, role select) and Deactivate (confirm) — both only with `platform:team_manage`.

- [ ] **Step 1:** Implement; `npm run lint && npm run typecheck && npm run build` → PASS, and check the `next build` route sizes: no platform route's first-load JS more than ~15 kB above `/admin/staff`.
- [ ] **Step 2:** Manual pass at 1440/390 px, light/dark, as both PLATFORM_ADMIN and a PLATFORM_SUPPORT user (support sees no create/revoke/suspend/team-manage buttons).
- [ ] **Step 3:** Commit `feat(web): platform overview, invites, emails, activity and team screens`.

### Task 13: End-to-end and screenshot review

**Files:** `frontend/e2e/support.ts` (add `platform: "platform@demo.hospital"`, `latestInviteLink(email)` reading the newest `.eml` for that recipient from `../backend/.mail-outbox`), `frontend/e2e/platform.spec.ts`, `frontend/e2e/platform-screenshots.spec.ts`.

- [ ] **Step 1:** `platform.spec.ts`:
  - `owner onboards a hospital and its admin signs in`: sign in as platform → Hospitals → New hospital & invite admin (unique suffix) → detail shows Pending → sign out → open `latestInviteLink` → set password → URL not `/login`, sidebar shows "Administration".
  - `a used link shows the invalid page`: reopen same link → text "no longer valid".
  - `a revoked link shows the invalid page`.
  - `a suspended hospital's admin cannot sign in` (suspend the hospital created above, then try to sign in → suspension message; reactivate in `afterAll`).
- [ ] **Step 2:** `platform-screenshots.spec.ts`: for each of `/platform`, `/platform/hospitals`, a hospital detail, `/platform/invites`, `/platform/emails`, `/platform/activity`, `/platform/team`, and an `/invite/<token>` page, capture full-page PNGs at viewport 1440×900 and 390×844, with `colorScheme: "light"` and `"dark"`, into `test-results/platform-screens/`. Assert `document.documentElement.scrollWidth <= window.innerWidth` on each.
- [ ] **Step 3:** Run `npx playwright test e2e/platform.spec.ts e2e/platform-screenshots.spec.ts` → PASS. Open every PNG and review: contrast, alignment, truncation, empty states, dark mode. Fix and rerun until clean.
- [ ] **Step 4:** Commit `test(e2e): platform onboarding flow and console screenshots`.

### Task 14: Docs and real Gmail smoke test

**Files:** `.env.example` (already updated in Task 1 — verify), `README.md` (Platform console section: what it is, how to invite, local dev outbox), `docs/runbook.md` (Gmail App Password setup: enable 2-Step Verification → https://myaccount.google.com/apppasswords → create "Hospital Management" → paste 16-char password into `SMTP_PASSWORD`, set `MAIL_BACKEND=smtp`, restart API; troubleshooting `535` = wrong app password, `Connection timed out` = port 587 blocked).

- [ ] **Step 1:** Write docs. Commit `docs: platform console and Gmail SMTP setup`.
- [ ] **Step 2 (needs the user):** once `SMTP_PASSWORD` is in `.env`, set `MAIL_BACKEND=smtp`, invite a test address the user controls, confirm delivery and that the email row shows `SENT` with a Message-ID. Until then this step is reported as pending, not done.

---

## Self-review notes
- Spec §5.6 "pending invites can't be accepted while suspended" → Task 4 `accept_invite` + Task 4 test `test_invite_to_suspended_hospital_refused` covers creation; add `test_accept_refused_when_hospital_suspended` in Task 4 Step 1 as well.
- Spec §6.6 rate limiting: the codebase has no IP rate limiter (login protection is per-account lockout). Invite tokens are 256-bit, so guessing is infeasible; failed accepts are audited. No limiter is added — deviation noted for the user.
- Spec "Expiry sweep is cosmetic" → Task 4 checks `expires_at` on every accept regardless of Task 8.
