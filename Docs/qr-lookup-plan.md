# Execution Plan — QR-based patient lookup

> Companion to `Docs/execution-plan.md`. Scope is CLAUDE.md §7b's post-MVP line:
> **"QR-based patient lookup (card QR opens the profile)."**
> Written against the code as it stands after the terminal-outcome screens
> (859 backend tests, 83 e2e specs, 34 routes).

---

## 1. What this is actually for

A patient arrives at a counter holding a card or a token slip. Today the
receptionist reads the UHID off it and types `DEMO-26-000042` into the search
box — fourteen characters, hyphens included, while somebody waits. Roughly
eight seconds, and a transposed digit finds the wrong person or nobody.

The QR replaces the typing. **That is the whole feature.** It is not an
authentication mechanism, not a patient-facing app, and not a check-in kiosk.
Keeping that boundary sharp is what stops this becoming a three-week project.

### Done means

| Criterion | Target | How it is checked |
|---|---|---|
| Scan to profile on screen | **< 2 s**, one physical action | e2e timing assertion, as `reception.spec.ts` does for registration |
| Fewer steps than typing | 1 (scan) vs 3 (focus, type, select) | §7b "fewer clicks than the paper process" |
| Works on counter hardware | USB scanner, no driver, no config | manual test on real hardware — see §7 |
| Wrong-hospital card | resolves to nothing | integration test, RLS |
| Card printed before a merge | opens the **surviving** record | integration test — see §3 |
| Every scan audited | one `patient.lookup.qr` row | integration test |

---

## 2. Decision record — what goes inside the QR

**Decision: the UHID as plain text, uppercase.** `DEMO-26-000042`.

The payload is exactly the string already printed on the card in human-readable
form. That single choice is what makes the rest cheap, and the reasoning is
worth stating because the alternatives all look more sophisticated.

### Why not an opaque token

A random 128-bit token per patient is unguessable, which sounds strictly
better. It is not:

- It adds a column, an index, a backfill for every existing patient, and a
  rotation story for when a card is lost.
- It creates a **durable secret on a piece of paper**. The UHID is already
  public-ish — it is on the card, on invoices, on lab reports. A token is a new
  thing that leaks.
- It buys nothing, because the QR is not a credential. Scanning it produces a
  *search term*. The API refuses every request without a JWT and RLS scopes
  every row to the caller's hospital. A stranger holding a card still needs a
  staff login to see anything.

The one scenario where a token wins is a future patient-facing "show this code
to check yourself in" flow. That is a different feature with a different threat
model, and it should get its own opaque token then rather than contorting this
one now.

### Why not a URL

`https://hms.example/p/DEMO-26-000042` opens straight from a phone camera,
which is genuinely attractive. Rejected because:

- The camera lands on a login page, so the "it just works" benefit evaporates
  for exactly the users a phone camera would serve.
- It writes the hostname and the UHID into the camera app's history and any
  link preview the OS generates.
- It couples printed cards to a domain name. Cards outlive DNS.

### Why no format prefix

A prefix like `HMS1:DEMO-26-000042` would let a scanner recognise our own
cards. It is rejected for the MVP because it breaks the zero-code path in §4:
a USB scanner types the payload verbatim into the focused field, and a prefix
would make that field's contents not a UHID.

Our format is already recognisable by shape — `^[A-Z0-9]{2,12}-\d{2}-\d{6}$` —
which is what the scan-anywhere handler matches on. If a prefix is ever needed,
the parser strips a known one before matching, and old cards keep working.

---

## 3. Three defects to fix first

Planning this surfaced three problems in code that already exists. Two are
patient-safety issues that QR lookup would make far more likely to be hit, and
one is the reason the feature has nowhere to print to. **None of them is
optional; all three land before any QR code is generated.**

### 3.1 Search does not follow a merge — and a card is what outlives a merge

`patients.service.merge_patients` keeps the losing record and points it at the
survivor, and its own docstring says why: *"encounters, invoices and printed
cards already reference its id, and reads follow the pointer."* Safety alerts —
allergies, infectious precautions — are moved onto the survivor, and the
function says that is *"the failure mode this whole function exists to
prevent."*

`get_patient(follow_merge=True)` honours that. **`search_patients` does not.**
`find_duplicates` and `list_patients` both filter `merged_into_id IS NULL`;
search filters neither, and does not follow the pointer either.

So today, resolving a merged patient's UHID returns the dead record — the one
whose allergies were moved away. It opens a chart with **no safety banner**.

