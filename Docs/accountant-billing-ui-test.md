# Accountant billing — how to test it in the browser

A click-by-click path through everything built for the accountant. Each step
says who to sign in as, what to click, and what you should see. The section
numbers in brackets point at the design (`Docs/superpowers/specs/2026-10-19-accountant-billing-design.md`).

**Before you start**

- Run the stack as usual: Postgres and Redis in Docker, API and web on your machine
  (see `Docs/runbook.md`). Open <http://localhost:3000>.
- All demo accounts use the password **`demo-password-2026`**.
- If `accountant@demo.hospital` does not sign in, run the seed once:
  `cd backend && python -m scripts.seed_demo` (it only adds what is missing).

| Account | Who it is | What they should be able to do |
|---|---|---|
| `admin@demo.hospital` | Hospital admin ("super admin") | Everything, including edit and finalise IPD bills |
| `accountant@demo.hospital` | Accountant (new) | Accounts dashboard, bill history, edit and finalise IPD bills. Takes no money |
| `reception@demo.hospital` | Reception | Queue, Get payment / Paid for OPD. Cash counter only |
| `cashier@demo.hospital` | Cashier | Cash counter only. Cannot change an IPD bill |
| `admission@demo.hospital` | Admission desk | Admits patients |

---

## 1. Hospital profile — the bill's letterhead [§5.8]

Sign in as **admin**.

1. Go to **Administration → Hospital profile** (the old "Payments" card;
   `/admin/payments` now opens this page too).
2. Fill **Address line 1**, **City**, **State**, **Pincode**, **Phone**,
   **GSTIN**. Click **Save profile** → "Hospital profile saved."
3. Under **Logo**, click **Upload logo** and pick a PNG or JPEG (≤ 500 KB).
   → "Logo uploaded." and the logo appears in the preview box.
4. Try uploading a GIF or a large photo → it is refused with
   "The logo must be a PNG or JPEG image." / "500 KB or smaller."
5. The **UPI ID** and its test QR are still here, at the bottom.

**Check:** reload the page — everything you typed and the logo are still there.

## 2. An OPD bill, made and frozen at the counter [§5.6]

1. Sign in as **reception**. Register a patient and send them to a doctor
   (Reception → Register).
2. On **Reception**, find the patient's row → tap **Paid** twice (cash).
   The row shows the paid chip.

That OPD bill is now issued and frozen: nobody changes it in place. Only the
accounts office can correct it, by issuing a corrected bill (section 8b).

## 3. The accountant's dashboard [§5.2]

Sign out, sign in as **accountant**.

1. Click **Billing**. It opens on the **Accounts** tab (Cash counter is the
   other tab).
2. The **totals strip** shows Collected (split UPI / Cash / Card), Bills
   generated, Still outstanding, Running in wards, Refunds / reversals.
3. **Bills** tab: the patient from step 2 is the **first row** (newest first),
   marked **OPD**, **Paid**.
4. Try the period buttons: **Today**, **Last 7 days**, **This month**,
   **Custom** (pick two dates).
5. Type part of the patient's name, their UHID or the bill number into the
   search box → the list narrows.
6. **Payments received** tab: the receipt is at the top, with method and
   "Received by".
7. **Outstanding** tab: issued bills not fully paid. **Patients** tab:
   patients by latest activity (not a ranking).
8. Click **Export CSV** → a `.csv` of the current tab downloads.
9. Each bill row has three buttons:
   - **Open** → the patient's bill page, with that bill selected and locked
     (see section 8b).
   - **Print** → the A4 bill prints straight away; you stay on the dashboard.
   - **PDF** → the same print window opens; choose **Save as PDF**. The file
     is named after the bill number.

**Check:** sign in as **reception** and open Billing → only the **Cash
counter**, no Accounts tab.

## 4. Admit a patient from an open OPD visit [§5.6]

1. As **reception**: register a patient, mark them **Seen**, then **Send to
   admission**.
2. As **admission desk**: open **Admissions**, admit the patient into a bed.

The consultation fee from that visit is frozen at this moment as its **own
OPD bill**. The IPD bill starts from the admission.

## 5. The patient's bill history [§5.3]

As **accountant**: Billing → Accounts → **Patients** tab → search the
patient from step 4 → click their name.

1. The header shows the patient and **lifetime billed · paid · due**.
2. On the left, **two visits**, newest first: **IPD admission** (selected)
   and **OPD** with the ₹500 consultation.
3. Click the **OPD** visit → its bill lines (OPD consultation) and receipts.
   Locked, with **Edit**, **PDF** and **Print** (section 8b).

## 6. Edit the IPD bill — totals change only on Save [§5.5]

Still on the patient's page, IPD visit selected.

1. Note the **Saved total**.
2. Click **Add charge**. Leave Service on "Something else — type it", fill
   **Description** "Dressing kit", **Unit price** 240, **Reason** "Used on
   the ward". Click **Add to bill**.
   → A yellow box says **"1 unsaved change"**. The Saved total has **not** moved.
3. Click **Save changes** → "Bill saved." The total goes up.
4. On the Dressing kit line click the **pencil** (Edit) → Quantity 2, Reason
   "Two kits" → **Stage change** → **Save changes**. The total goes up again.
5. Try the **%** (Discount) and **bin** (Remove) icons. Remove keeps the line,
   struck through as "Removed (waived)". The care stays on the record, the
   money doesn't. Every change needs a reason.
6. Click **Print running bill** → the A4 bill titled **"INTERIM — NOT A FINAL
   BILL"** prints without leaving the page.

