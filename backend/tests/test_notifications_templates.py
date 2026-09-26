"""Template rendering: safety, totality, and the language fallback.

No database. The interesting failure modes here are textual, and the two that
matter are worth naming:

* `test_a_template_cannot_reach_into_the_objects_it_renders` — the reason this
  module does not use `str.format`. Template bodies are hospital-authored data
  editable through the admin API, and `format` traverses attributes.
* `test_rendering_never_raises_...` — the reason a hospital with half its copy
  written can still discharge a patient.
"""

from __future__ import annotations

from app.modules.notifications import templates as tpl
from app.modules.notifications.models import NotificationCategory, NotificationChannel


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------
def test_placeholders_are_filled_from_the_context() -> None:
    result = tpl.render(
        "{{hospital_name}}: your {{test_name}} report is ready.",
        {"hospital_name": "Sunrise Hospital", "test_name": "CBC"},
    )

    assert result.body == "Sunrise Hospital: your CBC report is ready."
    assert result.missing == ()
    assert result.is_complete


def test_a_missing_placeholder_leaves_a_hole_and_is_reported() -> None:
    """A sentence with a gap in it beats a message that never went out — but the
    gap is reported rather than hidden, so somebody can fix the trigger."""
    result = tpl.render("Dear {{patient_name}}, see {{doctor_name}}.", {"patient_name": "Asha"})

    assert result.body == "Dear Asha, see ."
    assert result.missing == ("doctor_name",)
    assert not result.is_complete


def test_an_explicit_none_is_a_value_not_a_gap() -> None:
    """ "No doctor assigned" is a legitimate fact. Reporting it as a missing
    placeholder would send somebody hunting for a configuration bug that is not
    there."""
    result = tpl.render("Doctor: {{doctor_name}}.", {"doctor_name": None})

    assert result.body == "Doctor: ."
    assert result.missing == ()


def test_booleans_render_as_words_rather_than_python_repr() -> None:
    assert tpl.render("Signed: {{signed}}", {"signed": True}).body == "Signed: yes"
    assert tpl.render("Signed: {{signed}}", {"signed": False}).body == "Signed: no"


def test_a_repeated_placeholder_is_filled_every_time() -> None:
    result = tpl.render("{{name}}, {{name}}, {{name}}", {"name": "Ravi"})
    assert result.body == "Ravi, Ravi, Ravi"


def test_the_subject_is_rendered_too_and_shares_the_missing_list() -> None:
    result = tpl.render(
        "Body for {{patient_name}}.", {"other": "x"}, subject="Report for {{patient_name}}"
    )

    assert result.subject is None or "Report for" in result.subject
    assert "patient_name" in result.missing


def test_an_empty_subject_becomes_none_rather_than_an_empty_string() -> None:
    """An email with a subject header containing nothing looks broken; an email
    with no subject header at all is merely terse."""
    result = tpl.render("body", {}, subject="{{missing_thing}}")
    assert result.subject is None


# ---------------------------------------------------------------------------
# Injection safety — the reason this is not str.format
# ---------------------------------------------------------------------------
def test_a_template_cannot_reach_into_the_objects_it_renders() -> None:
    """The published format-string injection attack, run against our renderer.

    With `str.format` this reaches `SECRET_KEY` through `__globals__`. Here the
    text is not a valid placeholder — braces are single, and the name contains
    dots and brackets — so it is left on the page verbatim and nothing is
    evaluated.
    """
    hostile = "{patient.__class__.__init__.__globals__[SECRET_KEY]}"

    result = tpl.render(hostile, {"patient": object()})

    assert result.body == hostile
    assert result.missing == ()


def test_a_double_braced_attribute_path_is_not_a_placeholder_either() -> None:
    """Even in our own syntax, only a plain key is a placeholder. No traversal."""
    result = tpl.render("{{patient.phone}} {{a[0]}} {{OS.environ}}", {"patient": "x"})

    assert result.body == "{{patient.phone}} {{a[0]}} {{OS.environ}}"
    assert result.missing == ()


def test_a_substituted_value_containing_a_placeholder_is_not_re_expanded() -> None:
    """One pass, not a loop. A patient who registers as "{{secret}}" gets their
    silly name printed, not a second round of substitution."""
    result = tpl.render("Hello {{name}}", {"name": "{{hospital_name}}", "hospital_name": "Leak"})

    assert result.body == "Hello {{hospital_name}}"
    assert "Leak" not in result.body


def test_placeholders_in_lists_names_without_duplicates_in_order() -> None:
    assert tpl.placeholders_in("{{b}} {{a}} {{b}} {{c}}") == ("b", "a", "c")


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
def test_language_preference_falls_back_to_english() -> None:
    assert tpl.language_preference("hi") == ("hi", "en")
    assert tpl.language_preference("en") == ("en",)
    assert tpl.language_preference(None) == ("en",)


