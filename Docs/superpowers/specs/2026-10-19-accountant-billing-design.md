# Accountant billing — design

Date: 2026-10-19 · Status: for owner approval · Module: `billing` (+ `ipd`, `tenancy`, `identity`, frontend)

## 1. Intent — why this exists

The hospital's accountant has no screen of his own. Billing today is one list,
"Cash counter", built for the cashier's window: visits that still owe money,
**oldest debt first**. Money totals live elsewhere (Reports). The accountant
works out what was billed and collected by asking people or by hand, and the
long IPD (admission) bills — the ones that matter for **GST tax filing** — have
no place where he can review, correct and finalise them.

This work gives him that place, and gives every bill the hospital's own
letterhead so what is printed is a proper document.

What the owner said (paraphrased faithfully):

- The accountant needs **his own dashboard in the Billing section**.
- He sees **all payments received and bills generated**, **newest first**.
- He can **open, download or print any patient's complete bill**.
- Every patient has a **bill history** — previous admissions, their bills and
  amounts — and clicking a newer visit opens that visit's bill.
- On a patient he sees **what charges are on them** and can **add, edit and
  remove charges**. **The total is calculated only when he clicks Save.**
- After saving, **Print generates a proper PDF with the hospital name and
  everything**.
- Multi-tenant: when a hospital is **set up**, capture its **logo (PNG), name,
  address and so on**, and put them at the top of every bill.
- **The accountant's bill is the final one**; he generates the bill for every
  IPD patient, **after discharge**.
- **OPD bills are separate**: generated at the moment of payment (as today),
  visible to the accountant, **never changeable**. A consultation that happened
  before an admission stays its own OPD bill.
- **Only IPD bills are editable**, and **only by the accountant and the
  "super admin" = the hospital admin** (the owner tests everything with the
  hospital admin account).
- **No advance deposits.** The hospital does not take them.
- In the dashboard, bills are **readable, not editable**. The admin wants to see,
  per patient, **the whole journey's cost: what medicines were given, what was
  consumed, and anything else provided** — as a **latest-to-oldest list, not a
  ranking**.

The owner approved the two mockups (Accounts dashboard; patient bill history)
and asked for them **in the project's existing UI**, as working screens.

## 2. Goals

1. An **Accounts dashboard** as the accountant's home in Billing (§5.2).
2. A **patient bill history** page reachable from any patient row (§5.3).
3. A **patient cost journey** — everything given to the patient, itemised (§5.4).
4. An **IPD bill editor**: add / edit / remove charges, staged locally, applied
   atomically on **Save**, totals recalculated by the server (§5.5).
5. **Generate final bill** for a discharged IPD stay, after which it is frozen (§5.6).
6. A **printable A4 bill** with the hospital letterhead for every invoice, OPD
   and IPD; "Download PDF" and "Print" (§5.7).
7. A **hospital profile** (logo, name, address, contacts, GSTIN, UPI ID) set by
   the hospital admin and used on every bill (§5.8).
8. A new **ACCOUNTANT** role; the hospital admin gets the same powers (§5.1).

## 3. Non-goals

- Advance deposits / IPD advances / refunds of advances — the hospital takes none.
- Blocking or changing the ward's discharge flow — discharge stays as it is; the
  bill is made afterwards.
- A patient ranking by cost (owner: "I don't want a rank list").
- Payment gateway auto-confirmation (tracked separately).
- Credit notes. Correcting a final bill uses the existing **cancel invoice**
  route (charges return to the running bill, a new final bill is generated; the
  cancelled number stays in the series, which is correct for GST).
- Insurance / TPA claim screens, GST e-invoicing (IRN/QR), GSTR export files.
- Server-side PDF rendering or any new PDF library.
- Any change to how OPD bills are created (reception row and cash counter).
- A hospital-creation UI for platform admins (hospitals are created by script /
  API today; this adds the **profile** a hospital fills in after creation).

## 4. Anti-goals (failure even if it "works")

- An OPD bill that can be changed after it is generated, by anyone.
- Anyone other than the accountant or the hospital admin changing an IPD bill.
- A charge amount changing **without a recorded reason, actor and before/after**
  in the audit log (NABH/DPDP).
- Totals that move while the accountant is still editing — the total is
  computed **on Save**, as one deliberate step.
- A final bill that can still change after it is generated, or a reprint that
  shows different hospital details than the day it was issued.
- A printed bill without the hospital's identity, or showing another tenant's.
- Reception's one-tap "Paid" issuing an **IPD** bill behind the accountant's back.
- The pre-admission OPD consultation disappearing into, or being edited inside,
  the IPD bill.