**Check the audit trail:** there is no audit screen yet, so use the API. Signed
in as **admin**, open
<http://localhost:3000/bff/audit-logs?action=billing.ipd_bill.edit> in the
same browser. Each change is listed with before, after, reason and who made it.

**Check the lock:** open the same patient in two tabs, save a change in one,
then save a different change in the other → the second save is refused
("Someone else changed this bill…"), the editor reloads, and your unsaved
change is **kept** — check it and click **Save changes** again. The same
happens if you leave a tab with unsaved changes and come back after the bill
reloaded in the background: the save is still checked against the bill you
started from.

## 7. The final bill, after discharge [§5.6]

1. **Generate final bill** is not offered while the patient is still in a bed.
2. As **admin**: open the admission (Wards → the bed) → **Discharge** →
   Outcome **Recovered** → Discharge.
3. As **accountant**: open the patient again → the IPD visit says
   **Awaiting final bill** → click **Generate final bill** → confirm.
   → "Final bill INV-… generated."
4. The editor's Add / Edit / Remove buttons are gone: the final bill is frozen.
   It shows its number, status and balance, with **Open** and **Print**.
5. The counter takes payment against it as usual (Billing → Cash counter).
6. **Correct a final bill:** click **Cancel final bill**, give a reason →
   "Final bill INV-… cancelled." The editor opens again: fix the lines, Save,
   then **Generate final bill** again. (Refused while a payment stands
   against the bill — reverse the payment first.)

Also works when the stay ends by **death, LAMA or referral** instead of a
discharge.

## 8. The printed bill [§5.7]

From a dashboard bill row (**Print** / **PDF**), or from any bill on the
patient's page:

- **Print** prints the A4 bill without leaving the page. **PDF** opens the
  same print window — choose "Save as PDF" to download it.
- Top: your **logo, name, address, phone, GSTIN** (from step 1).
- Title: **TAX INVOICE** when any line carries GST, otherwise **BILL OF SUPPLY**.
- Patient name, UHID, age/sex. For IPD: admission number, dates, ward/bed, doctor.
- Lines by category with SAC/HSN, qty, rate, discount, taxable value, GST %,
  CGST, SGST, amount. Then totals, the **amount in words** (e.g. "Rupees One
  Lakh Fifty Thousand Only") and the payments received.

**Check the snapshot:** change the address in the Hospital profile and reprint
an *old* bill → it still shows the **old** address. Bills keep the letterhead
of the day they were issued.

## 8b. Open a bill, edit it, print it [§5.5, §5.6]

As **accountant** (or admin): Billing → Accounts → any bill row → **Open**.

1. You land on the patient's bill page: their visits on the left, newest
   first, and the bill you opened on the right — **locked**, with **Edit**,
   **PDF** and **Print**. Pick any other visit or bill on the left to print
   that one instead.
2. Click **Edit**. The lines unlock (pencil, %, bin per line, and **Add
   charge**). Change what you need; each change asks for a reason and is
   listed as unsaved.
3. Click **Issue corrected bill** → give the reason for the correction →
   **Issue corrected bill**.
   → "Corrected bill INV-… issued; INV-… cancelled."
4. The old bill now shows **Cancelled**; the new one carries the changes,
   under a new number, with the receipts already paid moved onto it (e.g.
   **Part paid** if the total went up).
5. A correction that would bring the total below what was already paid is
   refused — reverse the payment first.

The same **Edit** is on a final IPD bill. A running IPD bill (patient still
admitted) needs no Edit: it is editable directly (section 6).

## 9. Who cannot do what [§5.1]

- **Reception's Paid** on an admitted patient: there is no Get payment / Paid
  strip on the row. The IPD bill is the accountant's.
- **Cashier** opening `/billing/patients/<any id>` → "You do not have
  permission to open this."
- **Cashier** opening an admitted patient on the cash counter → no
  **Assemble draft** or **Issue** for the IPD bill, only the note "This
  patient is admitted. The IPD bill is made by the accounts office."
  (The server refuses those calls too.)
- **Hospital admin** cannot issue a stay's IPD bill early from the cash
  counter either: issuing it *is* generating the final bill, so it waits for
  discharge.

## 10. The consultation bill of an admitted patient is never lost [§5.6]

The consultation from before the admission is an OPD bill, issued at
admission. If it is cancelled to correct it, its lines wait for an OPD bill
again — never for the IPD one.

1. Cancel it: the counter has no Cancel button for an OPD bill yet (this
   predates the accountant work), so use the API — signed in as **billing
   staff** or **admin**, `POST /bff/billing/invoices/<invoice id>/cancel` with
   `{"reason": "Wrong fee"}`.
2. Billing → Cash counter → the admitted patient: the consultation is under
   pending charges and an **Issue OPD bill** button appears → click it →
   "OPD bill INV-… issued."
3. If nobody re-issues it, generating the final IPD bill issues it as its own
   OPD bill first — the IPD bill still holds only the stay.

---

### Automated tests covering the same path

- Backend: `backend/tests/test_billing_accounts.py`, `test_api_accounts.py`, `test_bill_correction.py`,
  `test_api_accounts_read.py`, `test_api_hospital_logo.py`.
- Browser (Playwright): `frontend/e2e/hospital-profile.spec.ts`,
  `accounts.spec.ts`, `patient-bills.spec.ts`, `ipd-bill-editor.spec.ts`, `bill-correction.spec.ts`,
  `printed-bill.spec.ts`.
- Unit: `frontend/src/features/billing/print/amount-in-words.test.mjs`
  (`npm run test:unit`).
