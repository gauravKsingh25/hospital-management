# Accountant Billing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the accountant (and hospital admin) an Accounts dashboard, patient bill history and cost journey, an IPD bill editor with a post-discharge final bill, and a lettered printable bill — while OPD bills stay frozen and nobody else can change an IPD bill.

**Architecture:** All money logic stays in `billing/service.py`. Billing learns whether a visit is an IPD stay through a **stay provider** that `ipd` registers at startup (the `clinical.register_pending_provider` pattern) — `ipd.service` already imports `billing.service`, so billing must never import `ipd`. Permission gates live in routers; structural rules (OPD lock, discharge-before-final) live in services. Frontend: new screens under `features/billing/accounts/`, `features/admin/`, one print route.

**Tech Stack:** FastAPI, SQLModel, Alembic, PostgreSQL (RLS), pytest · Next.js 16 App Router, React 19, TanStack Query v5, shadcn/ui + Tailwind v4, next-intl, Playwright.

**Spec:** `Docs/superpowers/specs/2026-10-19-accountant-billing-design.md` (approved 2026-10-19).

## Global Constraints

- Every schema change is an Alembic migration; latest head is `3b9e4c1d7a52`. New tenant tables get `hospital_id` + RLS policy like `billing_rls_and_permissions`.
- Every new list endpoint is paginated (`Page[...]`, `PageParams`).
- Money is `Decimal`; the frontend never computes money (display only via `formatMoney`).
- Every money-affecting action writes `identity_service.record_audit` with before/after.
- Roles/permissions declared in `rbac.py` **and** granted in a migration; `tests/test_rbac_seed.py` must stay green.
- New UI strings in `en.json` and `hi.json`; 44 px tap targets (`h-tap`); no new npm or pip dependency.
- Python patch scripts write LF (`newline="\n"`); commit messages conventional, **no Co-Authored-By trailer** (owner preference).
- `billing` must not import `app.modules.ipd` (cycle).
- Error codes fixed by the spec: `opd_bill_locked`, `ipd_bill_by_accounts`; this plan adds `stay_not_discharged`, `bill_changed`, `unpriced_charges` (exists).

## Review Focus

1. **Patient admitted from a still-open OPD visit** (same encounter, type stays `OPD`, status `ADMITTED`): must be treated as an IPD stay for edits/collect/final bill, and its pre-admission consultation must be a separate frozen OPD bill. → Task 3 tests both.
2. **Stay ended by death/LAMA/referral instead of discharge:** the encounter closes without `discharge_patient`; the final bill must still be generatable. → Task 5 test `test_final_bill_after_death_is_allowed`.
3. **Two accountants saving the same stay:** second Save must be refused (`bill_changed`), not silently merged. → Task 4 test.
4. **Old bill reprinted after the hospital changes its logo/address:** prints the details from the day of issue. → Task 6 test.
5. **Running IPD charges inflating "Outstanding":** still-admitted stays count only in "Running in wards". → Task 7 test.

---

## File map

Backend
- `app/modules/billing/rbac.py` — new permissions, ACCOUNTANT grants.
- `app/modules/identity/rbac.py` — `Roles.ACCOUNTANT`, `SYSTEM_ROLES` entry.
- `app/modules/billing/stays.py` (new) — `StayInfo` dataclass, provider registry.
- `app/modules/billing/models.py` — `InvoiceScope` enum, `Invoice.scope`, `Invoice.letterhead` (JSONB).
- `app/modules/billing/service.py` — guards, `split_pre_admission`, `edit_charge`, `apply_bill_changes`, `bill_version`, `finalise_ipd_bill`, letterhead snapshot, accounts read functions.
- `app/modules/billing/accounts.py` (new) — read-side queries for the dashboard, history, journey (keeps `service.py` from growing further); re-exported through `service.py`.
- `app/modules/billing/schemas.py`, `router.py` — DTOs and routes.
- `app/modules/ipd/handlers.py` — register the stay provider; `ipd/service.py` — call `split_pre_admission`.
- `app/modules/tenancy/models.py`, `service.py`, `router.py`, `schemas.py` — `HospitalLogo`, logo endpoints.
- `alembic/versions/20261019_1000-…_accountant_role_and_ipd_billing.py` (Task 1), `20261019_1100-…_invoice_scope_and_letterhead.py` (Task 3), `20261019_1200-…_hospital_logos.py` (Task 6).
- Tests: `tests/test_billing_accounts.py` (service), `tests/test_api_accounts.py` (HTTP), `tests/test_api_hospital_logo.py`.

