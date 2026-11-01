# Security, Testing & UX Audit — Hospital Patient-Journey System

**Date:** 28 Oct 2026 · **Branch:** `version2` (with uncommitted working-tree changes) · **Environment:** local Docker stack, demo tenant `DEMO`
**Scope:** complete patient journey (OPD → lab → billing → IPD → edge cases), payment security, data leakage, RBAC, session security, tablet/desktop UX and emergency usability.
**Method:** live API tests with real role logins (reception, nurse, doctor, lab, cashier, admission, accountant, admin); code review of the modules involved; Playwright measurements at 768×1024 (tablet portrait), 1180×820 (tablet landscape), 1366×768 (desktop).

Test data left in the dev DB (nothing was deleted): patients named `JOURNEY Test …`, `IPD Test …`, `EDGE …`, `ALLERGY Test …`, `PAYTEST …`.

---

## 1. Verdict in one paragraph

The core is solid. The encounter state machine, multi-tenant isolation, the BFF token design and **payment security held against every attack tried**, including 12 simultaneous payments on one bill (exactly one accepted). The serious problems are elsewhere:
- **Patient safety:** a drug can be ordered for a patient with a recorded critical allergy to it, with no warning.
- **Medico-legal records:** a death can be "certified" by a cashier, with a time of death years in the past.
- **Insider data harvesting:** any staff login can page out the full patient directory, phone numbers included, without leaving an audit row.
- **Usability:** the app breaks on portrait tablets, and there is no fast path for an unidentified emergency patient.

| Severity | Count | Items |
|---|---|---|
| Critical | 1 | SAFE-01 |
| High | 5 | BUG-01, BUG-02, SEC-01, UX-01, UX-02 |
| Medium | 7 | SEC-02, SEC-03, SEC-04, SEC-05, BUG-03, BUG-04, UX-03 |
| Low / hardening | 12 | rest of §4–§6 |

## 2. Diagrams (open in a browser)

| Diagram | What it shows |
|---|---|
| [diagrams/opd-journey.html](diagrams/opd-journey.html) | Sequence of the live OPD test, step by step, role by role |
| [diagrams/encounter-lifecycle.html](diagrams/encounter-lifecycle.html) | Every encounter state, the transitions tested, and where the bugs sit |
| [diagrams/money-controls.html](diagrams/money-controls.html) | How money moves from clinical act → charge → invoice → receipt, and each control verified |
| [diagrams/security-boundaries.html](diagrams/security-boundaries.html) | Trust boundaries (tablet → BFF → API → RLS Postgres → worker → WhatsApp) and where each finding lives |

All four passed archify's automated gates (schema, delivery, strict check, real-browser check). I did not do a separate manual visual review.

---

## 3. Patient journey — test results

| # | Step | Actor | Result |
|---|---|---|---|
| 1 | Register (4 fields) | Reception | PASS: 201, UHID `DEMO-26-000743` |
| 2 | Quick OPD → appointment + visit + token in one call | Reception | PASS: steps 1+2 took **0.19 s** server time |
| 3 | Vitals | Nurse | PASS |
| 4 | Start consultation | Doctor | PASS: `REGISTERED → IN_CONSULTATION` |
| 5 | Note, diagnosis, CBC order | Doctor | PASS (a `<script>` in the note is stored as text; React escapes it and there is no `dangerouslySetInnerHTML` anywhere) |
| 6 | Complete visit | Doctor | PASS: `→ PENDING_CLEARANCE`, pending = lab + ₹850 |
| 7 | Collect ₹850 by UPI | Cashier | PASS: billing item cleared |
| 8 | Accession, collect, receive, result | Lab | PASS, but see BUG-06 (free-text order needs catalogue mapping) |
| 9 | Verify report | Lab → **403** (correct), Doctor → 200 | PASS: separation of duties holds |
| 10 | Visit closes itself | System | PASS: `→ COMPLETED`, no trip back to reception |
| 11 | Timeline | — | PASS: 4 rows, each with the actor's email |
| 12 | Emergency-priority Quick OPD → admit → bed | Reception / Doctor / Admission | PASS (bed GW1-07), but `attending_doctor_id` is left empty (BUG-05) |
| 13 | IPD medication order | Doctor | PASS, but **no allergy check** (SAFE-01) |
| 14 | Death on an admitted patient | Doctor | PASS: patient flagged, admission closed, bed → CLEANING, new visit refused (409) |
| 15 | Follow-up message to deceased patient | Admin (manual) | PASS: **SUPPRESSED** (the §14 invariant holds) |
| 16 | Referral | Reception → 403, Doctor → `REFERRED_OUT` | PASS |
| 17 | LAMA | Nurse → **403** | Works, but see UX-07 |
| 18 | Cancel; then start a cancelled visit; PATCH `status` | Reception / Doctor / Admin | PASS: cancel works, restart is refused (409), PATCH cannot change status |
| 19 | Any transition out of a terminal state | — | PASS: 409 `illegal_state_transition` |
| 20 | Death certified by a cashier; death dated 2015 | Doctor | **FAIL**: both accepted (BUG-01, BUG-02) |

