"""The mailer: platform email (invites) over SMTP, or to the console in dev.

The contract under test is the one notification gateways already follow — a
send *returns* failure rather than raising — plus two things specific to mail:
the SMTP password never leaks into a stored error, and a production deployment
cannot silently fall back to printing invite links into a log.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core import mail
from app.core.config import Settings, settings
from app.core.mail import ConsoleMailer, MailMessage, SmtpMailer

_DB = {
    "DATABASE_URL": "postgresql://u:p@postgres:5432/hospital",
    "DIRECT_URL": "postgresql://u:p@postgres:5432/hospital",
}


def test_smtp_backend_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="SMTP_PASSWORD"):
        Settings(**_DB, MAIL_BACKEND="smtp", SMTP_USERNAME="", SMTP_PASSWORD="")


def test_console_backend_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="MAIL_BACKEND"):
        Settings(
            **_DB,
            ENVIRONMENT="production",
            SECRET_KEY="x" * 40,
            DEBUG=False,
            MAIL_BACKEND="console",
        )


async def test_console_mailer_writes_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MAIL_OUTBOX_DIR", str(tmp_path))

    result = await ConsoleMailer().send(
        MailMessage(to="a@b.in", subject="S", text="hello http://x/invite/T")
    )

    assert result.ok is True
    files = list(tmp_path.glob("*.eml"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "hello http://x/invite/T" in content
    assert "Subject: S" in content


class _FailingSMTP:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")


async def test_smtp_failure_returns_result_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mail.smtplib, "SMTP", _FailingSMTP)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "owner@gmail.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", mail.SecretStr("app-pass-123"))

    result = await SmtpMailer().send(MailMessage(to="a@b.in", subject="S", text="t"))

    assert result.ok is False
    assert result.error is not None
    assert "535" in result.error
    assert "app-pass-123" not in result.error


class _RecordingSMTP:
    calls: list[tuple[str, tuple[Any, ...]]] = []
    sent: list[EmailMessage] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.calls.append(("connect", (host, port, timeout)))

    def __enter__(self) -> _RecordingSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, **kwargs: Any) -> None:
        self.calls.append(("starttls", ()))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", (user, password)))

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append(("send_message", ()))
        self.sent.append(message)


async def test_smtp_mailer_sends_starttls(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingSMTP.calls = []
    _RecordingSMTP.sent = []
    monkeypatch.setattr(mail.smtplib, "SMTP", _RecordingSMTP)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "owner@gmail.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", mail.SecretStr("app-pass-123"))
    monkeypatch.setattr(settings, "MAIL_FROM", "gauravsingh10.vns@gmail.com")
    monkeypatch.setattr(settings, "MAIL_FROM_NAME", "Hospital Management")

    result = await SmtpMailer().send(
        MailMessage(to="a@b.in", subject="S", text="t", html="<p>t</p>")
    )

    names = [name for name, _ in _RecordingSMTP.calls]
    assert names == ["connect", "starttls", "login", "send_message"]
    assert ("login", ("owner@gmail.com", "app-pass-123")) in _RecordingSMTP.calls
    sent = _RecordingSMTP.sent[0]
    assert sent["From"] == "Hospital Management <gauravsingh10.vns@gmail.com>"
    assert sent["To"] == "a@b.in"
    assert result.ok is True
    assert result.message_id == sent["Message-ID"]
