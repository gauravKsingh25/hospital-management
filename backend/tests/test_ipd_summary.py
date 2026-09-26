"""The discharge-summary compiler.

No database. `summary.py` is deliberately I/O-free so the interesting question —
*what does the document say* — can be tested against awkward inputs rather than
only the happy path, which is where a compiler like this actually fails: the
note with no author, the report with no impression, the stay where nobody wrote
anything at all.

The rule under test throughout is **compile, never invent**. A section with no
source rows comes out empty, not filled with "unremarkable". A summary that
reads like a finished document while saying something nobody recorded is worse
than an obviously blank one, because the blank one gets noticed by the doctor
signing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.modules.ipd import summary


# ---------------------------------------------------------------------------
# Stand-ins. `summary.py` is duck-typed against Protocols precisely so these
# can be three-line dataclasses instead of database rows.
# ---------------------------------------------------------------------------
@dataclass
class FakeNote:
    note_type: str
    content: str
    author_name: str = "Dr Rao"
    created_at: datetime = field(default_factory=lambda: datetime(2026, 8, 1, tzinfo=UTC))


@dataclass
class FakeDiagnosis:
    description: str
    code: str | None = None
    code_system: str = "ICD-10"
    is_primary: bool = False
    diagnosis_type: str = "FINAL"


@dataclass
class FakeReport:
    test_name: str
    impression: str | None = None
    findings: str | None = None
    report_number: str = "RPT-001"
    created_at: datetime = field(default_factory=lambda: datetime(2026, 8, 2, tzinfo=UTC))


@dataclass
class FakeMedication:
    drug_name: str
    dose: str = "500 mg"
    route: str = "ORAL"
    frequency: str = "BD"
    is_prn: bool = False
    prn_indication: str | None = None
    instructions: str | None = None
    is_active: bool = True


# ---------------------------------------------------------------------------
# Diagnoses
# ---------------------------------------------------------------------------
def test_the_primary_final_diagnosis_leads() -> None:
    """What the GP reading this needs first, first."""
    text = summary.format_diagnoses(
        [
            FakeDiagnosis("Anaemia", diagnosis_type="PROVISIONAL"),
            FakeDiagnosis("Community-acquired pneumonia", code="J18.9", is_primary=True),
            FakeDiagnosis("Type 2 diabetes mellitus", code="E11.9"),
        ]
    )
    lines = text.splitlines()
    assert lines[0] == "- Community-acquired pneumonia (ICD-10 J18.9) [primary]"
    assert "Type 2 diabetes" in lines[1]
    assert lines[2] == "- Anaemia [provisional]"


def test_provisional_diagnoses_are_kept_but_labelled() -> None:
    """Dropping them would be tidier and wrong.

    A working diagnosis that was later excluded is part of the reasoning, and
    the clinician picking this patient up needs to know what was considered.
    """
    text = summary.format_diagnoses(
        [FakeDiagnosis("Pulmonary embolism", diagnosis_type="PROVISIONAL")]
    )
    assert "[provisional]" in text


def test_no_diagnoses_produces_an_empty_section_not_a_placeholder() -> None:
    assert summary.format_diagnoses([]) == ""


# ---------------------------------------------------------------------------
# Course in hospital
# ---------------------------------------------------------------------------
def test_the_course_is_chronological_and_attributed() -> None:
    """A consultant signing this is putting their name to other people's notes."""
    text = summary.format_course(
        [
            FakeNote(
                "PROGRESS",
                "Afebrile, chest clearing.",
                author_name="Dr Iyer",
                created_at=datetime(2026, 8, 3, tzinfo=UTC),
            ),
            FakeNote(
                "NURSING",
                "Tolerating oral intake.",
                author_name="Sr Fernandes",
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
        ]
    )
    assert text.index("Sr Fernandes") < text.index("Dr Iyer")
    assert "01 Aug 2026 — Sr Fernandes" in text
    assert "03 Aug 2026 — Dr Iyer" in text


def test_the_course_ignores_note_types_that_belong_in_other_sections() -> None:
    """A chief complaint is the presenting complaint, not the ward course."""
    text = summary.format_course(
        [FakeNote("CHIEF_COMPLAINT", "Cough for five days"), FakeNote("ADVICE", "Rest")]
    )
    assert text == ""


def test_an_unattributed_note_says_so_rather_than_being_dropped() -> None:
    """Losing the observation would be worse than admitting we cannot name its author."""
    text = summary.format_course([FakeNote("PROGRESS", "Patient stable.", author_name="")])
    assert "Unattributed" in text
    assert "Patient stable." in text


# ---------------------------------------------------------------------------
# Investigations
# ---------------------------------------------------------------------------
def test_the_impression_is_preferred_over_the_findings() -> None:
    """The line a clinician reads first, rather than three paragraphs of description."""
    text = summary.format_investigations(
        [
            FakeReport(
                "CT Head",
                impression="No acute intracranial abnormality.",
                findings="Extensive description of normal anatomy across many lines.",
            )
        ]
    )
    assert "No acute intracranial abnormality." in text
    assert "Extensive description" not in text


def test_a_report_with_no_conclusion_is_still_listed() -> None:
    """That the test was done is itself information, even unreported."""
    text = summary.format_investigations([FakeReport("Chest X-ray", report_number="RPT-009")])
    assert "Chest X-ray" in text
    assert "RPT-009" in text
    assert text.rstrip().endswith("(RPT-009)")


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------
def test_discharge_medications_are_only_what_is_still_running() -> None:
    """The take-home list, not the whole stay.

    A course of antibiotics that finished on day three is treatment given, not
    something the patient should keep taking — and a discharge list that says
    otherwise is a patient taking antibiotics for another week.
    """
    orders = [
        FakeMedication("Amoxicillin", is_active=False),
        FakeMedication("Metformin", dose="500 mg", frequency="OD"),
    ]
    take_home = summary.format_medications(orders, active_only=True)
    everything = summary.format_medications(orders)

    assert "Amoxicillin" not in take_home
    assert "Metformin" in take_home
    assert "Amoxicillin" in everything and "Metformin" in everything


def test_an_as_needed_drug_carries_its_indication() -> None:
    """ "Paracetamol as needed" without saying when is not a prescription."""
    text = summary.format_medications(
        [
            FakeMedication(
                "Paracetamol",
                is_prn=True,
                prn_indication="for temperature above 38",
                frequency="SOS",
            )
        ]
    )
    assert "as needed (for temperature above 38)" in text


# ---------------------------------------------------------------------------
# The whole document
# ---------------------------------------------------------------------------
def test_a_stay_with_nothing_recorded_compiles_to_empty_sections() -> None:
    """The rule: compile, never invent.

    Every section comes back empty rather than plausible. An obviously blank
    summary gets noticed by the doctor asked to sign it; a fluent one that says
    something nobody recorded does not.
    """
    sections = summary.compile_sections(notes=[], diagnoses=[], reports=[], medications=[])

    assert set(sections) == set(summary.SECTION_FIELDS)
    assert all(value == "" for value in sections.values())


def test_the_chief_complaint_on_the_encounter_wins_over_a_note() -> None:
    """It is what reception typed at the door, and it is usually the right one."""
    sections = summary.compile_sections(
        notes=[FakeNote("CHIEF_COMPLAINT", "From the note")],
        diagnoses=[],
        reports=[],
        medications=[],
        chief_complaint="Fever and cough for five days",
    )
    assert sections["presenting_complaint"] == "Fever and cough for five days"


def test_the_earliest_history_note_is_used_not_a_later_revision() -> None:
    """The admission's version of the history, not one rewritten on day four."""
    sections = summary.compile_sections(
        notes=[
            FakeNote("HISTORY", "Rewritten later", created_at=datetime(2026, 8, 5, tzinfo=UTC)),
            FakeNote("HISTORY", "Taken on admission", created_at=datetime(2026, 8, 1, tzinfo=UTC)),
        ],
        diagnoses=[],
        reports=[],
        medications=[],
    )
    assert sections["history"] == "Taken on admission"


def test_provenance_records_what_the_compiler_had_to_work_with() -> None:
    """So "was this blank because nobody wrote a note?" stays answerable."""
    record = summary.provenance(
        notes=[FakeNote("PROGRESS", "x")],
        diagnoses=[FakeDiagnosis("Pneumonia")],
        reports=[],
        medications=[FakeMedication("Metformin"), FakeMedication("Amoxicillin", is_active=False)],
    )
    assert record == {
        "notes": 1,
        "diagnoses": 1,
        "reports": 0,
        "medications": 2,
        "active_medications": 1,
    }
