"""Assembling a discharge summary out of what the hospital already recorded.

This file is CLAUDE.md §7 applied to the single worst piece of paperwork in a
hospital. A discharge summary is the document that takes a consultant forty
minutes at the end of a long day, which is why it is so often written a week
late, from memory, or not at all — and why the patient who needs it most, the
one being handed back to a GP with six new medicines, is the one who leaves
without it.

Almost none of it is new information. The diagnoses are in `clinical`. The
course of the stay is in the progress and nursing notes. The investigations are
in `diagnostics`. The drugs are on the chart in this module. The compiler's job
is to gather that into sections a doctor **edits**, so the clinical act is
review and signature rather than transcription.

Two rules govern what goes in:

* **Compile, never invent.** Every section is derived from a row somebody
  already wrote. Where there is nothing, the section is left empty for a human
  rather than filled with a plausible sentence — a summary that reads well and
  says something nobody recorded is worse than a blank one, because the blank
  one gets noticed.
* **Attribute what is quoted.** The course of the stay is other people's notes,
  so it carries their names and dates. A consultant signing the document needs
  to see whose observation they are putting their name to.

Kept free of I/O: everything here takes already-loaded rows and returns text or
a dict. That makes the interesting part — what the document says — testable
without a database, and keeps the queries in `service.py` where they belong.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

__all__ = [
    "SECTION_FIELDS",
    "compile_sections",
    "format_course",
    "format_diagnoses",
    "format_investigations",
    "format_medications",
]


# The sections the compiler fills. Named here so `service.apply_compiled` and
# the amendment path agree on the list without either duplicating it.
SECTION_FIELDS: tuple[str, ...] = (
    "presenting_complaint",
    "diagnoses",
    "history",
    "examination",
    "course_in_hospital",
    "investigations",
    "procedures",
    "treatment_given",
    "discharge_medications",
    "follow_up_instructions",
)


# ---------------------------------------------------------------------------
# Structural protocols.
#
# Deliberately duck-typed rather than importing `clinical.models` and
# `diagnostics.models`. This file is pure formatting, and a Protocol keeps it
# testable with three-line stand-ins instead of a database round trip per case —
# which is what makes it practical to test the awkward inputs (a note with no
# author, a report with no impression) rather than only the happy path.
# ---------------------------------------------------------------------------
class NoteLike(Protocol):
    note_type: Any
    content: str
    author_name: str
    created_at: datetime


class DiagnosisLike(Protocol):
    description: str
    code: str | None
    code_system: str
    is_primary: bool
    diagnosis_type: Any


class ReportLike(Protocol):
    test_name: str
    impression: str | None
    findings: str | None
    report_number: str
    created_at: datetime


class MedicationLike(Protocol):
    drug_name: str
    dose: str
    route: Any
    frequency: str
    is_prn: bool
    prn_indication: str | None
    instructions: str | None
    is_active: bool


def _value(item: Any) -> str:
    """Enum-or-string, without caring which."""
    return str(getattr(item, "value", item))


def _stamp(moment: datetime) -> str:
    return moment.strftime("%d %b %Y")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def format_diagnoses(diagnoses: Sequence[DiagnosisLike]) -> str:
    """Final diagnoses first, primary at the top, coded where coded.

    Provisional diagnoses are included but labelled. Dropping them would be
    tidier and wrong: a working diagnosis that was later excluded is part of the
    reasoning, and the GP reading this needs to know what was considered.
    """
    if not diagnoses:
        return ""

    def rank(item: DiagnosisLike) -> tuple[int, int]:
        final = 0 if _value(item.diagnosis_type) == "FINAL" else 1
        return (final, 0 if item.is_primary else 1)

    lines: list[str] = []
    for item in sorted(diagnoses, key=rank):
        parts = [item.description]
        if item.code:
            parts.append(f"({item.code_system} {item.code})")
        label = []
        if item.is_primary:
            label.append("primary")
        if _value(item.diagnosis_type) != "FINAL":
            label.append(_value(item.diagnosis_type).lower())
        if label:
            parts.append(f"[{', '.join(label)}]")
        lines.append("- " + " ".join(parts))
    return "\n".join(lines)


def format_course(notes: Sequence[NoteLike]) -> str:
    """The stay, told chronologically out of the progress and nursing notes.

    Attributed and dated, because a consultant signing this is putting their
    name to other people's observations and should be able to see whose.
    """
    relevant = [
        note for note in notes if _value(note.note_type) in ("PROGRESS", "NURSING", "ASSESSMENT")
    ]
    if not relevant:
        return ""

    lines: list[str] = []
    for note in sorted(relevant, key=lambda item: item.created_at):
        author = note.author_name or "Unattributed"
        lines.append(f"{_stamp(note.created_at)} — {author}\n{note.content.strip()}")
    return "\n\n".join(lines)


def format_investigations(reports: Sequence[ReportLike]) -> str:
    """What was tested and what it showed.

    The impression is preferred over the findings: it is the line a clinician
    reads first, and a summary that pastes three paragraphs of radiological
    description in place of "no acute intracranial abnormality" is a summary
    nobody finishes.
    """
    if not reports:
        return ""

    lines: list[str] = []
    for report in sorted(reports, key=lambda item: item.created_at):
        conclusion = (report.impression or report.findings or "").strip()
        head = f"- {_stamp(report.created_at)} {report.test_name} ({report.report_number})"
        lines.append(f"{head}: {conclusion}" if conclusion else head)
    return "\n".join(lines)


def format_medications(orders: Sequence[MedicationLike], *, active_only: bool = False) -> str:
    """The drug chart, as a list a patient and a pharmacist can both read.

    `active_only` produces the take-home list — what was still running when the
    patient left. The full list goes in "treatment given", because a course of
    antibiotics that finished on day three is part of the treatment and not part
    of what they should keep taking.
    """
    selected = [order for order in orders if order.is_active] if active_only else list(orders)
    if not selected:
        return ""

    lines: list[str] = []
    for order in selected:
        parts = [order.drug_name, order.dose, _value(order.route).replace("_", " ").title()]
        if order.is_prn:
            parts.append(f"as needed ({order.prn_indication or 'see chart'})")
        else:
            parts.append(order.frequency)
        line = "- " + " — ".join(part for part in parts if part)
        if order.instructions:
            line += f" ({order.instructions})"
        lines.append(line)
    return "\n".join(lines)


def _first_note(notes: Sequence[NoteLike], note_type: str) -> str:
    """The earliest note of a type — the admission's version, not a later revision."""
    matching = [note for note in notes if _value(note.note_type) == note_type]
    if not matching:
        return ""
    return min(matching, key=lambda item: item.created_at).content.strip()


