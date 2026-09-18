"""Structured logging with mandatory secret redaction.

Every log record passes through :class:`RedactionFilter`, which masks:

- Authorization headers and bearer tokens
- Client credentials (id/secret)
- Access, refresh, and id tokens
- Authorization codes
- Callback query strings (``?code=``/``&token=`` …)
- Any exact secret literals registered at setup time (e.g. the account hash)

Redaction is best-effort defense in depth; code should still avoid passing
secrets to the logger in the first place.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from schwab_trader.config import Settings

REDACTED = "[REDACTED]"

# Pattern-based redaction rules applied to the fully-formatted log message.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <token>  /  authorization=<token>
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._~+/\-]+=*"),
        rf"\1\2{REDACTED}",
    ),
    # "access_token": "...", refresh_token=..., client_secret: ..., etc.
    (
        re.compile(
            r"(?i)(\"?(?:access_token|refresh_token|id_token|client_secret|client_id)\"?"
            r"\s*[:=]\s*\"?)[^\"\s,&}]+"
        ),
        rf"\1{REDACTED}",
    ),
    # URL/query params: ?code=... &token=... &session=...
    (
        re.compile(r"(?i)([?&](?:code|token|session|access_token|refresh_token)=)[^&\s\"]+"),
        rf"\1{REDACTED}",
    ),
)


class RedactionFilter(logging.Filter):
    """A logging filter that scrubs secrets from formatted log messages."""

    def __init__(self, literals: Iterable[str] = ()) -> None:
        super().__init__()
        # Only redact reasonably long literals to avoid masking noise; sort by
        # length (desc) so longer secrets are replaced before shorter substrings.
        self._literals: list[str] = sorted(
            {value for value in literals if value and len(value) >= 4},
            key=len,
            reverse=True,
        )

    def _redact(self, text: str) -> str:
        for literal in self._literals:
            if literal in text:
                text = text.replace(literal, REDACTED)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive; never drop a record
            return True
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def reset_logging() -> None:
    """Remove and close all handlers on the root logger.

    Useful in tests and when reconfiguring logging so file handles are released.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def setup_logging(settings: Settings, *, level: int = logging.INFO) -> None:
    """Configure root logging with file + console handlers and redaction.

    Registers the known secret literals from ``settings`` so they are masked even
    if they somehow reach a log record.
    """
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)

    literals = [
        settings.client_id,
        settings.client_secret.get_secret_value(),
        settings.database_url.get_secret_value(),
        settings.account_hash.get_secret_value(),
        settings.smtp_password.get_secret_value(),
    ]
    redaction = RedactionFilter(literals)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    reset_logging()
    root = logging.getLogger()
    root.setLevel(level)

    file_handler = RotatingFileHandler(
        settings.log_path,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler()

    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redaction)
        root.addHandler(handler)

    # httpx/httpcore log full request URLs at INFO; account-specific paths embed
    # the account hash, so keep these quiet to avoid writing secrets to the log.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger by name."""
    return logging.getLogger(name)