---

## 4. Security findings

### Payment security — the strongest area. Nothing exploitable found.

Every one of these was tried live:

| Attack | Result |
|---|---|
| Amount `-500`, `0`, `99999999999999.00` | 422 (`gt=0`, `max_digits=12`, Decimal end to end) |
| Pay more than the balance; pay after the bill is settled | 422 `payment_exceeds_balance` |
| **12 simultaneous full payments on one invoice** | **1 × 201, 11 × 422**; `amount_paid == grand_total` (`SELECT … FOR UPDATE` plus a fixed lock order) |
| Cashier tries waive / discount / reverse / write-off / edit rate card | 403 on every one |
| Accountant tries to take cash | 403. Setting money and taking money are properly split |
| Discount larger than the line, or negative | Refused (not clamped), so no negative invoice and no "credit" extraction |
| Correct a paid bill downward to create a refund | Refused: `correction_below_paid` |
| Under-charge through the one-tap collect path | The amount comes from the server; `expected_amount` is only a "bill changed" guard |
| Edit an issued bill | Blocked; corrections replace the bill and carry receipts forward |
| Inject tenant/role/IP headers through the BFF | The BFF forwards only `Content-Type` and `Accept-Language` and strips `set-cookie` |

Remaining payment hardening (Low):
- **SEC-06** — Audit-log IP comes from the client-supplied `X-Forwarded-For` ([backend/app/core/deps.py:30](../../backend/app/core/deps.py#L30)). Through the web app this is harmless. But an insider who can reach `:8000` directly can forge the IP recorded against a payment or write-off. *Fix:* trust `X-Forwarded-For` only from a configured proxy list (uvicorn `--forwarded-allow-ips`), make the edge proxy overwrite it, and firewall `:8000` so only the BFF can reach it.
- **SEC-07** — `received_at` on a payment is caller-supplied and unbounded ([billing/schemas.py:307](../../backend/app/modules/billing/schemas.py#L307)), so a receipt can be back-dated into a closed day. *Fix:* reject anything more than ~10 min in the future or before the start of today unless the caller holds `payment:backdate`; always store server time separately.

### Data leakage

**SEC-01 · HIGH — Patient directory can be bulk-exported with no audit trail**
- *Evidence:* as `lab@`, `GET /api/v1/patients?limit=100&offset=…` returned 718 patients (name, phone, DOB, city, blood group) in 8 calls. Every role tested (reception, nurse, lab, cashier, admission, accountant, doctor) gets 200.
- *Why it matters:* single-record views and searches **are** audited, but [the list endpoint](../../backend/app/modules/patients/router.py#L200) writes nothing. A departing employee could take the whole patient phone book, a DPDP-reportable breach, and the audit log would show nothing.
- *Fix (≈½ day):*
  1. Write one audit row per page (`patient.list`, filters, offset, count returned).
  2. Split `patient:list` from `patient:read` and grant it only to front desk, records and admin. Lab, cashier and accountant work from a visit, not the directory.
  3. Mask phone numbers (`98•••••512`) for roles that do not dial patients.
  4. Alert an admin when one user reads more than ~300 distinct patients an hour.

**SEC-02 · MEDIUM — Cause of death visible to cashier and lab**
- *Evidence:* as `cashier@`, `GET /encounters/patient/{id}/timeline` returns `event_metadata: {"cause_of_death": "Acute MI", "deceased_at": …, "death_certified_by_id": …}`. The visit list correctly hides diagnoses from lab and cashier, so this is the gap in an otherwise good model.
- *Fix:* in the timeline serializer ([clinical/schemas.py:236](../../backend/app/modules/clinical/schemas.py#L236)), drop clinical keys (`cause_of_death`, `referral_reason`, `lama_reason`) unless the caller has clinical read permission. Keep from/to status, actor and time.

**SEC-03 · MEDIUM — The patient message outbox is readable by lab, nurse, cashier and reception**
- *Evidence:* as `lab@`, `GET /notifications/{id}` returns `recipient_phone`, the full `body`, and the delivery attempt log, for every patient in the hospital.
- *Fix:* grant `notification:read` only to admin and reception supervisors. Return a masked phone and omit `body` unless the caller can manage templates.

**SEC-04 · MEDIUM — No idle lock on shared tablets**
- The access token lasts 15 minutes, but refresh lasts **7 days** and the BFF refreshes silently ([core/config.py:115-117](../../backend/app/core/config.py#L115-L117)). There is no inactivity lock in the frontend. A ward tablet left on the counter stays logged in as the last nurse or doctor for the whole browser session, against CLAUDE.md §12 "session timeouts on all roles".
- *Fix:*
  - Client: after N idle minutes (per role, default 10), show a lock screen. Unlock with password or a 4–6 digit PIN, and offer "Switch user". Keep page state so nothing is lost mid-task.
  - Server: add a refresh **idle** expiry (e.g. 30 min) and a shift-length absolute cap (e.g. 12 h).

**SEC-05 · MEDIUM — 2FA is not built.** Columns are reserved ([identity/models.py:96](../../backend/app/modules/identity/models.py#L96)), but admin and doctor log in with a password only. *Fix:* TOTP enrolment, enforced for `HOSPITAL_ADMIN`, `PLATFORM_ADMIN` and `DOCTOR`, with remembered-device for 12 h on a hospital device.

**Low / informational**
- **SEC-08** — `PATCH /encounters/{id}` with `{"status":"COMPLETED"}` returns **200** and silently ignores the field. Not exploitable (status did not change), but a client believes it succeeded. *Fix:* `model_config = ConfigDict(extra="forbid")` on update schemas.
- **SEC-09** — `/docs` and `/openapi.json` are turned off only when `ENVIRONMENT=production`, so staging publishes the full API map. There is no HSTS header and the API itself sends no security headers. *Fix:* disable docs unless `local`; add HSTS at the TLS edge.
- **SEC-10** — `backend/app/core/Untitled-1.tex` (untracked) sits inside the application package. Delete it so it never ships in an image.

**Confirmed good (no action):** BFF keeps the JWT server-side in an httpOnly cookie. Strict nonce-based CSP (`unsafe-eval` only in dev), `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. CORS refuses foreign origins (400). Argon2id hashing. 15-minute access tokens with `typ` checking. Lockout after 5 failures. Tenant re-read from the database on every request and RLS set per request. Page size capped at 100. Patient view, search and UHID lookup audited. Only `.env.example` is tracked in git.

---

## 5. Functional bugs

**SAFE-01 · CRITICAL — No allergy check when prescribing; allergies not shown on the drug round**
- *Evidence:* recorded a `DRUG_ALLERGY` alert "Aspirin — anaphylaxis, CRITICAL" on patient `ALLERGY Test …`, then the doctor ordered **Aspirin 300 mg OD**: **201 Created**, no warning. [ipd/service.py `prescribe` (line 1794)](../../backend/app/modules/ipd/service.py#L1794) never reads patient alerts, and the drug-round screen ([features/chart/medication-round.tsx](../../frontend/src/features/chart/medication-round.tsx)) never displays them.
- *Fix (≈1–2 days):*
  1. In `prescribe` (and the OPD prescription path), load active `DRUG_ALLERGY` alerts through `patients.service`. Match the drug name or catalogue item (and its generic name or class, once the medicine catalogue carries one) case-insensitively.
  2. On a match, raise `allergy_conflict` (409) unless the request carries `override_reason`, and audit the override.
  3. Show the existing `SafetyBanner` on each patient row in the drug round, and a red "ALLERGY: Aspirin" chip beside any dose that matches.

**BUG-01 · HIGH — Any staff member can be named as the certifying doctor on a death**
- *Evidence:* `POST /encounters/{id}/death` with `death_certified_by_id` = the **cashier's** user id → 200, patient recorded deceased. A random UUID is refused (404), so existence is checked but role is not.
- *Fix:* in `record_death` ([clinical/service.py:796](../../backend/app/modules/clinical/service.py#L796)), fetch the certifier through `identity.service` and require that they are active, in the same tenant, and hold the `DOCTOR` role (or a new `death:certify` permission). In the UI, make the certifier a doctor-only picker that defaults to the current doctor.

**BUG-02 · HIGH — Time of death can be years before the visit or before birth**
- *Evidence:* a 5-year-old's visit opened today, `deceased_at = 2015-01-01` → 200. Future times are rightly refused.
- *Fix:* in the `DeathRecord` validation (service side, which can see the encounter), require `deceased_at ≥ encounter.started_at − 1 h` (allows "brought in dead" entries) and `≥ patient.birth_date`. Apply the same rule to `lama_at` and `referred_at`.

**BUG-03 · MEDIUM — The live English "appointment booked" message is corrupted, and templates accept unknown placeholders**
- *Evidence:* the demo tenant's `APPOINTMENT_BOOKED / en` template reads `Namaste {patient_name}, aapka {nonexistent} taiyar hai.`, and messages with those literal braces were marked **SENT** today. There are also 33 junk templates (`q06`, `quv5oxdh`, …) left by `frontend/e2e/messages.spec.ts`, which runs against the demo tenant.
- *Fix:*
  1. Restore the shipped English template.
  2. On template create/update, **reject** (not just warn in preview) any placeholder not in the code's allowed set, and any single-brace `{word}`.
  3. Validate `language` against a known list (`en`, `hi`, regional codes).
  4. Run e2e against a throwaway tenant, or delete what each spec creates.

**BUG-04 · MEDIUM — The follow-up date never reaches the patient**
- *Evidence:* the doctor set follow-up = 5 Nov. The only message sent was `FOLLOW_UP_REMINDER` at the moment the visit closed: "your visit is complete, please book a follow-up if advised". There is no date, and nothing is scheduled before 5 Nov ([notifications/handlers.py:445](../../backend/app/modules/notifications/handlers.py#L445)).
- *Fix:* add `{{follow_up_date}}` to the template. Have the worker sweep schedule a reminder 1 day before `follow_up_date`, re-checking the deceased flag at send time (the suppression logic already exists).

**BUG-05 · LOW** — IPD admission leaves `attending_doctor_id` empty instead of defaulting to the visit's doctor, so ward lists and summaries show no consultant. *Fix:* default it from `encounter.doctor_id` when omitted.

**BUG-06 · LOW** — A doctor can place a free-text lab order ("Complete Blood Count", no code). The lab must then map it to a catalogue test before accessioning (`catalogue_item_id` is required). That is an extra step and a source of mismatched prices. *Fix:* make the doctor's order box a catalogue search, keeping free text only as an "other" escape that lands in a "needs mapping" lane on the bench.

**OPS-01 · LOW** — The `worker` container is permanently **unhealthy**. It inherits the API image's `HEALTHCHECK curl :8000/health` ([backend/Dockerfile:52](../../backend/Dockerfile#L52)), but ARQ serves no HTTP; logs show its sweeps running fine. On Render/Fly this can cause restart loops or hide a real failure. *Fix:* override `healthcheck` for `worker` in `docker-compose.yml` with an ARQ health-key check in Redis.

---

## 6. UX — tablets, desktops and emergencies

Measured with Playwright. Screenshots are in [screenshots/](screenshots/).

| Screen | Portrait tablet 768 | Landscape tablet 1180 | Desktop 1366 |
|---|---|---|---|
| Every page | **Sideways scroll, 6–38 px** | OK | OK |
| Reception board | 9,267 px tall; names cut to "Tes t…"; 65 targets < 40 px | 4,065 px tall | same |
| Admission desk | 58 "Open admission" links only **20 px** tall | same | same |
| Doctor queue | 4,556 px tall, sideways scroll | OK | OK |
| Registration | Clean, 4 fields; doctor picker 32 px tall | OK | OK |
| Smallest text | 12 px | **10 px** | **10 px** |

**UX-01 · HIGH — No emergency / unidentified-patient path**
An unconscious road-accident patient has no name and no mobile, but registration requires `full_name` and `phone`. The registration screen also offers no Emergency priority, even though the API supports `priority=EMERGENCY (10)`. In casualty, staff will type fake data, which breaks duplicate detection and messaging.
*Fix:*
- Add a big red **"Emergency — unknown patient"** button on Reception (shortcut F9). One tap creates `Unknown Male ~40y (EM-0034)` with no phone, gender from a 3-button choice, priority EMERGENCY, and the visit straight to the duty doctor, or straight to `ADMITTED`, which the state machine already allows from `REGISTERED`.
- Identify the patient later with the existing `/patients/{id}/merge`.
- Suppress messaging while the phone is empty, and surface "needs identification" on the reception board.

**UX-02 · HIGH — Portrait tablets: the sidebar crushes the content**
The sidebar is a fixed `w-60` from the `md` breakpoint (768 px), [sidebar.tsx:27](../../frontend/src/components/app-shell/sidebar.tsx#L27). On a portrait iPad that leaves ~520 px for the page, so queue cards wrap one character per line ([screenshot](screenshots/reception-board-tablet-portrait.png)). The header's right group (language + user name) is what pushes every page 36 px wider than the screen ([layout.tsx:33](../../frontend/src/app/(app)/layout.tsx#L33)).
*Fix (≈½ day):*
- Show the sidebar from `lg` (1024 px) and use the existing `MobileNav` drawer below that (change `md:flex` → `lg:flex` and `md:hidden` → `lg:hidden`).
- In the header, hide the user's name and the "English" label below `lg` (keep the icons).
- On the reception board, stack doctor columns below 900 px with a doctor tab strip (it already exists) choosing which one shows.

**UX-03 · MEDIUM — Touch targets and text size**
Raise every tappable element to at least **44×44 px**: "Open admission", "Open reports", the language switch (28 px), drag handles (32 px) and the doctor combobox (32 px). Make the whole admission row the link. Raise the minimum text size from 10 px to 13 px; counter staff often read at arm's length.

**UX-04 · Emergency-friendly behaviour (recommendations)**
1. **Code buttons:** a persistent, role-visible "Code Blue / Rapid Response" button that pages the duty team (a notification with a `staff` recipient type) and logs the time. One tap, no form.
2. **Fewer clicks for terminal events in a crisis:** pre-fill the death form with the current doctor as certifier and "now" as the time. For LAMA, let the nurse in charge record it (currently 403, UX-07). On a night ward the doctor may not be present, and "absconded at 02:10" should be captured when it is noticed, with doctor countersign later.
3. **Humanise durations:** "waiting 4173 min" → "2 d 21 h", and colour anything waiting more than 24 h red on the admission desk ([screenshot](screenshots/admission-desk-tablet-landscape.png)).
4. **Safety banner everywhere a patient is acted on:** it is on the patient page and the IPD stay, but not on the drug round or the billing collect strip. The lab bench has its own identity header but no allergy or infection-precaution alerts, and the precautions matter to whoever handles the sample. The drug round matters most (SAFE-01).
5. **Broken logo:** the sidebar shows an empty grey box where the hospital logo should be (all screenshots). Fall back to initials when no logo is uploaded.
6. **Long boards:** with 60+ waiting, the reception board is a 4,000–9,000 px scroll. Default to "Waiting" only, collapse "Seen" behind a count, and add jump-to-token search.
7. **Cashier history:** cashiers get 403 on the patient's billing history (`accounts:read`), so they can't answer "what did I pay last time?" at the counter. Consider a read-only receipts view for `CASHIER`.

**What already works well:** four-field registration that reads clearly; one-call Quick OPD; F2/F3 shortcuts with visible hints; strong colour-coded status chips; a delay banner ("Running more than 30 minutes behind"); Hindi available from the header.

---

## 7. Fix order (suggested)

| Order | Item | Effort |
|---|---|---|
| 1 | SAFE-01 allergy check + drug-round banner | 1–2 d |
| 2 | BUG-01 / BUG-02 death-record validation | ½ d |
| 3 | SEC-01 audit + restrict + mask patient directory | ½ d |
| 4 | UX-02 portrait layout, UX-03 touch targets | 1 d |
| 5 | UX-01 emergency / unknown-patient registration | 1–2 d |
| 6 | SEC-02 / SEC-03 redaction, BUG-03 templates + e2e isolation | 1 d |
| 7 | SEC-04 idle lock, SEC-05 TOTP | 2–3 d |
| 8 | BUG-04 follow-up reminder, remaining Lows | 1 d |

Each fix should ship with a regression test: pytest for the backend items, and a Playwright spec at 768×1024 for the layout items.