Frontend
- `src/app/(app)/billing/page.tsx` — tabs Accounts / Cash counter.
- `src/features/billing/accounts/` (new): `accounts-dashboard.tsx`, `totals-strip.tsx`, `bills-table.tsx`, `payments-table.tsx`, `patients-table.tsx`, `use-accounts.ts`.
- `src/app/(app)/billing/patients/[patientId]/page.tsx` + `features/billing/accounts/patient-history.tsx`, `cost-journey.tsx`, `ipd-bill-editor.tsx`.
- `src/app/(print)/bills/[invoiceId]/page.tsx` + `features/billing/print/printed-bill.tsx`, `amount-in-words.ts`.
- `src/features/admin/hospital-profile-screen.tsx` (replaces `payments-screen.tsx`; route `/admin/payments` → `/admin/hospital`).
- E2E: `e2e/accounts.spec.ts`, `e2e/hospital-profile.spec.ts` (replaces `admin-payments.spec.ts`).
- Docs: `Docs/accountant-billing-ui-test.md`; diagram via the archify skill.

---

### Task 1: ACCOUNTANT role and IPD-bill permissions

**Files:** Modify `app/modules/identity/rbac.py`, `app/modules/billing/rbac.py`; Create migration `alembic/versions/20261019_1000-a1c4e7b9d2f0_accountant_role_and_ipd_billing.py`; Test `tests/test_rbac_seed.py` (existing drift tests), `tests/test_billing_accounts.py`.

**Interfaces — Produces:** `Roles.ACCOUNTANT = "ACCOUNTANT"`; `BillingPermissions.IPD_BILL_EDIT = "ipd_bill:edit"`, `IPD_BILL_FINALISE = "ipd_bill:finalise"`, `ACCOUNTS_READ = "accounts:read"`.

- [ ] **Step 1: Failing test** `test_accountant_holds_exactly_the_ipd_billing_powers`: `role_grants()["ACCOUNTANT"]` ⊇ {`accounts:read`, `ipd_bill:edit`, `ipd_bill:finalise`, `charge:read`, `invoice:read`, `payment:read`} and excludes `payment:record`; HOSPITAL_ADMIN holds all three new codes; CASHIER, RECEPTIONIST, BILLING_STAFF, PHARMACIST hold none of `ipd_bill:edit|ipd_bill:finalise`.
- [ ] **Step 2: Run** `pytest tests/test_billing_accounts.py -k accountant_holds -q` → FAIL (KeyError ACCOUNTANT).
- [ ] **Step 3:** Add the role (`"Accountant"`, `"Reviews, corrects and finalises IPD bills; sees all billing."`), `_STAFF_BASELINE` in `SYSTEM_ROLE_PERMISSIONS`; three `PermissionDef`s; `_ACCOUNTANT = (*_READ, ACCOUNTS_READ, IPD_BILL_EDIT, IPD_BILL_FINALISE)` plus, in `patients/rbac.py` and `ipd/rbac.py` and `scheduling/rbac.py`, the read grants the screens need (`patient:read`, `admission:read`, `ward:read`, `doctor:read`). Migration: insert role (pattern `8d31c7ae04b2`), insert the three permissions (pattern `7f4a1c92d6b3`), grant ACCOUNTANT its codes and HOSPITAL_ADMIN + PLATFORM_ADMIN the three new codes; downgrade removes role grants, role, permissions.
- [ ] **Step 4: Run** `pytest tests/test_billing_accounts.py tests/test_rbac_seed.py -q` → PASS. Run `node scripts/codegen.mjs` in `frontend/`.
- [ ] **Step 5: Commit** `feat(billing): accountant role and IPD bill permissions`.

