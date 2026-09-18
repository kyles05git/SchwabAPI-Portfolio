"""Notification channel: push short operational messages out-of-band.

A small, backend-agnostic notifier used by the notify-and-approve runner
(``NEXT_TASKS.md`` idea #1) and, later, the weekly digest (idea #4). One backend
to start: SMTP email over STARTTLS. The SMTP password comes from
:class:`~schwab_trader.config.Settings` as a ``SecretStr`` and is never logged;
message bodies are the caller's responsibility to keep sanitized (no account
hash, no secrets - only a single-use approval id where relevant).

Dependency-free (stdlib ``smtplib``/``email`` only). :func:`build_notifier`
returns a :class:`NullNotifier` when SMTP is unconfigured, so a caller can always
obtain a notifier and decide whether the channel is actually live via
``Settings.has_smtp`` - a runner that must deliver an approval refuses to act when
the channel is dark rather than silently dropping it.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from schwab_trader.config import Settings
from schwab_trader.logging_config import get_logger

log = get_logger("schwab_trader.notify")


class NotifyError(RuntimeError):
    """A notification could not be delivered."""


class NotifyMessage(BaseModel):
    """One notification: a subject, a plain-text body, and an optional HTML body.

    ``body`` is always the plain-text version (and the fallback for clients that
    can't render HTML). When ``html_body`` is set, the email is sent as
    multipart/alternative so rich clients show the formatted version.

    ``category`` is an advisory tag (``info`` | ``alert`` | ``approval``) that a
    backend may use for routing or filtering; it carries no security meaning.
    """

    subject: str
    body: str
    html_body: str | None = None
    category: str = "info"


@runtime_checkable
class Notifier(Protocol):
    """Anything that can deliver a :class:`NotifyMessage`."""

    def send(self, message: NotifyMessage) -> None: ...


class NullNotifier:
    """Drops messages (used when no channel is configured); logs at debug level."""

    def send(self, message: NotifyMessage) -> None:
        log.debug("Notification dropped (no channel configured): %s", message.subject)


@dataclass(frozen=True)
class SmtpConfig:
    """Resolved SMTP settings for :class:`SmtpNotifier` (password in the clear here,
    only in memory and only at send time; never logged)."""

    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    sender: str
    recipients: tuple[str, ...]


# A factory for an SMTP connection (host, port) -> client. Injectable for tests.
SmtpFactory = Callable[[str, int], smtplib.SMTP]


def _default_smtp_factory(host: str, port: int) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=30)


class SmtpNotifier:
    """Deliver notifications as plain-text email over SMTP (optional STARTTLS)."""

    def __init__(self, config: SmtpConfig, *, smtp_factory: SmtpFactory | None = None) -> None:
        self._config = config
        self._smtp_factory = smtp_factory or _default_smtp_factory

    def build_email(self, message: NotifyMessage) -> EmailMessage:
        """Construct the outgoing email (pure; no I/O) - the testable core."""
        email = EmailMessage()
        email["Subject"] = f"[schwab-trader] {message.subject}"
        email["From"] = self._config.sender
        email["To"] = ", ".join(self._config.recipients)
        email.set_content(message.body)  # plain-text part (and fallback)
        if message.html_body:
            # multipart/alternative: rich clients render this, others use the text.
            email.add_alternative(message.html_body, subtype="html")
        return email

    def send(self, message: NotifyMessage) -> None:
        email = self.build_email(message)
        try:
            with self._smtp_factory(self._config.host, self._config.port) as smtp:
                if self._config.use_tls:
                    smtp.starttls()
                if self._config.username:
                    smtp.login(self._config.username, self._config.password)
                smtp.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            # Deliberately do not include the exception's text/args: keep any
            # credential material out of the message. The type name is enough to
            # triage (auth vs. connect vs. TLS).
            raise NotifyError(f"SMTP delivery failed: {type(exc).__name__}") from exc
        log.info("Notification sent: %s", message.subject)


def build_notifier(settings: Settings, *, smtp_factory: SmtpFactory | None = None) -> Notifier:
    """Return an :class:`SmtpNotifier` when SMTP is configured, else a NullNotifier.

    Never raises on missing configuration; callers that require a live channel must
    check ``settings.has_smtp`` themselves and fail closed when it is False.
    """
    if not settings.has_smtp:
        return NullNotifier()
    config = SmtpConfig(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password.get_secret_value(),
        use_tls=settings.smtp_use_tls,
        sender=settings.effective_notify_from,
        recipients=tuple(settings.notify_to_list),
    )
    return SmtpNotifier(config, smtp_factory=smtp_factory)
