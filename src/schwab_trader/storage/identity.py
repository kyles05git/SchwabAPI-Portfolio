"""Deterministic identities used across local and shared storage."""

from __future__ import annotations

import hashlib

LEGACY_NAMESPACE_NAME = "legacy-laptop-import"
LEGACY_NAMESPACE_ID = hashlib.sha256(
    f"namespace\x1f{LEGACY_NAMESPACE_NAME}".encode()
).hexdigest()


def stable_id(kind: str, *parts: str) -> str:
    """Return a stable, non-sensitive identifier from normalized source identity."""
    payload = "\x1f".join((kind, *(part.strip() for part in parts)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sleeve_scope(*, namespace_id: str, cohort_id: str | None) -> str:
    return f"cohort:{cohort_id}" if cohort_id else f"namespace:{namespace_id}"


def stable_sleeve_id(*, source_identity: str, scope_key: str, name: str) -> str:
    return stable_id("sleeve", source_identity, scope_key, name.casefold())


def standalone_paper_sleeve_id() -> str:
    scope = sleeve_scope(namespace_id=LEGACY_NAMESPACE_ID, cohort_id=None)
    return stable_sleeve_id(
        source_identity="data/paper.sqlite3",
        scope_key=scope,
        name="standalone-paper",
    )