### Task 2: Stay provider, IPD guards on existing routes, collect refusal

**Files:** Create `app/modules/billing/stays.py`; Modify `billing/service.py`, `billing/router.py`, `ipd/handlers.py`, `ipd/service.py` (provider fn), `scheduling/router.py` (board), `frontend/src/features/billing/collect-strip.tsx` (no change expected — verify); Test `tests/test_billing_accounts.py`, `tests/test_api_accounts.py`.

**Interfaces — Produces:**
- `stays.StayInfo` (frozen dataclass): `admission_id: UUID, admission_number: str, status: str` (`ADMITTED|DISCHARGE_INITIATED|DISCHARGED|CANCELLED`), `admitted_at: datetime, discharged_at: datetime | None, ward_bed: str | None, attending_doctor: str | None`.
- `stays.register_stay_provider(fn)`; `async stays.stay_for(session, encounter_id, *, hospital_id) -> StayInfo | None`; `async stays.stays_for(session, encounter_ids, *, hospital_id) -> dict[UUID, StayInfo]` (bulk, for lists). Re-exported from `billing.service`.
- `ipd.service.stay_info_for(session, encounter_ids, *, hospital_id) -> dict[UUID, StayInfo]` registered in `ipd/handlers.py`.
- Router helper `_require_ipd_power(context, permission)` raising `PermissionDeniedError(code="ipd_bill_accounts_only")`.

- [ ] **Step 1: Failing tests:** `test_billing_staff_cannot_discount_an_ipd_charge` (HTTP 403 `ipd_bill_accounts_only`); `test_billing_staff_can_still_discount_an_opd_charge` (200); `test_cashier_cannot_issue_an_ipd_invoice` (403); `test_collect_refuses_an_inpatient_stay` (service, `ConflictError.code == "ipd_bill_by_accounts"`); `test_board_shows_no_balance_for_an_admitted_patient` (`balance_due is None` on the board row of a patient admitted from that visit); `test_stay_for_an_opd_visit_is_none`.
- [ ] **Step 2: Run** → FAIL (no `stay_for`).
- [ ] **Step 3:** Implement provider (raises nothing when none registered — returns None, so billing tests without ipd still work). Guard in router for `add_charge`, `discount_charge`, `waive_charge` (`ipd_bill:edit`), `issue_invoice`, `cancel_invoice`, `write_off_invoice` (`ipd_bill:finalise`) **only when** `stay_for(...)` is not None. `collect_balance` raises `ipd_bill_by_accounts` when the encounter has a stay. `balances_for` skips encounters with a stay (bulk `stays_for`).
- [ ] **Step 4: Run** `pytest tests/test_billing_accounts.py tests/test_api_accounts.py tests/test_billing_service.py tests/test_api_billing.py tests/test_api_queue_board.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(billing): only accounts may change IPD bills`.

### Task 3: Invoice scope and the pre-admission OPD split

**Files:** Modify `billing/models.py`, `billing/service.py`, `ipd/service.py:admit_from_request`; Create migration `20261019_1100-b2d5f8a0c3e1_invoice_scope_and_letterhead.py` (adds `invoices.scope` varchar(8) not null default `'OPD'`, backfill `'IPD'` for invoices whose encounter has an admission, and `invoices.letterhead` JSONB null — used in Task 6); Test `tests/test_billing_accounts.py`.

**Interfaces — Produces:** `InvoiceScope(StrEnum)`: `OPD`, `IPD`; `Invoice.scope`; `async split_pre_admission(session, encounter: Encounter, *, admitted_at: datetime, actor: User | None, hospital_id) -> Invoice | None` — gathers PENDING charges with `captured_at < admitted_at` onto a new `scope=OPD` invoice and issues it; returns None when there are none. `assemble_draft` sets `scope=IPD` when `stay_for` is not None and only gathers charges with `captured_at >= stay.admitted_at`.

