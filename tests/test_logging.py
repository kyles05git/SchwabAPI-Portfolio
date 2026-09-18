"""Tests for secret redaction in logging."""

from __future__ import annotations

import logging

from schwab_trader.config import Settings
from schwab_trader.logging_config import (
    REDACTED,
    RedactionFilter,
    reset_logging,
    setup_logging,
)


def test_redacts_registered_literal() -> None:
    flt = RedactionFilter(["supersecretvalue123"])
    assert flt._redact("token is supersecretvalue123 here") == f"token is {REDACTED} here"


def test_redacts_bearer_authorization_header() -> None:
    flt = RedactionFilter()
    out = flt._redact("Authorization: Bearer abc.def.ghijkl123")
    assert "abc.def.ghijkl123" not in out
    assert REDACTED in out


def test_redacts_token_json_fields() -> None:
    flt = RedactionFilter()
    out = flt._redact('{"access_token": "AAA111BBB222", "refresh_token": "RRR333"}')
    assert "AAA111BBB222" not in out
    assert "RRR333" not in out


def test_redacts_authorization_code_in_query() -> None:
    flt = RedactionFilter()
    out = flt._redact("callback https://127.0.0.1:8182/callback?code=SECRETCODE&session=xyz")
    assert "SECRETCODE" not in out
    assert "xyz" not in out


def test_short_literals_are_not_redacted() -> None:
    # Avoid masking short/common substrings; only >= 4 chars are registered.
    flt = RedactionFilter(["ab"])
    assert flt._redact("abstract") == "abstract"


def test_setup_logging_writes_redacted_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        client_secret="mysupersecret",
        database_url="postgresql://user:database-secret@localhost/test",
        log_path=tmp_path / "logs" / "app.log",
    )
    try:
        setup_logging(settings)
        logging.getLogger("schwab_trader.test").info(
            "leaked client_secret=mysupersecret, "
            "database=postgresql://user:database-secret@localhost/test, "
            "and token access_token=ABC123XYZ"
        )
        for handler in logging.getLogger().handlers:
            handler.flush()
        contents = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    finally:
        reset_logging()

    assert "mysupersecret" not in contents
    assert "ABC123XYZ" not in contents
    assert "database-secret" not in contents
    assert REDACTED in contents