def test_a_regional_locale_is_normalised_to_its_base_language() -> None:
    """A hospital writes one Hindi template, not one per state. A lookup that
    misses on a suffix nobody set is a message that silently drops to English."""
    assert tpl.language_preference("hi-IN") == ("hi", "en")
    assert tpl.language_preference("HI_in") == ("hi", "en")


# ---------------------------------------------------------------------------
# Shipped defaults
# ---------------------------------------------------------------------------
def test_every_template_code_ships_english_copy() -> None:
    """A code the system can trigger with no shipped copy would send the generic
    fallback line to a real patient. This test is what stops that landing."""
    codes = [
        value
        for name, value in vars(tpl.TemplateCode).items()
        if not name.startswith("_") and isinstance(value, str)
    ]

    assert codes
    for code in codes:
        assert code in tpl.DEFAULT_TEMPLATES[tpl.DEFAULT_LANGUAGE], f"{code} has no English copy"


def test_every_english_code_also_ships_hindi() -> None:
    """`patients.preferred_language` defaults to `hi` (CLAUDE.md §9), so a code
    with English-only copy silently reaches almost every patient in the wrong
    language."""
    english = set(tpl.DEFAULT_TEMPLATES["en"])
    hindi = set(tpl.DEFAULT_TEMPLATES["hi"])

    assert english == hindi


def test_shipped_copy_only_asks_for_placeholders_a_trigger_supplies() -> None:
    """Guards the seam between `handlers.py` and the copy.

    Every handler supplies `hospital_name`, `patient_name` and `uhid`; anything
    else has to come from that event's own payload. This asserts the vocabulary
    stays inside a known set, so a reworded template cannot quietly start asking
    for a value nothing provides.
    """
    known = {
        "hospital_name",
        "patient_name",
        "uhid",
        "doctor_name",
        "appointment_time",
        "appointment_number",
        "test_name",
        "report_number",
        "discipline",
        "critical_values",
        "reason",
        "accession_number",
        "amount",
        "method",
        "receipt_number",
        "balance_due",
        "invoice_number",
        "grand_total",
        "context_status",
    }

    for language, entries in tpl.DEFAULT_TEMPLATES.items():
        for code, template in entries.items():
            unknown = set(tpl.placeholders_in(template.body)) - known
            assert not unknown, f"{code}/{language} asks for {sorted(unknown)}"


def test_default_template_lookup_falls_back_to_english() -> None:
    found = tpl.default_template(tpl.TemplateCode.REPORT_READY, "ta")
    assert found is not None
    language, template = found

    assert language == "en"
    assert template.category is NotificationCategory.REPORT


def test_default_template_prefers_the_requested_language() -> None:
    found = tpl.default_template(tpl.TemplateCode.REPORT_READY, "hi")
    assert found is not None
    language, _ = found
    assert language == "hi"


def test_an_unknown_code_has_no_shipped_copy() -> None:
    assert tpl.default_template("SOMETHING_NOBODY_WROTE", "en") is None


def test_only_email_carries_a_subject() -> None:
    """WhatsApp and SMS have nowhere to put one, and a stored subject nobody can
    see is a subject somebody will eventually believe is being delivered."""
    found = tpl.default_template(tpl.TemplateCode.REPORT_READY, "en")
    assert found is not None
    _, template = found

    assert tpl.subject_for(NotificationChannel.EMAIL, template) == template.subject
    assert tpl.subject_for(NotificationChannel.SMS, template) is None
    assert tpl.subject_for(NotificationChannel.WHATSAPP, template) is None


# ---------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------
def test_rendering_never_raises_whatever_the_context_holds() -> None:
    """The contract that lets the dispatcher subscribe to a death without being
    able to fail it. Nothing in here may throw."""

    class Awkward:
        def __str__(self) -> str:
            return "fine"

    for context in (
        {},
        {"a": Awkward()},
        {"a": [1, 2, 3]},
        {"a": {"nested": "dict"}},
        {"a": 3.14159},
    ):
        result = tpl.render("{{a}} {{b}}", context)
        assert isinstance(result.body, str)


def test_a_code_with_no_copy_at_all_still_produces_something_sendable() -> None:
    """Staff can raise an ad-hoc message under a code this file has never heard
    of. Refusing to render it means the receptionist's message does not go out."""
    body = tpl.fallback_body("A_CODE_INVENTED_AT_THE_COUNTER")
    result = tpl.render(
        body, {"hospital_name": "Sunrise", "patient_name": "Asha"}, used_fallback=True
    )

    assert "Sunrise" in result.body
    assert "Asha" in result.body
    assert result.used_fallback
    assert not result.is_complete