- [ ] **Step 1: Failing tests:** `test_admitting_from_an_open_visit_freezes_the_consultation_as_an_opd_bill` (admit via `ipd_service.admit_from_request` on an open OPD visit with the ₹500 fee pending → one issued `scope=OPD` invoice of 500, and the IPD draft later contains no consultation line); `test_an_already_paid_consultation_is_left_alone` (fee collected before admission → no new invoice); `test_admitting_from_a_closed_visit_splits_nothing`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3:** Implement; call `billing_service.split_pre_admission` in `admit_from_request` right after `admit_patient` **only** when `target is encounter` (reuse path).
- [ ] **Step 4: Run** `pytest tests/test_billing_accounts.py tests/test_admission_requests.py tests/test_ipd_service.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(billing): consultation before admission is its own OPD bill`.

### Task 4: IPD bill editor backend (batch save)

**Files:** Modify `billing/service.py`, `billing/schemas.py`, `billing/router.py`; Test `tests/test_billing_accounts.py`, `tests/test_api_accounts.py`.

**Interfaces — Produces:**
- Schemas: `BillOp` discriminated union on `op`: `{"op":"add", service_item_code?, description?, category, quantity, unit_price?, reason}` · `{"op":"edit", charge_id, quantity?, unit_price?, description?, reason}` · `{"op":"discount", charge_id, amount, reason}` · `{"op":"remove", charge_id, reason}`; `reason` min 3 chars. `BillChangesRequest {expected_version: str, ops: list[BillOp] (1..200)}`. `IpdBill {encounter_id, version: str, stay: StayRead, charges: list[ChargeRead], total: Decimal, has_unpriced_items: bool, final_invoice: InvoiceSummary | None, can_finalise: bool}`.
- `async bill_version(session, encounter_id, *, hospital_id) -> str` (ISO of max `updated_at` over the stay's charges + count, e.g. `"2026-10-19T10:00:00+00:00#14"`).
- `async edit_charge(session, charge, *, quantity: Decimal | None, unit_price: Decimal | None, description: str | None) -> Charge` (via `_assert_charge_editable` + `_reprice`).
- `async apply_bill_changes(session, encounter, ops, *, expected_version, actor, hospital_id) -> list[tuple[str, Charge, dict, dict]]` (op, charge, before, after — router audits each). Raises `ConflictError("opd_bill_locked")` without a stay, `ConflictError("bill_changed")` on version mismatch, propagates `ValidationError` so nothing is applied.
- Routes: `GET /billing/accounts/{encounter_id}/ipd-bill` (`accounts:read`) → `IpdBill`; `POST /billing/accounts/{encounter_id}/ipd-bill/changes` (`ipd_bill:edit`) → `IpdBill`.

- [ ] **Step 1: Failing tests:** `test_save_applies_every_change_and_recalculates_total`; `test_one_bad_change_applies_nothing` (edit + discount larger than line → ValidationError, total unchanged); `test_a_stale_save_is_refused` (`bill_changed`); `test_opd_visits_cannot_be_edited` (`opd_bill_locked`); `test_remove_waives_and_keeps_the_line`; `test_every_change_is_audited_with_before_and_after` (HTTP; audit rows `billing.ipd_bill.edit` with `changes.before/after/reason`); `test_cashier_cannot_save_changes` (403).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3:** Implement; lock stay rows with existing `_lock_visit_billing` before the version check.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(billing): IPD bill editor saves all changes at once`.

### Task 5: Final bill after discharge

**Files:** `billing/service.py`, `billing/router.py`; Test `tests/test_billing_accounts.py`, `tests/test_api_accounts.py`.

**Interfaces — Produces:** `async finalise_ipd_bill(session, encounter, *, actor, hospital_id) -> Invoice` — requires a stay whose encounter `is_closed` (discharge, death, LAMA, referral all close it) else `ConflictError("stay_not_discharged")`; refuses unpriced (`unpriced_charges`); assembles (`scope=IPD`) and issues (letterhead snapshot from Task 6 lands inside `issue_invoice`). Route `POST /billing/accounts/{encounter_id}/final-bill` (`ipd_bill:finalise`) → `InvoiceRead`, audited `billing.ipd_bill.finalise`.

- [ ] **Step 1: Failing tests:** `test_final_bill_is_refused_while_admitted`; `test_final_bill_after_discharge_issues_and_freezes` (then an edit → `ConflictError` from `_assert_charge_editable`); `test_final_bill_after_death_is_allowed`; `test_final_bill_includes_the_last_night` (discharge accrues → line present).
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat(billing): final IPD bill after discharge`.

### Task 6: Hospital logo and letterhead snapshot

**Files:** `tenancy/models.py` (`HospitalLogo`: `hospital_id` FK, `version: int`, `content_type`, `data: bytes` (LargeBinary), timestamps; unique (`hospital_id`, `version`)), `tenancy/service.py`, `tenancy/router.py`, `tenancy/schemas.py` (`HospitalRead.logo_version: int | None`), `billing/service.py:issue_invoice`; migration `20261019_1200-c3e6a9b1d4f2_hospital_logos.py` (table + RLS); Test `tests/test_api_hospital_logo.py`, `tests/test_billing_accounts.py`.

**Interfaces — Produces:** `PUT /hospitals/{id}/logo` (raw request body, `Content-Type: image/png|image/jpeg` — **no multipart**, `python-multipart` is not installed; `hospital:update`; type checked by magic bytes, ≤ 512 000 bytes, else 422 `logo_invalid`) → `{version}`; `GET /hospitals/{id}/logo?version=` (`hospital:read`) → bytes with content type, 404 when none; `tenancy_service.letterhead_for(session, hospital_id) -> dict` with keys `name, legal_name, address_line1, address_line2, city, state, pincode, phone, email, gstin, hfr_facility_id, logo_version`. `issue_invoice` stores it in `Invoice.letterhead` (all scopes).

- [ ] **Step 1: Failing tests:** `test_admin_uploads_a_png_and_it_versions`; `test_a_gif_or_oversized_file_is_refused`; `test_another_hospital_cannot_read_the_logo` (404); `test_reprint_uses_the_letterhead_of_the_issue_day` (issue, then PATCH address + new logo → `invoice.letterhead` unchanged).
- [ ] **Step 2–4:** run → FAIL, implement (`await request.body()` in the route, size checked before reading the whole body via `Content-Length`), run → PASS.
- [ ] **Step 5: Commit** `feat(tenancy): hospital logo and bill letterhead`.

### Task 7: Accounts read API (dashboard, history, journey, print payload)

**Files:** Create `billing/accounts.py`; Modify `billing/schemas.py`, `billing/router.py`; Test `tests/test_api_accounts.py`.

**Interfaces — Produces** (all `accounts:read`, all lists `Page[...]`, all newest first):
- `GET /billing/accounts/summary?from=&to=` → `AccountsSummary {collected: Decimal, collected_by_method: dict[str, Decimal], bills_count: int, billed: Decimal, outstanding: Decimal, outstanding_patients: int, running_in_wards: Decimal, running_patients: int, reversals: Decimal}`.
- `GET /billing/accounts/bills?from=&to=&q=` → `Page[BillRow {invoice_id, invoice_number, issued_at, created_at, status, scope, patient_id, patient_name, uhid, admission_number?, stay_days?, grand_total, amount_paid, balance_due}]`.
- `GET /billing/accounts/payments?from=&to=&q=` → `Page[PaymentRow {payment fields + patient_name, uhid, invoice_number, received_by_name}]`.
- `GET /billing/accounts/outstanding` → `Page[BillRow]` (issued with balance > 0).
- `GET /billing/accounts/patients?q=` → `Page[PatientRow {patient_id, name, uhid, last_activity_at, visits, billed, paid, due}]` ordered by `last_activity_at desc`.
- `GET /billing/patients/{patient_id}/history` → `BillHistory {patient, lifetime_billed, lifetime_paid, lifetime_due, visits: list[VisitBills {encounter_id, kind: "OPD"|"IPD", started_at, ended_at?, doctor_name?, admission_number?, ward_bed?, status_label, total, paid, due, invoices: list[InvoiceSummary]}]}` — a reused encounter yields two entries (OPD invoice(s) and the IPD stay).
- `GET /billing/patients/{patient_id}/journey?category=&encounter_id=` → `Journey {lines: Page[JourneyLine {captured_at, encounter_id, kind, category, description, quantity, unit_price, total_amount, status}], totals_by_category: dict[str, Decimal]}`.
- `GET /billing/invoices/{invoice_id}/printable` → `PrintableBill {number: str | None, date: datetime, lines: list[ChargeRead], totals: {taxable, cgst, sgst, igst, grand_total, paid, balance}, letterhead: dict, patient: {name, uhid, age_years, gender, phone}, stay: StayRead | None, payments: list[PaymentRead], title: "TAX INVOICE"|"BILL OF SUPPLY"|"INTERIM — NOT A FINAL BILL"}` (`invoice:read`). Letterhead from `invoice.letterhead`; live `letterhead_for` for drafts.
- `GET /billing/accounts/{encounter_id}/ipd-bill/printable` → the same `PrintableBill` for the stay's **running** bill (number None, title INTERIM, live letterhead) (`accounts:read`).

- [ ] **Step 1: Failing tests** (one per endpoint, plus): `test_running_bill_prints_as_interim`; `test_lists_are_newest_first`; `test_running_ipd_charges_are_not_outstanding`; `test_summary_splits_collections_by_method`; `test_history_shows_the_consultation_and_the_stay_separately`; `test_journey_lists_medicines_newest_first_and_totals_by_category`; `test_title_is_tax_invoice_only_when_a_line_carries_gst`; `test_receptionist_cannot_open_the_accounts_dashboard` (403).
- [ ] **Step 2–4:** run → FAIL, implement (bulk lookups through `patients_service`, `clinical_service`, `stays_for` — no per-row queries), run → PASS. Full `pytest -q` green; `ruff check`, `ruff format --check`, `mypy app` clean.
- [ ] **Step 5: Commit** `feat(billing): accounts dashboard, bill history and cost journey API`. Regenerate frontend types.

### Task 8: Hospital profile screen (frontend)

**Files:** Create `src/features/admin/hospital-profile-screen.tsx`, `src/app/(app)/admin/hospital/page.tsx`; Delete `features/admin/payments-screen.tsx`, `app/(app)/admin/payments/page.tsx` (redirect `/admin/payments` → `/admin/hospital` via `redirect()` in a kept page file); Modify `admin-sections.ts` (card "Hospital profile", `hospital:update`); i18n; E2E `e2e/hospital-profile.spec.ts` replacing `admin-payments.spec.ts` (keeps its UPI assertions and restore-in-afterEach).

- [ ] **Step 1: Failing e2e** `an administrator sets the letterhead and logo`: fill address/GSTIN, upload `e2e/fixtures/logo.png` (create a 1×1 PNG), Save → preview `img` visible, reload keeps values; UPI flow from the old spec still passes.
- [ ] **Step 2–4:** run → FAIL, build the form (one `PATCH` for fields, one `PUT` of the file's raw bytes with its MIME type for the logo — check the BFF route forwards non-JSON bodies, extend it if not — preview from `GET …/logo?version=`), run → PASS.
- [ ] **Step 5: Commit** `feat(admin): hospital profile with logo`.

### Task 9: Accounts dashboard (frontend)

**Files:** Modify `src/app/(app)/billing/page.tsx` (tabs; Accounts default when `accounts:read`); Create `features/billing/accounts/{accounts-dashboard,totals-strip,bills-table,payments-table,patients-table,use-accounts}.tsx|ts`; nav: Billing item permissions gain `accounts:read`; i18n.

**Interfaces — Consumes:** Task 7 endpoints. **Produces:** `useAccounts(tab, period, q, offset)` hooks; period presets `today | 7d | month | custom`.

- [ ] **Step 1: Failing e2e** in `e2e/accounts.spec.ts`: `the accountant opens Billing on the Accounts dashboard, newest bill first` (sign in as new seeded `accountant@demo.hospital`, collect a bill as reception first, expect it as row 1 of Bills, totals strip shows Collected); `reception keeps the cash counter and no Accounts tab`.
- [ ] **Step 1b:** e2e `a banner asks for the hospital profile when the logo is missing` (admin with no logo → banner links to `/admin/hospital`; hidden once `logo_version` and `address_line1` are set).
- [ ] **Step 2–4:** run → FAIL, implement (server-render page 1, TanStack Query for the rest, `Pager`, CSV export built client-side from the fetched page set via a `?limit=` loop capped at 5 000 rows), run → PASS. Add the accountant to `scripts/seed_demo.py` `STAFF` and `e2e/support.ts` `ACCOUNTS`.
- [ ] **Step 5: Commit** `feat(billing): accounts dashboard`.

### Task 10: Patient bill history and cost journey (frontend)

**Files:** Create `src/app/(app)/billing/patients/[patientId]/page.tsx`, `features/billing/accounts/{patient-history,cost-journey}.tsx`; i18n.

- [ ] **Step 1: Failing e2e** `a patient's bills are listed newest first and the newest is open`; `the cost journey lists what was given, newest first` (category filter narrows; totals shown).
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat(billing): patient bill history and cost journey`.

### Task 11: IPD bill editor (frontend)

**Files:** Create `features/billing/accounts/ipd-bill-editor.tsx` (+ `use-ipd-bill.ts`); used by `patient-history.tsx` for IPD visits when the user has `ipd_bill:edit`.

**Interfaces — Consumes:** `GET/POST …/ipd-bill[/changes]`, `POST …/final-bill`. Staged ops held in `useReducer`; unsaved banner; Save disabled with no ops; on `bill_changed` toast + refetch + clear ops; "Generate final bill" shown only when `can_finalise`; reason dialog per op (min 3 chars).

- [ ] **Step 1: Failing e2e** `the accountant edits an IPD bill and the total changes only on Save` (admit a patient via the admission desk helpers in `support.ts`, add a ₹240 dressing + edit qty, total unchanged until Save, then increases); `after discharge the accountant generates the final bill and it can no longer be edited`; `billing staff see the IPD bill read-only`.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat(billing): IPD bill editor`.

### Task 12: Printable bill

**Files:** Create `src/app/(print)/layout.tsx` (no app chrome, white, A4 `@page { size: A4; margin: 12mm }`), `src/app/(print)/bills/[invoiceId]/page.tsx` and `src/app/(print)/bills/running/[encounterId]/page.tsx` (server components, fetch the two `printable` endpoints), `features/billing/print/printed-bill.tsx`, `features/billing/print/amount-in-words.ts` (`amountInWords(amount: string): string` — Indian system, e.g. `"24600.00"` → `"Rupees Twenty-Four Thousand Six Hundred Only"`, `"150000.50"` → `"Rupees One Lakh Fifty Thousand and Fifty Paise Only"`); logo `img` from the BFF logo route at `letterhead.logo_version`; Print / Download buttons call `window.print()`; links from every Open/PDF/Print action point here (`?print=1` auto-prints).

- [ ] **Step 1: Failing tests:** unit-style e2e `amount in words` via `page.evaluate` on an exported helper page **or** a tiny Node test run with `node --test` on the TS file compiled by `tsc` (choose whichever already runs in CI; default: assert through the printed page); e2e `the printed bill carries the letterhead, GST columns and amount in words` (opens the print route of the final bill from Task 11's flow).
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat(billing): printable bill with letterhead`.

### Task 13: Verification, UI test guide, decision diagram

- [ ] **Step 1:** Full gates: backend `pytest -q`, `ruff check`, `ruff format --check`, `mypy app`; frontend `npm run verify`; full `npx playwright test`. Record pass/fail counts.
- [ ] **Step 2:** Write `Docs/accountant-billing-ui-test.md` — the owner's click-by-click path (accounts per role, what to click, what they should see), mapping each step to the spec section.
- [ ] **Step 3:** Use the archify skill to draw the decision/flow diagram (roles → screens → OPD/IPD split → edit/save → discharge → final bill → print; plus the stay-provider dependency inversion), save under `Docs/diagrams/`.
- [ ] **Step 4: Commit** `docs: accountant billing test guide and flow diagram`.
