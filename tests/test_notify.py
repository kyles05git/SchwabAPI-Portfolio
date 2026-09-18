"""Tests for the notification channel (offline; no real SMTP, no network)."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import pytest

from schwab_trader.config import Settings
from schwab_trader.notify import (
    NotifyError,
    NotifyMessage,
    NullNotifier,
    SmtpNotifier,
    build_notifier,
)


class _FakeSMTP:
    """A stand-in for smtplib.SMTP that records calls instead of connecting."""

    def __init__(self, *, fail_on_send: bool = False) -> None:
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        self.quit_called = False
        self._fail_on_send = fail_on_send

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.quit_called = True
        return False

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        if self._fail_on_send:
            raise smtplib.SMTPException("boom")
        self.sent.append(message)


def _smtp_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "user@example.com",
        "smtp_password": "hunter2-secret-pw",
        "notify_from": "",
        "notify_to": "me@example.com, second@example.com",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_build_notifier_null_when_unconfigured() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not settings.has_smtp
    assert isinstance(build_notifier(settings), NullNotifier)


def test_null_notifier_send_is_noop() -> None:
    # Should not raise and should require no configuration.
    NullNotifier().send(NotifyMessage(subject="hi", body="there"))


def test_build_notifier_smtp_when_configured() -> None:
    settings = _smtp_settings()
    assert settings.has_smtp
    assert isinstance(build_notifier(settings), SmtpNotifier)


def test_notify_recipients_and_from_fallback() -> None:
    settings = _smtp_settings()
    assert settings.notify_to_list == ["me@example.com", "second@example.com"]
    # notify_from empty -> falls back to the SMTP username.
    assert settings.effective_notify_from == "user@example.com"
    explicit = _smtp_settings(notify_from="alerts@example.com")
    assert explicit.effective_notify_from == "alerts@example.com"


def test_build_email_sets_headers_and_body() -> None:
    settings = _smtp_settings()
    notifier = build_notifier(settings)
    assert isinstance(notifier, SmtpNotifier)
    email = notifier.build_email(NotifyMessage(subject="Proposed order", body="BUY 1 AAPL"))
    assert email["Subject"] == "[schwab-trader] Proposed order"
    assert email["From"] == "user@example.com"
    assert email["To"] == "me@example.com, second@example.com"
    assert email.get_content().strip() == "BUY 1 AAPL"


def test_build_email_adds_html_alternative_when_present() -> None:
    settings = _smtp_settings()
    notifier = build_notifier(settings)
    assert isinstance(notifier, SmtpNotifier)
    email = notifier.build_email(
        NotifyMessage(subject="s", body="plain fallback", html_body="<b>rich</b>")
    )
    assert email.get_content_type() == "multipart/alternative"
    html_part = email.get_body(preferencelist=("html",))
    text_part = email.get_body(preferencelist=("plain",))
    assert html_part is not None and "<b>rich</b>" in html_part.get_content()
    assert text_part is not None and "plain fallback" in text_part.get_content()


def test_build_email_stays_plain_without_html_body() -> None:
    notifier = build_notifier(_smtp_settings())
    assert isinstance(notifier, SmtpNotifier)
    email = notifier.build_email(NotifyMessage(subject="s", body="just text"))
    assert email.get_content_type() == "text/plain"


def test_send_uses_starttls_login_and_sends() -> None:
    fake = _FakeSMTP()
    captured: dict[str, object] = {}

    def factory(host: str, port: int) -> _FakeSMTP:
        captured["host"] = host
        captured["port"] = port
        return fake

    notifier = build_notifier(_smtp_settings(), smtp_factory=factory)
    notifier.send(NotifyMessage(subject="Ping", body="pong"))

    assert captured == {"host": "smtp.example.com", "port": 587}
    assert fake.started_tls is True
    assert fake.logged_in == ("user@example.com", "hunter2-secret-pw")
    assert len(fake.sent) == 1
    assert fake.sent[0]["Subject"] == "[schwab-trader] Ping"
    assert fake.quit_called is True


def test_send_skips_starttls_when_disabled() -> None:
    fake = _FakeSMTP()
    notifier = build_notifier(_smtp_settings(smtp_use_tls=False), smtp_factory=lambda _h, _p: fake)
    notifier.send(NotifyMessage(subject="s", body="b"))
    assert fake.started_tls is False
    assert len(fake.sent) == 1


def test_send_wraps_errors_without_leaking_password() -> None:
    fake = _FakeSMTP(fail_on_send=True)
    notifier = build_notifier(_smtp_settings(), smtp_factory=lambda _h, _p: fake)
    with pytest.raises(NotifyError) as excinfo:
        notifier.send(NotifyMessage(subject="s", body="b"))
    # The wrapped error must not carry the password anywhere in its text.
    assert "hunter2-secret-pw" not in str(excinfo.value)
    assert "hunter2-secret-pw" not in repr(excinfo.value)
