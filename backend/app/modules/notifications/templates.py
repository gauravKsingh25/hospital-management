"""Template codes, the built-in default copy, and the renderer. No I/O.

Kept separate from `service.py` for the same reason `billing/tax.py` is: the
interesting failure modes here are textual, and text is far easier to reason
about and test when nothing around it touches a database.

---------------------------------------------------------------------------
Why `{{double_braces}}` and a hand-rolled substitution
---------------------------------------------------------------------------

The obvious implementation is `template.format(**context)`. It is also a
vulnerability. Template bodies are **hospital-authored data**, editable through
the admin API, and Python's format mini-language reaches into objects:

    "{patient.__class__.__init__.__globals__[SECRET_KEY]}".format(patient=p)

That is a real, published class of bug (format-string injection), and the fix is
not to sanitise the input but to never hand attacker-influenced text to a
resolver that can traverse attributes. So: one regex, one flat `dict[str, str]`,
no attribute access, no indexing, no evaluation. A placeholder either names a key
we supplied or it does not.

`{{double}}` rather than `{single}` because it matches what the WhatsApp Business
template editor shows staff, and because clinical copy occasionally contains a
literal brace that should stay literal.

---------------------------------------------------------------------------
Why the default copy lives in Python rather than in a seed migration
---------------------------------------------------------------------------

The tempting alternative is to INSERT a row per (code, channel, language, tenant)
in the migration. It has one flaw that only shows up later: a hospital onboarded
*after* the migration gets nothing, and starts life sending bare fallback text
that nobody notices because the messages still technically go out.

So `DEFAULT_TEMPLATES` is the shipped copy, a hospital-specific
`notification_templates` row is an **override**, and a tenant created tomorrow
inherits working English and Hindi wording with no seeding step at all. The
`needs_template` flag then means something sharp — neither an override nor a
default exists — rather than "somebody forgot to run a backfill".

Hindi is shipped alongside English because `patients.preferred_language` defaults
to `hi` (CLAUDE.md §9). English-only copy would mean nearly every patient
silently receiving the fallback language.

---------------------------------------------------------------------------
Why rendering never raises
---------------------------------------------------------------------------

Same contract as `billing.service.capture_charge`. A missing placeholder, an
untranslated template, a code this file has never heard of — none of these may
stop the hospital working. A message degrades and says so
(`RenderResult.missing`, `Notification.needs_template`); it does not explode in
the middle of recording a death.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from app.modules.notifications.models import NotificationCategory, NotificationChannel

__all__ = [
    "DEFAULT_LANGUAGE",
    "DEFAULT_TEMPLATES",
    "PLACEHOLDER_PATTERN",
    "DefaultTemplate",
    "RenderResult",
    "TemplateCode",
    "coerce_context",
    "default_template",
    "fallback_body",
    "language_preference",
    "placeholders_in",
    "render",
]

# The language every hospital is guaranteed to have copy for.
DEFAULT_LANGUAGE: Final[str] = "en"

# Deliberately narrow: lowercase, digits, underscore. No dots, no brackets, no
# whitespace-delimited expressions. If it is not a plain key it is not a
# placeholder, and it is left on the page untouched rather than interpreted.
PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,63})\s*\}\}")


class TemplateCode:
    """Every message the system can send. Import the constant, never the string."""

    # --- scheduling -------------------------------------------------------
    APPOINTMENT_BOOKED = "APPOINTMENT_BOOKED"
    APPOINTMENT_CANCELLED = "APPOINTMENT_CANCELLED"

    # --- diagnostics ------------------------------------------------------
    REPORT_READY = "REPORT_READY"
    # Staff-directed. A panic value does not wait for anyone to refresh a screen.
    CRITICAL_RESULT = "CRITICAL_RESULT"
    # The patient has to be stuck again, and is owed an explanation.
    SPECIMEN_REJECTED = "SPECIMEN_REJECTED"

    # --- billing ----------------------------------------------------------
    PAYMENT_RECEIPT = "PAYMENT_RECEIPT"
    INVOICE_ISSUED = "INVOICE_ISSUED"
    SETTLEMENT_REQUIRED = "SETTLEMENT_REQUIRED"

    # --- clinical ---------------------------------------------------------
    FOLLOW_UP_REMINDER = "FOLLOW_UP_REMINDER"


@dataclass(frozen=True, kw_only=True, slots=True)
class DefaultTemplate:
    """Shipped copy for one code in one language.

    One body across all three channels on purpose. An SMS costs money per 160
    characters and nobody has ever complained that a hospital message was too
    short, so the SMS wording is the right wording for WhatsApp too. Only email
    adds anything — a subject line, which the other two channels do not have.
    """

    category: NotificationCategory
    subject: str
    body: str


def _en() -> dict[str, DefaultTemplate]:
    return {
        TemplateCode.APPOINTMENT_BOOKED: DefaultTemplate(
            category=NotificationCategory.APPOINTMENT,
            subject="Appointment confirmed",
            body=(
                "{{hospital_name}}: Dear {{patient_name}}, your appointment with "
                "{{doctor_name}} is confirmed for {{appointment_time}}. "
                "Booking {{appointment_number}}. Please arrive 15 minutes early."
            ),
        ),
        TemplateCode.APPOINTMENT_CANCELLED: DefaultTemplate(
            category=NotificationCategory.APPOINTMENT,
            subject="Appointment cancelled",
            body=(
                "{{hospital_name}}: Dear {{patient_name}}, your appointment on "
                "{{appointment_time}} has been cancelled. Please call us to rebook."
            ),
        ),
        TemplateCode.REPORT_READY: DefaultTemplate(
            category=NotificationCategory.REPORT,
            subject="Your report is ready",
            body=(
                "{{hospital_name}}: Dear {{patient_name}}, your {{test_name}} report "
                "({{report_number}}) is ready for collection."
            ),
        ),
        TemplateCode.CRITICAL_RESULT: DefaultTemplate(
            category=NotificationCategory.CRITICAL_ALERT,
            subject="CRITICAL result requires attention",
            body=(
                "CRITICAL RESULT: {{patient_name}} ({{uhid}}) - {{test_name}}: "
                "{{critical_values}}. Please review immediately and record the callback."
            ),
        ),
        TemplateCode.SPECIMEN_REJECTED: DefaultTemplate(
            category=NotificationCategory.REPORT,
            subject="Repeat sample required",
            body=(
                "{{hospital_name}}: Dear {{patient_name}}, your sample could not be tested "
                "({{reason}}). Please visit the laboratory for a repeat collection."
            ),
        ),
        TemplateCode.PAYMENT_RECEIPT: DefaultTemplate(
            category=NotificationCategory.BILLING,
            subject="Payment received",
            body=(
                "{{hospital_name}}: Received {{amount}} from {{patient_name}} by "
                "{{method}}. Receipt {{receipt_number}}. Balance {{balance_due}}."
            ),
        ),
        TemplateCode.INVOICE_ISSUED: DefaultTemplate(
            category=NotificationCategory.BILLING,
            subject="Your invoice",
            body=(
                "{{hospital_name}}: Invoice {{invoice_number}} for {{patient_name}} - "
                "{{grand_total}}. Amount due {{balance_due}}."
            ),
        ),
        TemplateCode.SETTLEMENT_REQUIRED: DefaultTemplate(
            category=NotificationCategory.BILLING,
            subject="Outstanding balance",
            body=(
                "{{hospital_name}}: Dear {{patient_name}}, an amount of {{balance_due}} is "
                "outstanding on your visit. Please settle it at the billing counter."
            ),
        ),
        TemplateCode.FOLLOW_UP_REMINDER: DefaultTemplate(
            category=NotificationCategory.FOLLOW_UP,
            subject="Your visit is complete",
            body=(
                "{{hospital_name}}: Dear {{patient_name}}, your visit is complete. "
                "Please book a follow-up if your doctor has advised one."
            ),
        ),
    }


def _hi() -> dict[str, DefaultTemplate]:
    return {
        TemplateCode.APPOINTMENT_BOOKED: DefaultTemplate(
            category=NotificationCategory.APPOINTMENT,
            subject="अपॉइंटमेंट की पुष्टि",
            body=(
                "{{hospital_name}}: प्रिय {{patient_name}}, {{doctor_name}} के साथ आपकी "
                "अपॉइंटमेंट {{appointment_time}} के लिए तय हो गई है। "
                "बुकिंग {{appointment_number}}। कृपया 15 मिनट पहले पहुँचें।"
            ),
        ),
        TemplateCode.APPOINTMENT_CANCELLED: DefaultTemplate(
            category=NotificationCategory.APPOINTMENT,
            subject="अपॉइंटमेंट रद्द",
            body=(
                "{{hospital_name}}: प्रिय {{patient_name}}, {{appointment_time}} की आपकी "
                "अपॉइंटमेंट रद्द कर दी गई है। दोबारा बुक करने के लिए कृपया हमें कॉल करें।"
            ),
        ),
        TemplateCode.REPORT_READY: DefaultTemplate(
            category=NotificationCategory.REPORT,
            subject="आपकी रिपोर्ट तैयार है",
            body=(
                "{{hospital_name}}: प्रिय {{patient_name}}, आपकी {{test_name}} रिपोर्ट "
                "({{report_number}}) तैयार है।"
            ),
        ),
        # Deliberately not translated. A critical-value alert is read under time
        # pressure by whoever is on shift, and a half-translated clinical alert
        # is worse than a plain English one.
        TemplateCode.CRITICAL_RESULT: DefaultTemplate(
            category=NotificationCategory.CRITICAL_ALERT,
            subject="CRITICAL result requires attention",
            body=(
                "CRITICAL RESULT: {{patient_name}} ({{uhid}}) - {{test_name}}: "
                "{{critical_values}}. Please review immediately and record the callback."
            ),
        ),
        TemplateCode.SPECIMEN_REJECTED: DefaultTemplate(
            category=NotificationCategory.REPORT,
            subject="दोबारा सैंपल आवश्यक",
            body=(
                "{{hospital_name}}: प्रिय {{patient_name}}, आपके सैंपल की जाँच नहीं हो सकी "
                "({{reason}})। कृपया दोबारा सैंपल देने के लिए लैब आएँ।"
            ),
        ),
        TemplateCode.PAYMENT_RECEIPT: DefaultTemplate(
            category=NotificationCategory.BILLING,
            subject="भुगतान प्राप्त हुआ",
            body=(
                "{{hospital_name}}: {{patient_name}} से {{method}} द्वारा {{amount}} "
                "प्राप्त हुए। रसीद {{receipt_number}}। शेष राशि {{balance_due}}।"
            ),
        ),
        TemplateCode.INVOICE_ISSUED: DefaultTemplate(
            category=NotificationCategory.BILLING,
            subject="आपका बिल",
            body=(
                "{{hospital_name}}: {{patient_name}} के लिए बिल {{invoice_number}} - "
                "{{grand_total}}। देय राशि {{balance_due}}।"
            ),
        ),
        TemplateCode.SETTLEMENT_REQUIRED: DefaultTemplate(
            category=NotificationCategory.BILLING,
            subject="बकाया राशि",
            body=(
                "{{hospital_name}}: प्रिय {{patient_name}}, आपकी विज़िट पर {{balance_due}} "
                "बकाया है। कृपया बिलिंग काउंटर पर भुगतान करें।"
            ),
        ),
        TemplateCode.FOLLOW_UP_REMINDER: DefaultTemplate(
            category=NotificationCategory.FOLLOW_UP,
            subject="आपकी विज़िट पूरी हुई",
            body=(
                "{{hospital_name}}: प्रिय {{patient_name}}, आपकी विज़िट पूरी हो गई है। "
                "डॉक्टर की सलाह पर कृपया फ़ॉलो-अप बुक करें।"
            ),
        ),
    }


# language -> code -> copy.
DEFAULT_TEMPLATES: Final[dict[str, dict[str, DefaultTemplate]]] = {
    DEFAULT_LANGUAGE: _en(),
    "hi": _hi(),
}


def default_template(code: str, language: str | None) -> tuple[str, DefaultTemplate] | None:
    """Shipped copy for a code, and the language it was actually found in.

    Same fallback chain as a hospital's own templates: asked-for, then English.
    Returns `None` for a code with no shipped copy — an ad-hoc message raised
    under a made-up code, which is legitimate and handled by `fallback_body`.
    """
    for candidate in language_preference(language):
        found = DEFAULT_TEMPLATES.get(candidate, {}).get(code.strip().upper())
        if found is not None:
            return candidate, found
    return None


def subject_for(channel: NotificationChannel, template: DefaultTemplate) -> str | None:
    """Only email carries a subject; the other two channels have nowhere to put one."""
    return template.subject if channel is NotificationChannel.EMAIL else None


@dataclass(frozen=True, slots=True)
class RenderResult:
    """The rendered text plus an honest account of what was wrong with it."""

    subject: str | None
    body: str
    # Placeholders the template asked for that the caller did not supply. Each
    # was replaced with an empty string — a sentence with a hole in it beats a
    # message that never went out, but the gap is reported rather than hidden.
    missing: tuple[str, ...] = field(default_factory=tuple)
    # True when neither a hospital template nor shipped copy existed.
    used_fallback: bool = False

    @property
    def is_complete(self) -> bool:
        return not self.missing and not self.used_fallback


def placeholders_in(text: str) -> tuple[str, ...]:
    """Every placeholder a template asks for, in order, deduplicated."""
    seen: dict[str, None] = {}
    for match in PLACEHOLDER_PATTERN.finditer(text):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def coerce_context(context: dict[str, Any]) -> dict[str, str]:
    """Flatten a caller's context to plain strings.

    Everything becomes a string *here* rather than at substitution time, so the
    substitution itself cannot invoke anything — no `__format__`, no `__str__` on
    a lazily-loaded ORM object mid-render, no surprise database query inside a
    template. `None` becomes an empty string and is **not** reported as missing:
    "no doctor assigned" is a legitimate value, not a configuration gap.
    """
    flattened: dict[str, str] = {}
    for key, value in context.items():
        if value is None:
            flattened[key] = ""
        elif isinstance(value, bool):
            # `str(True)` is "True", which is not a sentence anyone wants to read.
            flattened[key] = "yes" if value else "no"
        else:
            flattened[key] = str(value)
    return flattened


def _substitute(text: str, values: dict[str, str], missing: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        missing.add(name)
        return ""

    return PLACEHOLDER_PATTERN.sub(replace, text)


def render(
    body: str,
    context: dict[str, Any],
    *,
    subject: str | None = None,
    used_fallback: bool = False,
) -> RenderResult:
    """Fill a template. Never raises, never evaluates anything.

    Whitespace is normalised only at the ends. A placeholder that resolved to an
    empty string mid-line leaves a double space, and collapsing that would also
    collapse deliberate formatting in a multi-line email.
    """
    values = coerce_context(context)
    missing: set[str] = set()

    rendered_body = _substitute(body, values, missing).strip()
    rendered_subject = _substitute(subject, values, missing).strip() if subject else None

    return RenderResult(
        subject=rendered_subject or None,
        body=rendered_body,
        missing=tuple(sorted(missing)),
        used_fallback=used_fallback,
    )


def fallback_body(code: str) -> str:
    """Last resort: a code with neither an override nor shipped copy.

    Not defensive padding — `service.send_adhoc` lets staff raise a message under
    a code this file has never heard of, and refusing to render it would mean the
    receptionist's message silently does not go out.
    """
    return "{{hospital_name}}: an update regarding {{patient_name}}. Please contact the hospital."


def language_preference(requested: str | None) -> tuple[str, ...]:
    """The order to look in: asked-for, then English.

    Two rungs, not a full locale negotiation. `hi-IN` normalises to `hi` because
    a hospital writes one Hindi template, not one per region, and a lookup that
    misses on a suffix nobody set is a message that silently downgrades to
    English.
    """
    if not requested:
        return (DEFAULT_LANGUAGE,)
    primary = requested.strip().lower().replace("_", "-").split("-")[0][:8]
    if not primary or primary == DEFAULT_LANGUAGE:
        return (DEFAULT_LANGUAGE,)
    return (primary, DEFAULT_LANGUAGE)
