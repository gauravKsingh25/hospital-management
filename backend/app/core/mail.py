"""Platform email: the one place this application talks SMTP.

Used for messages from the platform owner to the people who run a hospital —
today, invitations. Patient messages go through `notifications`, whose
channel ladder and suppression rules are a different problem.

The contract matches the notification gateways: `send` **returns** failure, it
never raises. An SMTP server refusing a message is an expected outcome that the
caller records and shows ("Email failed — resend?"), not an exception that
should roll back the invite it was announcing.

`smtplib` is blocking, so the send runs in a worker thread. A handful of
invitations a day does not justify an async SMTP dependency.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from app.core.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "ConsoleMailer",
    "MailMessage",
    "MailResult",
    "Mailer",
    "SecretStr",
    "SmtpMailer",
    "get_mailer",
]

# An SMTP error string is stored and shown to platform staff; it is bounded so
# a server that answers with a novel cannot bloat the email log.
_MAX_ERROR_LENGTH = 500


@dataclass(frozen=True, kw_only=True, slots=True)
class MailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class MailResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None


class Mailer(Protocol):
    async def send(self, message: MailMessage) -> MailResult: ...


def _build(message: MailMessage) -> EmailMessage:
    email = EmailMessage()
    email["From"] = formataddr((settings.MAIL_FROM_NAME, settings.MAIL_FROM))
    email["To"] = message.to
    email["Subject"] = message.subject
    domain = settings.MAIL_FROM.rpartition("@")[2] or None
    email["Message-ID"] = make_msgid(domain=domain)
    email.set_content(message.text)
    if message.html is not None:
        email.add_alternative(message.html, subtype="html")
    return email


class SmtpMailer:
    """Sends through an authenticated SMTP relay (Gmail by default)."""

    def _send_blocking(self, email: EmailMessage) -> None:
        with smtplib.SMTP(
            settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS
        ) as client:
            if settings.SMTP_STARTTLS:
                client.starttls(context=ssl.create_default_context())
            client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD.get_secret_value())
            client.send_message(email)

    async def send(self, message: MailMessage) -> MailResult:
        email = _build(message)
        try:
            await asyncio.to_thread(self._send_blocking, email)
        except (smtplib.SMTPException, OSError) as error:
            # Never the password: SMTP replies do not echo it, but scrub anyway
            # in case a relay ever does.
            text = f"{type(error).__name__}: {error}"
            secret = settings.SMTP_PASSWORD.get_secret_value()
            if secret:
                text = text.replace(secret, "••••")
            logger.warning("Email to %s failed: %s", message.to, text)
            return MailResult(ok=False, error=text[:_MAX_ERROR_LENGTH])
        return MailResult(ok=True, message_id=email["Message-ID"])


class ConsoleMailer:
    """Development stand-in: logs the message, optionally writes an .eml file.

    The full text is logged, link token included — that is the point on a
    developer's machine, and `Settings` refuses this backend in production.
    """

    async def send(self, message: MailMessage) -> MailResult:
        email = _build(message)
        logger.info(
            "Email (console) to %s — %s\n%s", message.to, message.subject, message.text
        )
        if settings.MAIL_OUTBOX_DIR:
            outbox = Path(settings.MAIL_OUTBOX_DIR)
            outbox.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            safe_to = re.sub(r"[^A-Za-z0-9@._-]", "_", message.to)
            (outbox / f"{stamp}-{safe_to}.eml").write_bytes(email.as_bytes())
        return MailResult(ok=True, message_id=email["Message-ID"])


def get_mailer() -> Mailer:
    return SmtpMailer() if settings.MAIL_BACKEND == "smtp" else ConsoleMailer()
