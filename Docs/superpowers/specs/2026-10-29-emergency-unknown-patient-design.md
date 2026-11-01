# Emergency / unidentified patient (UX-01)

## Why

An unconscious road-accident patient arrives with no name and no mobile, but
registration requires both, and the registration screen offers no Emergency
priority (the API has `QueuePriority.EMERGENCY = 10`). Casualty staff will type
fake data, which breaks duplicate detection and sends messages to made-up
numbers. The system needs one fast, honest path for "we don't know who this is
yet", and a safe way to fill in who they are later.

## Decisions (agreed)

- **One tap to register.** A red **Emergency** button on Reception (shortcut
  **F9**) opens `/reception/emergency`. The only choices are: gender (Male / Female /
  Unknown, three big buttons), an approximate age (optional), the doctor (remembered
  on the device) and an optional "also send to the admission desk".
- **One transaction.** `POST /queue/emergency` does all of this in one database
  transaction, so an unknown patient never exists without a visit:
  - creates the unidentified patient;
  - books and checks them in with priority EMERGENCY (top of the doctor's line);
  - opens an `EMERGENCY` encounter;
  - optionally files an admission request.

  The state machine already allows REGISTERED → ADMITTED.
- **The record.** The patient is named `Unknown Male EM-0034`, with no phone,
  `is_unidentified = true` and `emergency_tag = EM-0034`. The EM number is a
  per-hospital, per-year counter in the existing `uhid_sequences` table, which
  gains a `series` column. The patient also gets a normal UHID, for wristband,
  samples and bills.
- **Identification later: the emergency record stays the main one.**
  `POST /patients/{id}/identify` takes one of two forms:
  - **New person:** real name, mobile, gender and age are written onto the
    emergency record.
  - **Existing record** (`existing_patient_id`):
    - the old record is folded into the emergency record using the existing
      merge mechanics: allergies, consents and identifiers move across, blanks are
      filled, and the old card still resolves through the merge pointer;
    - the old record's name, mobile, gender, birth date and language become the
      emergency record's.

  Because the live visit, queue row, admission and bills already point at the
  emergency record, none of them needs to move.
- **Reception may identify**, including linking to an existing record, with
  `patient:update`. This is a narrowly scoped link; the general merge still needs
  `patient:merge`.

## Invariants (each enforced and tested)

1. **No phone ⇔ unidentified.** A DB CHECK constraint
   (`phone IS NOT NULL OR is_unidentified`) enforces it, and identification
   requires a mobile.
2. **No patient messages while unidentified.** A new `SuppressionReason.UNIDENTIFIED`
   is checked in both layers (enqueue and dispatch). DECEASED still outranks it. It
   cannot be set by hand, and nothing raised during that time is re-sent after
   identification.
3. **Identity changes only through identify.**
   - `PATCH /patients/{id}` refuses a name or phone change on an unidentified
     record (`identify_first`).
   - The general merge refuses either side being unidentified (`use_identify`).
   - Identify refuses an already-identified record (`already_identified`).
4. **A dead record never makes a live patient dead.** Linking to an existing record
   that is deceased, merged, unidentified, the same record, or another hospital's
   is refused. The merge would otherwise copy `is_deceased` onto the patient in
   casualty.
5. **Unknown records never pollute duplicate detection.** They are excluded from
   `find_duplicates` (generic names, no phone).
6. **The identified name shows everywhere.** This needs no extra work: the record
   is updated in place, so boards, bills and wristbands show the real name.

## Also in scope

- The normal registration form gains an **Emergency** toggle for a known patient:
  priority EMERGENCY, reason "Emergency", and an EMERGENCY encounter type.
  Quick-OPD with priority EMERGENCY opens an EMERGENCY encounter.
- **Reception board:**
  - a "Needs identification" strip, listing unidentified patients each with an
    **Identify** button;
  - queue rows show a "Not identified" marker;
  - emergency rows are red.
- **Patient record:** a "Not yet identified" banner with an Identify button.
- **Every place that shows a patient's phone** handles none: index, record header,
  profile, duplicate warning, bills, search subtitle and admission desk.
- English and Hindi copy; backend unit and API tests; a Playwright e2e test.

## Out of scope

- A server-side "duty doctor" roster. The device remembers the last emergency
  doctor instead.
- Re-pointing visits from an old record. This is not needed under the chosen
  direction.

## Build order (TDD throughout)

1. Migration:
   - `patients.phone` nullable, plus `is_unidentified`, `identified_at` and
     `emergency_tag`, and the CHECK constraint;
   - `uhid_sequences.series` and a new unique index;
   - `suppressionreason += UNIDENTIFIED`.
2. Patients service:
   - `register_unidentified`;
   - `identify_patient`;
   - the guards in `update_patient` and `merge_patients`;
   - the duplicate exclusion;
   - the list filter.
3. Notifications: the UNIDENTIFIED suppression.
4. `POST /queue/emergency`; quick-OPD EMERGENCY opens an EMERGENCY encounter; board
   rows carry `patient_is_unidentified`.
5. Frontend: emergency page and F9, identify dialog, needs-identification strip,
   badges, nullable phone, i18n.
6. Full backend suite, ruff, mypy, tsc, eslint, codegen check, e2e.