This is already reachable by typing, but a QR makes it likely rather than
theoretical: a card is precisely the artifact that survives a merge and keeps
being scanned afterwards.

**Fix.** `search_patients` excludes merged records from results and
`get_patient_by_uhid` follows the pointer, so both paths land on the survivor.
Pin with an integration test that merges two patients, gives the loser an
allergy, and asserts the old UHID resolves to the survivor **with the alert
present**.

### 3.2 `get_patient_by_uhid` has no route and no callers

It exists in `patients/service.py`, is in `__all__`, does an exact
tenant-scoped uppercase match — and nothing calls it. It is the correct
resolution primitive for a scan (exact, not the `ILIKE %term%` the search box
uses, which would match `DEMO-26-000042` against a substring of something
else).

**Fix.** Give it a route and the merge-following behaviour from 3.1.

### 3.3 The token slip carries no patient identity

`TokenSlip` prints the token number, doctor, department, location and how many
are ahead. It does **not** print the patient's name or UHID, and
`QuickOpdResponse` does not carry them. The slip a patient walks away with
cannot identify them.

That is the same identity gap found in six read models already, in the one
place where the fix is also the delivery mechanism for this feature: the slip
is where the QR goes.

**Fix.** `QuickOpdResponse` gains `patient_name` and `uhid`; the slip prints
both, and later the QR.

---

## 4. The two scan paths, in the order they are worth building

### Path A — USB/HID scanner (the default, and it is nearly free)

A counter barcode scanner presents itself as a **keyboard**. It types the
payload and sends Enter. No driver, no permission prompt, no library, and it is
what Indian hospital counters already own.

Because the payload *is* the UHID, this works against the existing search box
with zero new frontend code: press F3, scan, done. That is the MVP, and it
should be shipped and used before anything cleverer is written.

**Then** the improvement worth having: *scan anywhere*. A global key handler
that recognises a scanner by its timing signature and opens the patient without
anyone focusing a box first.

The heuristic, stated precisely because it is the part that can misfire:

- Buffer printable keydowns with their timestamps.
- A burst qualifies when: ≥ 8 characters, **median inter-key gap < 30 ms**, and
  terminated by `Enter` within 500 ms of the first character.
- The buffer must match the UHID shape. Anything else is discarded silently.
- **Ignored entirely when the active element is a `textarea`, or an `input`
  that is not the search box.** A doctor dictating a note and a nurse entering
  vitals must never have their typing hijacked, and no human types eight
  characters at 30 ms intervals — but the element guard means we never have to
  rely on that alone.
- On a match: open `/patients/{id}` (or the active encounter, see §5.4).

Every one of those thresholds gets a unit test with synthetic key sequences,
including a "fast human typist" negative case at 60 ms/char.

### Path B — camera, as progressive enhancement

Where the browser has the `BarcodeDetector` API (Chromium on Android and
desktop), a camera scan needs **no library**: `getUserMedia` plus
`BarcodeDetector.detect()` against a video frame.

- Feature-detected. Where it is missing the button is not rendered at all —
  not disabled, per the pattern used for the death-suppression release.
- **Requires a secure context.** HTTPS or localhost. A hospital deployment on
  plain HTTP has no camera, silently. Flag to the deployment checklist.
- Explicitly **not** shipping a JS decoding library. `zxing-js` and `jsQR` are
  large, and CLAUDE.md §14 says to question any dependency that bloats the
  bundle. The HID path covers the counter; the camera is a convenience for
  ward staff on a tablet.

---

## 5. Work breakdown

Vertical slices, each independently shippable, each with its own tests. Order
matters — the defects first, then read, then write, then the clever input.

### 5.1 Fix the merge gap (backend)

- `search_patients`: exclude `merged_into_id IS NOT NULL`.
- `get_patient_by_uhid`: follow `merged_into_id` to the survivor.
- Tests: `test_patients_service.py` — a merged UHID resolves to the survivor
  and carries the survivor's alerts; a merged patient does not appear twice in
  search results.

### 5.2 The lookup endpoint (backend)

- `GET /patients/by-uhid/{uhid}` → `PatientDetail`, gated on `patient:read`.
- Returns 404 for an unknown or other-tenant UHID. **404, never 403** — the
  existing rule that confirming a record exists in another tenant is itself a
  leak.
- Audited as `patient.lookup.qr` with the scanned value, so a scan is as
  traceable as `search.universal` already is.
- Tests: exact match; case-insensitive; whitespace-tolerant; another hospital's
  UHID → 404; merged UHID → survivor; audit row written.