## 5. The design

### 5.1 Roles and permissions

- New system role **`ACCOUNTANT`** ("Accountant — reviews, corrects and finalises
  IPD bills; sees all billing"). Seeded like every role: `identity/rbac.py` plus a
  data migration (pattern: `20261005_0903-…admission_desk_takes_vitals`).
- New permissions in `billing/rbac.py`:
  - `ipd_bill:edit` — add, edit (qty / unit price / description), discount or
    remove (waive) a charge **on an IPD stay**.
  - `ipd_bill:finalise` — generate (issue) the final bill of an IPD stay, and
    cancel it to correct it.
  - `accounts:read` — Accounts dashboard, bill history, cost journey, print.
- **ACCOUNTANT** holds: all billing read permissions, `accounts:read`,
  `ipd_bill:edit`, `ipd_bill:finalise`, `payment:read`. Not `payment:record`
  (money is taken at the counter) — builder may add it if a test shows the
  accountant needs it; default no.
- **HOSPITAL_ADMIN** (= the owner's super admin, confirmed) and PLATFORM_ADMIN
  get every new permission.
- **Everyone else loses the power to change IPD bills by hand.** On an IPD stay,
  the existing manual routes — add charge, discount, waive, issue invoice,
  cancel invoice — require `ipd_bill:edit` / `ipd_bill:finalise`, regardless of
  the caller's other permissions. BILLING_STAFF, CASHIER, RECEPTIONIST and
  PHARMACIST keep their current powers **on OPD visits only**.
- **Automatic charge capture is untouched.** Bed-days, medicines issued and
  administered, lab and radiology orders still land on the IPD bill through the
  event bus (`billing/handlers.py`) — that is how the stay's cost is recorded,
  not a hand edit.
- Server-side rules (in `billing/service.py`, not just hidden buttons):
  - An OPD bill, once issued, is never edited (already true: issued = frozen).
    The new edit endpoint refuses OPD visits: `ConflictError("opd_bill_locked")`.
  - `collect_balance` (reception's Get payment / Paid) refuses an IPD stay:
    `ConflictError("ipd_bill_by_accounts")`; the queue-row strip hides itself
    for those visits.
  - "Is this an IPD stay?" is answered through `ipd.service` (cross-module rule).

### 5.2 Accounts dashboard (Billing → Accounts)

The approved mockup, Screen 1, in the app's own components (shadcn/ui, Tailwind
tokens, `formatMoney`, status chips). `/billing` becomes a two-tab area:
**Accounts** (default for anyone with `accounts:read`) and **Cash counter** (the
existing board, unchanged, default for cashiers and reception).

- **Period filter:** Today · Last 7 days · This month · Custom range. Default Today.
- **Totals strip** (server-computed for the period): Collected (split by method:
  UPI / Cash / Card / other); Bills generated (count, amount); Outstanding (issued
  bills not fully paid — amount and patient count); **Running in wards** (charges
  on patients still admitted, not yet billed — amount and patient count);
  Reversals (amount).
- **Tabs, newest first, paginated, read-only:**
  - **Bills** — every invoice: date, bill no., patient (name + UHID), OPD / IPD
    (with length of stay), amount, paid, status. Actions: Open, Download PDF,
    Print. Patient name → bill history (§5.3).
  - **Payments received** — every receipt: date/time, receipt no., patient,
    bill no., method, reference, amount, received by; reversals struck through.
  - **Outstanding** — issued bills with a balance, newest first.
  - **Patients** — patients with billing activity, **latest activity first**:
    name, UHID, last visit, visits count, total cost, paid, due. Opens the cost
    journey (§5.4). Not a ranking.
- **Search:** patient name, UHID, bill number, receipt number.
- **Export:** CSV of the current tab and period.
- Banner linking to §5.8 when the hospital profile lacks a logo or address.

### 5.3 Patient bill history (`/billing/patients/[patientId]`)

Approved mockup, Screen 2. Header: patient identity + **lifetime billed · paid ·
due**. Left: every bill-bearing visit, **newest first**:

- **OPD** rows: date, doctor, amount, status — always read-only.
- **IPD** rows: admission number, admission → discharge dates, ward/bed,
  amount, status (Running / Awaiting final bill / Final / Paid).
- A patient admitted straight from an open OPD visit shows **two rows**: the OPD
  consultation (its own bill) and the IPD stay (§5.6 explains the split).

Right: the selected visit's bill (lines by category, payments, balance); the
newest visit is selected on open. For an IPD stay that is not yet final, users
with `ipd_bill:edit` get the editor (§5.5) in this pane. A **Cost journey** tab
switches the pane to §5.4.

### 5.4 Patient cost journey (read-only)

The owner's "how much each patient cost": the whole journey, itemised, **latest
to oldest**. One chronological list of everything charged to the patient across
all visits — date/time, visit (OPD / IPD admission no.), what it was
(medicine name and quantity, consumable, procedure, test, room-day,
consultation), who recorded it where the system knows (ward / pharmacy / lab),
amount, and whether it is billed, paid or waived. Filters: visit, category
(medicines · consumables · tests · procedures · room · consultation · other).
Totals per category for the filter. Read-only for everyone, including the
accountant (edits happen in §5.5). Source: billing's own charge rows — every
captured act is already a charge with a category, description, quantity and
`captured_at`.

### 5.5 IPD bill editor

For one IPD stay, from admission until its final bill exists.

- Every charge on the stay (pending and on the running draft), grouped by
  category, with SAC/HSN, qty, rate, discount, GST, line total. Waived lines
  shown struck through.
- **Add charge:** from the service catalogue (rate-card price, editable) or a
  free-text line with a price. Quantity default 1.
- **Edit charge:** quantity, unit price, discount. **Reason required.**
- **Remove charge:** waives it — the act stays on the clinical record, the
  money does not. **Reason required.** Never a hard delete.
- Changes are **staged in the browser** and visibly marked unsaved; the total
  shown is the **last saved** total. **Save** sends every change in one request;
  the server applies all or none, audits each (before/after, reason, actor),
  and returns the recalculated bill. If someone else changed the stay since the
  editor loaded, Save is refused and the editor reloads.
- **Print running bill** prints the current saved state marked
  "INTERIM — NOT A FINAL BILL".
- Backend: one endpoint, e.g. `POST /billing/accounts/{encounter_id}/charges/batch`
  taking a list of `add | edit | discount | remove` operations, each with a
  reason. New service function `edit_charge` (qty / unit price / description →
  existing `_reprice`); `add_charge`, `discount_charge`, `waive_charge` reused.

### 5.6 OPD / IPD split and the final bill

- **Separating the pre-admission OPD consultation.** When `ipd.admit_from_request`
  reuses a still-open OPD visit for the stay, every charge captured on that visit
  **before the admission** is its own **OPD bill**: gathered and issued at the
  moment of admission (frozen, payable at the counter as any OPD bill; if it was
  already paid nothing changes). The IPD bill holds only charges from the
  admission onward. When the admission opens a new IPD visit (the OPD visit had
  already closed), the split is natural and nothing extra happens.
- **Final bill after discharge.** Discharge is unchanged and not blocked
  (`ipd.discharge_patient` already accrues the last night before closing). Once
  the admission is **DISCHARGED**, the stay shows **Awaiting final bill** on the
  dashboard and history. **Generate final bill** (needs `ipd_bill:finalise`)
  gathers the remaining charges and issues the IPD invoice — refused if any line
  is unpriced or edits are unsaved, and not offered before discharge.
- After this the bill is frozen; payments are taken against it at the counter
  as today. Correction = cancel (reason) → edit → generate again.
- The issued bill **snapshots the letterhead** (name, legal name, address,
  phone, email, GSTIN, logo version) so reprints match the day of issue.

### 5.7 The printed bill

- A print-optimised page per invoice: `/billing/invoices/[invoiceId]/print`, A4,
  black on white, no app chrome. **Download PDF** and **Print** both open the
  browser's print dialog on this page (Save as PDF). No new dependency.
- Top to bottom:
  - **Letterhead** (from the invoice's snapshot; live profile for drafts): logo,
    name (legal name if set), address, phone, email, GSTIN, HFR id if set.
  - **Title:** "TAX INVOICE" when any line carries GST, otherwise "BILL OF
    SUPPLY"; drafts: "INTERIM — NOT A FINAL BILL". (*Usual GST practice; the
    owner's CA should confirm.*)
  - Bill no., date, place of supply; patient name, UHID, age/sex, phone; IPD:
    admission no., admission and discharge dates, ward/bed, treating doctor.
  - Lines by category: description, SAC/HSN, qty, rate, discount, taxable value,
    GST %, CGST, SGST/IGST, amount. Room rent always its own line.
  - Totals: taxable, each tax, grand total, **amount in words** (Indian system:
    lakh/crore), paid, balance; payments list (date, receipt no., method, ref).
  - Footer: computer-generated note, generated by / at, authorised signatory line.
- One component for OPD and IPD bills.

### 5.8 Hospital profile (Administration → Hospital profile)

- Grows the existing **Payments** admin screen into **Hospital profile**: name,
  legal name, address lines, city, state, pincode, phone, email, GSTIN, UPI ID,
  **logo upload** with preview. Fields save through the existing
  `PATCH /hospitals/{id}`.
- **Logo:** PNG or JPEG, ≤ 500 KB, stored by the backend per hospital in a small
  tenant table (`hospital_logos`: `hospital_id`, `content_type`, `data`, version,
  timestamps). A new upload adds a version; issued bills keep pointing at the
  version they were issued with. `PUT /hospitals/{id}/logo`,
  `GET /hospitals/{id}/logo[?version=]`. Works identically on local Postgres and
  Neon; no external storage service.

## 6. Constraints

- CLAUDE.md throughout: modular monolith, `service.py` is the public interface,
  cross-module reads through services, typed domain errors, every schema change
  an Alembic migration, every list endpoint paginated, RLS + `hospital_id` on
  every new table, audit log on every money-affecting action, Pydantic schemas
  for all I/O, i18n English + Hindi, tablet-first (44 px targets), no heavy UI
  dependency, lazy-load anything heavy.
- Money is `Decimal` end to end; the frontend never computes money.
- Must not regress: OPD reception collect flow, cash counter board and payments,
  settlement on death / referral / LAMA, the row locks from the double-tap fix,
  closure rules in `clinical.reevaluate_closure`, the ward discharge flow.

## 7. Research notes (how established systems do it)

- Running/interim bill vs final bill; billing validates before the final bill is
  issued at discharge — mirrored by §5.5/§5.6.
  ([SGRH billing](https://sgrh.com/billing), [Medanta billing manager role](https://www.medanta.org/jobs/manager-billing/form))
- Each admission has its own IP number; all charges tagged to it — we already
  have `admission_number`.
- Room rent as a separate line with its GST; non-ICU room rent above
  ₹5,000/day/patient is 5% GST without ITC under SAC 9993 (Notification
  03/2022-CT (Rate)). ([Grant Thornton](https://www.grantthornton.in/en/insights/blogs/non-icu-rooms-to-be-carved-out-from-healthcare-services-under-gst), [Bajaj Finserv](https://bajajfinserv.in/insurance/gst-on-hospital-room-rent))
- A BIS draft standard on hospital bill formats exists (draft — guidance only).
  ([BIS draft](https://www.services.bis.gov.in/tmp/WCSSD40327799_02092025_2.pdf))
- GST on IPD medicines/consumables is under litigation; this design does **not**
  change tax computation — rates come from the existing per-line configuration.

## 8. Left to the builder

- CSV column sets; chart or no chart on the dashboard.
- Concurrency token for the editor (version column vs `updated_at`).
- How the pre-admission charges are identified (`captured_at` before
  `admitted_at`, or tagging at capture) — behaviour in §5.6 is what matters.
- Amount-in-words implementation (no new dependency).
- Tablet layout details beyond "works in portrait and landscape".

## 9. Calls made by the designer (owner to check)

- Letterhead and logo are **snapshotted at issue**, so old bills reprint unchanged.
- "Outstanding" counts **issued bills with a balance** only; still-admitted
  patients' running charges are shown separately as **Running in wards**.
- The accountant does not take payments (no `payment:record`) — the counter does.
- Discount is offered on IPD lines (it was in the existing billing powers);
  it is an edit like any other — reason required, accountant/admin only.

## 10. Testing

- pytest: permission matrix (accountant and hospital admin may edit/finalise IPD;
  billing staff, cashier, reception, pharmacist may not — including via the old
  discount/waive/add/issue/cancel routes); automatic IPD capture still works;
  OPD edit refused; batch edit all-or-nothing; reason required; audit rows with
  before/after; stale-edit refusal; final bill only after discharge; final bill
  freezes; collect refuses IPD; pre-admission OPD split on admit-from-open-visit;
  dashboard totals incl. Running in wards; newest-first ordering; cost journey
  itemisation and filters; logo type/size limits and versioning; letterhead
  snapshot on issue; tenant isolation of logos and bills.
- Playwright: accountant: dashboard → patient → edit IPD charges → Save → total
  updates → (after discharge) Generate final bill → print page shows letterhead;
  OPD bill shows no edit controls; billing staff / cashier / reception see no
  IPD edit controls; cost journey lists medicines newest first.