def compile_sections(
    *,
    notes: Sequence[NoteLike],
    diagnoses: Sequence[DiagnosisLike],
    reports: Sequence[ReportLike],
    medications: Sequence[MedicationLike],
    chief_complaint: str | None = None,
    procedures: Sequence[str] = (),
) -> dict[str, str]:
    """Build every section. Empty strings where the hospital recorded nothing.

    An empty section is left empty on purpose. The alternative — "unremarkable",
    "as documented" — reads like a finished document and hides the fact that
    nobody wrote anything, which is the one thing the signing doctor most needs
    to notice.
    """
    return {
        "presenting_complaint": (chief_complaint or "").strip()
        or _first_note(notes, "CHIEF_COMPLAINT"),
        "diagnoses": format_diagnoses(diagnoses),
        "history": _first_note(notes, "HISTORY"),
        "examination": _first_note(notes, "EXAMINATION"),
        "course_in_hospital": format_course(notes),
        "investigations": format_investigations(reports),
        "procedures": "\n".join(f"- {item}" for item in procedures),
        "treatment_given": format_medications(medications),
        "discharge_medications": format_medications(medications, active_only=True),
        "follow_up_instructions": _first_note(notes, "ADVICE"),
    }


def provenance(
    *,
    notes: Sequence[NoteLike],
    diagnoses: Sequence[DiagnosisLike],
    reports: Sequence[ReportLike],
    medications: Sequence[MedicationLike],
) -> dict[str, Any]:
    """What the compiler had to work with, recorded alongside the draft.

    Kept so it is possible to answer "was this section blank because nobody
    wrote a note, or because the compiler dropped it?" — a question that comes
    up the first time a summary looks thin, and is unanswerable afterwards
    without it.
    """
    return {
        "notes": len(notes),
        "diagnoses": len(diagnoses),
        "reports": len(reports),
        "medications": len(medications),
        "active_medications": sum(1 for order in medications if order.is_active),
    }