### 5.3 Print the QR (frontend, server-rendered)

- `QuickOpdResponse` gains `patient_name` + `uhid` (defect 3.3).
- QR rendered **server-side as inline SVG**, so the client bundle pays nothing
  — the same discipline as lazy-loading Recharts, taken one step further
  because a print artifact needs no interactivity.
  - Candidate: the `qrcode` npm package's SVG string renderer, used only in a
    server component. **Verify before committing**: size, no native deps, and
    that it renders in the Next 16 server runtime. Fallback is
    `qrcode.react` behind `next/dynamic`.
- Two surfaces:
  - `TokenSlip` — name, UHID and QR added to the existing print stylesheet.
  - `/patients/[patientId]/card` — a printable patient card. New route.
- Error correction level **M**, quiet zone included, minimum 20 mm square at
  print size. A thermal slip printer at 203 dpi needs that to scan reliably;
  this is the single most common cause of "the QR does not work".

### 5.4 Resolve a scan to the right screen

A scan should land where the person scanning needs to be, which is not always
the profile:

- Patient has an **open encounter today** → their chart is what a nurse or
  doctor wants.
- Otherwise → the patient record.
- Composed in `scheduling.service.search`, which already does exactly this
  composition for token hits and already resolves an encounter for them. No new
  cross-module dependency.

### 5.5 Scan anywhere (frontend)

- `src/lib/scanner.ts` — the burst heuristic from §4, as a pure function over a
  key-event sequence so it is unit-testable without a browser.
- Wired in the app shell beside the existing F3 handler.
- Tests: scanner burst matches; fast human typing does not; a burst inside a
  textarea is ignored; a burst that is not UHID-shaped is discarded.

### 5.6 Camera scan (frontend) — **built**

Built as planned, with the detail the plan left open resolved as follows.

- `CameraScanButton` (`src/features/scan/camera-scan-button.tsx`) —
  `BarcodeDetector` only, **no decoder library**. A JavaScript QR decoder costs
  40–90 kB on every page that imports it and does nothing the platform does not
  already do natively (CLAUDE.md §14).
- `src/lib/camera-scan.ts` holds everything decidable without a camera —
  support detection, which decoded string in a frame is a card, and why a
  camera failed — so those are unit-tested and the component is left with only
  the parts that need hardware.
- **It renders nothing where it cannot work.** Three conditions: the API
  exists, the page is a secure context, `mediaDevices` exists. Windows desktop
  Chrome and Firefox fail the first, which is to say *the counter PC*. A button
  that opens a dialog to explain its own impossibility teaches staff to ignore
  buttons.
- The support check is read through `useSyncExternalStore`, not an effect: the
  answer does not exist on the server, and a `setState` in an effect is both a
  cascading render and a visible flash.
- Frame loop at 200 ms, not `requestAnimationFrame` — five looks a second is
  imperceptible against the two-second target and does not heat a tablet.
- The camera is released on every exit path, failures included. The indicator
  light stays lit until the tracks are stopped, and a camera left running on a
  shared hospital machine is how software gets removed from a ward.
- Denial, no camera, and any other failure each get their own sentence, and all
  three say the counter scanner and typing still work. Separating denial from
  "no camera" matters: telling somebody to check their permissions on a desktop
  with no webcam wastes their morning.
- What a scan *does* moved into `useResolveScan`, shared with the HID path, so
  the two devices cannot drift into resolving a card differently. `normaliseUhid`
  moved out of `scanner.ts` for the same reason — one definition of "is this a
  card", whichever device read it.

**Tests.** 16 unit tests in `e2e/camera-scan-support.spec.ts` (no browser) and
4 e2e in `e2e/camera-scan.spec.ts`. Headless Chromium has neither the API nor a
camera, so the first e2e asserts the button is **absent** unstubbed, and the
rest stub exactly two platform pieces — the decoder and the camera — leaving
everything between them genuinely exercised. Both loop-dependent tests were
confirmed to fail when detection and camera release were each broken on purpose.

**Still not covered, by construction:** whether a real lens reads a real
printed code. Same hardware gate as §7.

### 5.7 Documentation — **done**

- README has a "Scanning a card" section: payload format, why it is the UHID,
  the three ways in, and the HTTPS requirement for the camera path.
- No `.env.example` change. Print size and error correction stayed constants,
  as the plan hoped — nothing about them is per-deployment.

---

## 6. Test plan

**Backend (pytest).** Merge resolution, tenant isolation, audit, case and
whitespace handling on the UHID, unknown UHID → 404.

**Frontend (unit).** The scanner heuristic, in isolation: real-scanner timing,
fast-typist timing, wrong shape, wrong focus target.

**e2e (Playwright), `e2e/scan.spec.ts`.**

1. Register a patient, read the UHID off the slip, type it into the search box
   with a synthetic burst — the profile opens. (This is the HID scanner, faithfully:
   Playwright's keyboard *is* what a HID scanner is.)
2. A scan while a patient has an open visit lands on the chart, not the profile.
3. A burst typed into the clinical-note textarea changes nothing.
4. A merged patient's old UHID opens the surviving record, **with the allergy
   banner visible** — the safety assertion this whole plan turns on.
5. Timing: scan to profile under two seconds.

**Manual, with hardware.** The one thing no automated test covers: a real USB
scanner against a real thermal-printed slip. See §7.

---

## 7. The hardware question — the biggest risk, and it is not code

Everything above can be built and tested without a scanner. None of it proves
the feature works, because the two failure modes that actually occur are
physical:

1. **The printed QR is too small or too faint to scan.** Thermal slips at
   203 dpi, low contrast, cheap paper. Mitigated by error-correction level M
   and a 20 mm minimum, confirmed only by printing one and scanning it.
2. **The scanner's keyboard layout or suffix differs.** Some scanners send Tab
   rather than Enter, some prepend a prefix, most are configurable by scanning
   a setup barcode from their manual. The heuristic must tolerate Tab as a
   terminator, which costs one line and is easy to forget.

**Action before building 5.5/5.6:** buy one commodity USB scanner (₹1,500–2,500)
and one thermal slip printer if the pilot site does not already have them, and
test 5.3's output against them. Buying the hardware is on the critical path;
the code is not.

---

## 8. Security & DPDP

- **The QR grants nothing.** It is a lookup key for an already-authenticated
  session. Every route stays behind JWT + RBAC + RLS.
- **A lost card exposes what the card already prints.** The QR encodes the same
  UHID visible in ink on the same piece of paper. Choosing an opaque token
  would have made a lost card *more* sensitive, not less.
- **Scanning is a read of patient data and is audited as one.** DPDP Act 2023
  and NABH both require an access trail; §12 makes the audit log append-only.
  A scan writes `patient.lookup.qr`.
- **Cross-tenant scanning is structurally impossible.** UHIDs carry a hospital
  prefix and every query is RLS-scoped; hospital B scanning hospital A's card
  gets a 404, which is also what it gets for a UHID that never existed.
- **No new PII is created.** No new column, no new identifier, nothing to
  retain or erase beyond what already exists.

---

## 9. Non-goals

Stated so they do not creep in:

- Patient-facing check-in kiosks or self-service.
- QR on anything other than the token slip and the patient card — not on
  invoices, not on lab reports.
- Barcode formats other than QR (no Code 128, no PDF417).
- Offline scanning. CLAUDE.md §7b rules offline-first out entirely.
- A native or hybrid mobile app.

---

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Printed QR does not scan reliably | Medium | High — feature is useless | Error correction M, 20 mm minimum, hardware test before 5.5 |
| Scan-anywhere hijacks real typing | Low | **High — corrupts a clinical note** | Element guard *and* timing guard; unit tests for both; ship 5.5 after the plain-search path is in daily use |
| Scanner sends Tab, not Enter | Medium | Medium | Accept both terminators |
| No HTTPS at the pilot site | Medium | Low | **Handled.** `cameraScanSupport` requires a secure context, so the camera button renders nothing and the HID path is untouched. Unit-tested. Flag in the deployment checklist |
| Merge gap ships unfixed | — | **High — chart with no allergy banner** | 5.1 is first and blocks the rest |
| `qrcode` package unsuitable in the Next 16 server runtime | Low | Low | Verify in 5.3; documented fallback |

---

## 11. Sequence

```
5.1 merge gap ──▶ 5.2 endpoint ──▶ 5.3 print ──▶ ship, use it with F3 + scanner
                                       │
                                       ├──▶ 5.4 resolve to the right screen
                                       ├──▶ 5.5 scan anywhere   (after hardware test)
                                       └──▶ 5.6 camera          (optional, needs HTTPS)
```

5.1 through 5.3 are the feature. Everything after is refinement, and each piece
is worth stopping to evaluate against real use — a receptionist who is happy
pressing F3 before scanning does not need 5.5 at all, and 5.5 is where the only
serious risk in this plan lives.
