"""Durable, idempotent orchestration for official paper-sleeve cohort runs.

The orchestration boundary in this module is paper-only.  It coordinates the
existing :class:`~schwab_trader.sleeves.SleeveStore`, paper engines, strategy
registry, scheduling primitives, and :class:`~schwab_trader.evaluation.EvaluationStore`
without importing or calling any broker order operation.

Three durability rules are deliberately conservative:

* no member executes until a **cohort-wide data preflight** passes, so a session is
  all-or-nothing rather than a permanent mixture of members that ran against different
  data (see :mod:`schwab_trader.cohort_preflight`);
* a member is checkpointed as ``running`` before its paper engine can write a fill;
* after a restart, a ``running`` member is considered complete only when its
  idempotent official observation exists.  Otherwise it is marked interrupted and
  is not replayed, because replaying an ambiguous paper cycle could duplicate fills.

Pending members may resume only from a snapshot with the exact identity already
stored on the run.  A caller that cannot reconstruct that snapshot fails closed.

:attr:`SleeveRunStatus.AWAITING_DATA` is the one **non-terminal** outcome the runner
records.  It means nothing was executed and the session is still recordable, so the
next scheduler invocation retries it cleanly.  Two conditions reach it, told apart by
the error code carried on the run rather than by a second status:

* ``awaiting_data`` — a snapshot was captured and the cohort preflight refused it;
* ``awaiting_reauthentication`` — snapshot capture could not run at all because Schwab
  reauthentication is required, and no member had started and no snapshot was bound.

See :data:`WAIT_ERROR_CODES` and :func:`current_wait_error`.  Either wait is bounded by
the existing scheduler deadline rather than a second timer: once
:mod:`schwab_trader.scheduling` calls the session missed, the wait is converted into a
durable missed result with a truthful ``MISSING`` observation per member.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from schwab_trader import (
    cohort_preflight,
    execution_timing,
    market_calendar,
    scheduling,
    strategy_registry,
)
from schwab_trader.agent import AgentRunner, CycleReport, QuoteSource, Strategy
from schwab_trader.auth import ReauthRequiredError
from schwab_trader.data_contracts import Provenance
from schwab_trader.data_readiness import DataReadiness
from schwab_trader.evaluation import (
    EvaluationStore,
    ObservationStatus,
    OfficialDailyObservation,
    summarize_readiness,
)
from schwab_trader.execution_timing import ExecutionMethodology, SessionPlan
from schwab_trader.market_data import Quote, QuoteError
from schwab_trader.models import OrderSide
from schwab_trader.next_open_fill import OpeningBarEvidence
from schwab_trader.paper import PaperEngine
from schwab_trader.safety import KillSwitch
from schwab_trader.sleeves import SleeveConfig, SleeveStore


class SleeveRunStatus(StrEnum):
    """Durable lifecycle state for a cohort/session run."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_DATA = "awaiting-data"
    """Required data is not ready. Nothing executed; the session is still retryable.

    Distinct from every other state on purpose. It is not ``completed`` (no evidence
    exists), not ``partial`` (nothing ran at all), not ``failed`` or ``missed`` (the
    session can still be recorded correctly), and not ``pending``/``running`` (the
    runner did reach a considered verdict). Treating it as any of those is what turns
    a recoverable wait into a permanent hole in the record.
    """

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    MISSED = "missed"
    SKIPPED_CLOSED_SESSION = "skipped-closed-session"


#: Statuses that mean the session is finished and must never be re-executed. Notably
#: excludes ``AWAITING_DATA``, which exists precisely so a retry is allowed.
TERMINAL_RUN_STATUSES = frozenset(
    {
        SleeveRunStatus.COMPLETED,
        SleeveRunStatus.PARTIAL,
        SleeveRunStatus.FAILED,
        SleeveRunStatus.MISSED,
        SleeveRunStatus.SKIPPED_CLOSED_SESSION,
    }
)


class MemberRunStatus(StrEnum):
    """Durable lifecycle state for one expected cohort member."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    DATA_NOT_READY = "data-not-ready"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


class SleeveRunError(BaseModel):
    """Sanitized, machine-readable run or member failure."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    member_id: str | None = None
    capability: str | None = None
    retryable: bool = False
    reasons: tuple[str, ...] = ()
    """Structured ``"<kind>:<reason>"`` codes behind this error, when it has them.

    Kept separate from ``capability``, which names only the data *kind*: a later stage
    that persists an observation needs the same parseable codes every other observation
    carries, and deriving them from the kind alone loses the reason. Optional and
    defaulted, so error rows written before this field existed still load.
    """

    context: dict[str, object] = Field(default_factory=dict)
    """Sanitized operator context. Never provider payloads, requests, or secrets."""


class SleeveRunMember(BaseModel):
    """Persisted member checkpoint used for restart decisions."""

    model_config = ConfigDict(frozen=True)

    sleeve_id: str
    status: MemberRunStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: SleeveRunError | None = None


class SleeveRun(BaseModel):
    """Persisted identity and outcome for one cohort exchange session."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    run_key: str
    cohort_id: str
    session_id: str
    scheduled_for: date
    expected_members: tuple[str, ...]
    completed_members: tuple[str, ...]
    snapshot_id: str | None = None
    quote_snapshot_id: str | None = None
    data_snapshot_ids: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None
    status: SleeveRunStatus
    errors: tuple[SleeveRunError, ...] = ()
    members: tuple[SleeveRunMember, ...] = ()


def active_errors(run: SleeveRun) -> tuple[SleeveRunError, ...]:
    """Errors describing the run's *current* outcome.

    ``run.errors`` is append-only because the audit trail requires it, so it mixes
    every attempt's verdict together and its oldest row is not the run's state. A
    retryable error that a later attempt superseded is history, not a live failure: a
    COMPLETED run that once waited on provider data is not broken.

    Only ``COMPLETED`` is filtered, and only for retryable rows. That deliberately
    keeps ``data_deadline_exceeded`` on a ``MISSED`` run and ``snapshot_unavailable``
    on a ``FAILED`` run visible — nothing superseded those. Every presentation
    boundary must read through here; ``run.errors`` stays the durable record.
    """
    if run.status is not SleeveRunStatus.COMPLETED:
        return run.errors
    return tuple(error for error in run.errors if not error.retryable)


#: Error code for a wait on provider evidence the cohort preflight refused.
AWAITING_DATA_CODE = "awaiting_data"

#: Error code for a wait on the operator: Schwab reauthentication is required before a
#: snapshot can even be captured. Kept distinct from :data:`AWAITING_DATA_CODE` because
#: the two need opposite instructions — one resolves itself when the provider publishes,
#: the other never resolves until a human authenticates.
AWAITING_REAUTH_CODE = "awaiting_reauthentication"

#: Every code an ``AWAITING_DATA`` run can carry. Order matters only for reading: the
#: *last* row with one of these codes is the wait the run is currently in.
WAIT_ERROR_CODES = (AWAITING_DATA_CODE, AWAITING_REAUTH_CODE)


def current_wait_error(run: SleeveRun) -> SleeveRunError | None:
    """The error describing the wait this run is in right now, if it is waiting.

    ``run.errors`` is append-only and ordered by when each distinct verdict was last
    reached (see :meth:`SleeveRunStore.set_status`), so the last row carrying a wait
    code is the current wait even when a session alternated between waiting on data and
    waiting on authentication. Returns ``None`` for any run that is not waiting, so a
    terminal run can never be presented as recoverable.
    """
    if run.status is not SleeveRunStatus.AWAITING_DATA:
        return None
    for error in reversed(run.errors):
        if error.code in WAIT_ERROR_CODES:
            return error
    return None


def awaits_reauthentication(run: SleeveRun) -> bool:
    """Whether this run is parked waiting for the operator to authenticate."""
    error = current_wait_error(run)
    return error is not None and error.code == AWAITING_REAUTH_CODE


#: Exception types that make a **pre-execution** snapshot failure recoverable rather
#: than terminal. Deliberately a short, explicit allowlist rather than a rule over
#: ``Exception``: every unlisted failure keeps the existing fail-closed treatment,
#: because an unknown provider fault may have left ambiguous state behind while a
#: rejected refresh token provably has not.
RETRYABLE_PRE_EXECUTION_ERRORS: tuple[type[BaseException], ...] = (ReauthRequiredError,)


def is_retryable_pre_execution_failure(exc: BaseException) -> bool:
    """Whether ``exc`` is an explicitly classified, recoverable authentication failure.

    Follows ``__cause__`` only — the chain a caller built deliberately with
    ``raise ... from`` — and never ``__context__``, which merely records whatever
    exception happened to be in flight and would let an unrelated, already-handled
    error reclassify a genuine fault as recoverable. The walk is depth-bounded so a
    self-referential chain cannot hang the runner.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, RETRYABLE_PRE_EXECUTION_ERRORS):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


class SleeveRunConflictError(RuntimeError):
    """The durable identity exists with incompatible immutable inputs."""


class SnapshotMismatchError(RuntimeError):
    """A restart could not reproduce the run's already-persisted snapshot."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sleeve_runs (
    run_id TEXT PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    cohort_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    expected_members TEXT NOT NULL,
    completed_members TEXT NOT NULL,
    snapshot_id TEXT,
    quote_snapshot_id TEXT,
    data_snapshot_ids TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    errors TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sleeve_run_members (
    run_id TEXT NOT NULL,
    sleeve_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    PRIMARY KEY (run_id, sleeve_id),
    FOREIGN KEY (run_id) REFERENCES sleeve_runs(run_id)
);
CREATE TABLE IF NOT EXISTS official_session_leases (
    cohort_id TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (cohort_id, scheduled_for)
);
"""


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("run timestamps must include a timezone")
    return stamp.astimezone(UTC)


#: Context keys whose payload scales with the cohort universe rather than being a fixed
#: few fields. A 76-symbol universe makes ``market_data`` roughly 29 KB per row, and
#: ``set_status`` read-modify-writes the whole ``errors`` blob on every retry, so keeping
#: one copy per attempt measured 353 KB in a single TEXT column across 12 retries — all
#: of which ``cohort_ops._run_payload`` and the dashboard payload then serialize.
_BULKY_CONTEXT_KEYS = ("market_data",)


def _supersede_bulky_context(errors: list[object], code: str) -> None:
    """Drop per-symbol coverage from same-code rows a newer attempt has replaced.

    Only the current wait's coverage is operationally meaningful — every consumer reads
    the latest row — so older copies are cost without a reader. The rows themselves,
    with their code, message, capability, and reason codes, are left in place: the
    durable audit trail is *which* verdicts this run reached and in what order, not a
    per-attempt snapshot of every symbol's interval counts.
    """
    for existing in errors:
        if not isinstance(existing, dict) or existing.get("code") != code:
            continue
        context = existing.get("context")
        if not isinstance(context, dict):
            continue
        for key in _BULKY_CONTEXT_KEYS:
            context.pop(key, None)


def record_run_error(errors: list[Any], error: SleeveRunError) -> None:
    """Merge one verdict into a run's append-only error trail, in place.

    Shared by both run stores so the local SQLite and shared SQLAlchemy backends cannot
    drift on the durable contract every reader depends on:

    * one row per *distinct* verdict, so repeated scheduler polls that reach the same
      conclusion do not accumulate rows (and do not re-notify);
    * ordered by when each distinct verdict was **last** reached, so the last matching
      row is the run's current state. A session that waited on data, then on
      authentication, then on data again must not read back as waiting on
      authentication — :func:`current_wait_error`, :meth:`_expire_awaiting_data`, and
      the dashboard's provider panel all take the last matching row.
    """
    serialized = error.model_dump(mode="json")
    if serialized in errors:
        if errors[-1] != serialized:
            errors.remove(serialized)
            errors.append(serialized)
        return
    # Dedupe *before* superseding, so an unchanged verdict still collapses into the
    # existing row it matches in full.
    _supersede_bulky_context(errors, error.code)
    errors.append(serialized)


def _members(values: Sequence[str]) -> tuple[str, ...]:
    members = tuple(value.strip() for value in values if value.strip())
    if not members:
        raise ValueError("a cohort run requires at least one expected member")
    if len({member.casefold() for member in members}) != len(members):
        raise ValueError("expected_members contains a duplicate sleeve")
    return members


class SleeveRunStore:
    """SQLite persistence for run identity, member checkpoints, and errors."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def ensure_run(
        self,
        *,
        cohort_id: str,
        session: scheduling.ExchangeSession,
        expected_members: Sequence[str],
        status: SleeveRunStatus = SleeveRunStatus.PENDING,
        now: datetime | None = None,
    ) -> SleeveRun:
        """Create the deterministic run once, or return its compatible record."""

        cohort = cohort_id.strip()
        if not cohort:
            raise ValueError("cohort_id must not be empty")
        members = _members(expected_members)
        key = scheduling.run_key(cohort, session)
        run_id = scheduling.run_fingerprint(cohort, session)
        stamp = _utc(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT expected_members, cohort_id, session_id FROM sleeve_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO sleeve_runs (run_id, run_key, cohort_id, session_id, "
                    "scheduled_for, expected_members, completed_members, snapshot_id, "
                    "quote_snapshot_id, data_snapshot_ids, started_at, completed_at, status, "
                    "errors) VALUES (?, ?, ?, ?, ?, ?, '[]', NULL, NULL, '{}', ?, NULL, ?, '[]')",
                    (
                        run_id,
                        key,
                        cohort,
                        session.session_id,
                        session.session_date.isoformat(),
                        json.dumps(list(members)),
                        stamp.isoformat(),
                        status.value,
                    ),
                )
                for ordinal, member in enumerate(members):
                    conn.execute(
                        "INSERT INTO sleeve_run_members (run_id, sleeve_id, ordinal, status) "
                        "VALUES (?, ?, ?, ?)",
                        (run_id, member, ordinal, MemberRunStatus.PENDING.value),
                    )
            else:
                stored = tuple(json.loads(existing["expected_members"]))
                if (
                    existing["cohort_id"] != cohort
                    or existing["session_id"] != session.session_id
                    or stored != members
                ):
                    raise SleeveRunConflictError(
                        "the cohort/session run already exists with different expected members"
                    )
        result = self.get(run_id)
        assert result is not None
        return result

    def get(self, run_id: str) -> SleeveRun | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sleeve_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            member_rows = conn.execute(
                "SELECT * FROM sleeve_run_members WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        return self._row_to_run(row, member_rows)

    def get_by_key(self, run_key: str) -> SleeveRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM sleeve_runs WHERE run_key = ?", (run_key,)
            ).fetchone()
        return None if row is None else self.get(str(row["run_id"]))

    def list(self, *, cohort_id: str | None = None, limit: int = 200) -> list[SleeveRun]:
        query = "SELECT run_id FROM sleeve_runs"
        params: list[object] = []
        if cohort_id is not None:
            query += " WHERE cohort_id = ?"
            params.append(cohort_id)
        query += " ORDER BY scheduled_for DESC, started_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [run for row in rows if (run := self.get(str(row["run_id"]))) is not None]

    def completed_run_keys(self, *, cohort_id: str | None = None) -> frozenset[str]:
        query = "SELECT run_key FROM sleeve_runs WHERE status = ?"
        params: list[object] = [SleeveRunStatus.COMPLETED.value]
        if cohort_id is not None:
            query += " AND cohort_id = ?"
            params.append(cohort_id)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return frozenset(str(row["run_key"]) for row in rows)

    def set_status(
        self,
        run_id: str,
        status: SleeveRunStatus,
        *,
        error: SleeveRunError | None = None,
        now: datetime | None = None,
        terminal: bool = False,
    ) -> SleeveRun:
        stamp = _utc(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT errors FROM sleeve_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            errors = list(json.loads(row["errors"]))
            if error is not None:
                record_run_error(errors, error)
            conn.execute(
                "UPDATE sleeve_runs SET status = ?, errors = ?, completed_at = ? WHERE run_id = ?",
                (
                    status.value,
                    json.dumps(errors, sort_keys=True),
                    stamp.isoformat() if terminal else None,
                    run_id,
                ),
            )
        result = self.get(run_id)
        assert result is not None
        return result

    def set_snapshot(
        self,
        run_id: str,
        *,
        snapshot_id: str,
        quote_snapshot_id: str,
        data_snapshot_ids: Mapping[str, str],
        allow_replace: bool = False,
    ) -> SleeveRun:
        """Bind the run to the immutable snapshot its members execute against.

        Once written, a differing snapshot raises :class:`SnapshotMismatchError`: paper
        state is bound to the data that produced it, so a resume must reproduce exactly
        what it already used.

        ``allow_replace`` relaxes that for the one case where the constraint protects
        nothing — a run in which **no member has started**, and therefore no fill,
        cycle, or observation depends on the stored identity. Callers must derive it
        from member state, never from convenience.
        """
        if not snapshot_id.strip() or not quote_snapshot_id.strip():
            raise ValueError("snapshot identities must not be empty")
        normalized = dict(sorted(data_snapshot_ids.items()))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT snapshot_id, quote_snapshot_id, data_snapshot_ids "
                "FROM sleeve_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["snapshot_id"] is not None and not allow_replace:
                stored_data = json.loads(row["data_snapshot_ids"])
                if (
                    row["snapshot_id"] != snapshot_id
                    or row["quote_snapshot_id"] != quote_snapshot_id
                    or stored_data != normalized
                ):
                    raise SnapshotMismatchError(
                        "the captured snapshot does not match the persisted run snapshot"
                    )
            else:
                conn.execute(
                    "UPDATE sleeve_runs SET snapshot_id = ?, quote_snapshot_id = ?, "
                    "data_snapshot_ids = ? WHERE run_id = ?",
                    (
                        snapshot_id,
                        quote_snapshot_id,
                        json.dumps(normalized, sort_keys=True),
                        run_id,
                    ),
                )
        result = self.get(run_id)
        assert result is not None
        return result

    def start_member(self, run_id: str, sleeve_id: str, *, now: datetime | None = None) -> bool:
        """Atomically claim one pending member before paper state can be mutated."""

        stamp = _utc(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE sleeve_run_members SET status = ?, started_at = ?, error = NULL "
                "WHERE run_id = ? AND sleeve_id = ? AND status = ?",
                (
                    MemberRunStatus.RUNNING.value,
                    stamp.isoformat(),
                    run_id,
                    sleeve_id,
                    MemberRunStatus.PENDING.value,
                ),
            )
        return cursor.rowcount == 1

    def finish_member(
        self,
        run_id: str,
        sleeve_id: str,
        *,
        status: MemberRunStatus,
        error: SleeveRunError | None = None,
        now: datetime | None = None,
    ) -> SleeveRun:
        if status in (MemberRunStatus.PENDING, MemberRunStatus.RUNNING):
            raise ValueError("finish_member requires a terminal member status")
        stamp = _utc(now)
        serialized = None if error is None else error.model_dump_json()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM sleeve_run_members WHERE run_id = ? AND sleeve_id = ?",
                (run_id, sleeve_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"{run_id}:{sleeve_id}")
            current = MemberRunStatus(row["status"])
            if current is MemberRunStatus.COMPLETED and status is not MemberRunStatus.COMPLETED:
                raise SleeveRunConflictError("a completed member cannot be downgraded")
            conn.execute(
                "UPDATE sleeve_run_members SET status = ?, completed_at = ?, error = ? "
                "WHERE run_id = ? AND sleeve_id = ?",
                (status.value, stamp.isoformat(), serialized, run_id, sleeve_id),
            )
            if status is MemberRunStatus.COMPLETED:
                rows = conn.execute(
                    "SELECT sleeve_id FROM sleeve_run_members WHERE run_id = ? AND status = ? "
                    "ORDER BY ordinal",
                    (run_id, MemberRunStatus.COMPLETED.value),
                ).fetchall()
                completed = [str(member["sleeve_id"]) for member in rows]
                conn.execute(
                    "UPDATE sleeve_runs SET completed_members = ? WHERE run_id = ?",
                    (json.dumps(completed), run_id),
                )
            if error is not None:
                run_row = conn.execute(
                    "SELECT errors FROM sleeve_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                errors = list(json.loads(run_row["errors"]))
                item = error.model_dump(mode="json")
                if item not in errors:
                    errors.append(item)
                    conn.execute(
                        "UPDATE sleeve_runs SET errors = ? WHERE run_id = ?",
                        (json.dumps(errors, sort_keys=True), run_id),
                    )
        result = self.get(run_id)
        assert result is not None
        return result

    def finalize(self, run_id: str, *, now: datetime | None = None) -> SleeveRun:
        """Derive the run result from durable member outcomes."""

        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        statuses = {member.status for member in run.members}
        if statuses == {MemberRunStatus.COMPLETED}:
            result = SleeveRunStatus.COMPLETED
        elif run.completed_members:
            result = SleeveRunStatus.PARTIAL
        elif MemberRunStatus.PENDING in statuses or MemberRunStatus.RUNNING in statuses:
            result = SleeveRunStatus.RUNNING
        else:
            result = SleeveRunStatus.FAILED
        return self.set_status(
            run_id,
            result,
            now=now,
            terminal=result
            in {
                SleeveRunStatus.COMPLETED,
                SleeveRunStatus.PARTIAL,
                SleeveRunStatus.FAILED,
            },
        )

    def _row_to_run(self, row: sqlite3.Row, member_rows: Sequence[sqlite3.Row]) -> SleeveRun:
        members = tuple(
            SleeveRunMember(
                sleeve_id=member["sleeve_id"],
                status=MemberRunStatus(member["status"]),
                started_at=(
                    datetime.fromisoformat(member["started_at"]) if member["started_at"] else None
                ),
                completed_at=(
                    datetime.fromisoformat(member["completed_at"])
                    if member["completed_at"]
                    else None
                ),
                error=(
                    SleeveRunError.model_validate_json(member["error"]) if member["error"] else None
                ),
            )
            for member in member_rows
        )
        return SleeveRun(
            run_id=row["run_id"],
            run_key=row["run_key"],
            cohort_id=row["cohort_id"],
            session_id=row["session_id"],
            scheduled_for=date.fromisoformat(row["scheduled_for"]),
            expected_members=tuple(json.loads(row["expected_members"])),
            completed_members=tuple(json.loads(row["completed_members"])),
            snapshot_id=row["snapshot_id"],
            quote_snapshot_id=row["quote_snapshot_id"],
            data_snapshot_ids=dict(json.loads(row["data_snapshot_ids"])),
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            status=SleeveRunStatus(row["status"]),
            errors=tuple(SleeveRunError.model_validate(item) for item in json.loads(row["errors"])),
            members=members,
        )

    @contextmanager
    def official_session(
        self,
        cohort_id: str,
        scheduled_for: date,
        *,
        owner_id: str | None = None,
    ) -> Iterator[bool]:
        """Acquire a local lease for one official cohort session.

        ``owner_id`` is accepted for parity with PostgreSQL but is deliberately not
        stored in the local file. The shared backend additionally holds a PostgreSQL
        advisory lock for the full context.
        """
        del owner_id
        token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        stamp = datetime.now(UTC)
        expires = stamp.replace(microsecond=0) + timedelta(hours=6)
        acquired = False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT expires_at, released_at FROM official_session_leases "
                "WHERE cohort_id = ? AND scheduled_for = ?",
                (cohort_id, scheduled_for.isoformat()),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO official_session_leases "
                    "(cohort_id, scheduled_for, token_hash, acquired_at, expires_at, released_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        cohort_id,
                        scheduled_for.isoformat(),
                        token_hash,
                        stamp.isoformat(),
                        expires.isoformat(),
                    ),
                )
                acquired = True
            elif (
                row["released_at"] is not None or datetime.fromisoformat(row["expires_at"]) <= stamp
            ):
                conn.execute(
                    "UPDATE official_session_leases SET token_hash = ?, acquired_at = ?, "
                    "expires_at = ?, released_at = NULL "
                    "WHERE cohort_id = ? AND scheduled_for = ?",
                    (
                        token_hash,
                        stamp.isoformat(),
                        expires.isoformat(),
                        cohort_id,
                        scheduled_for.isoformat(),
                    ),
                )
                acquired = True
        try:
            yield acquired
        finally:
            if acquired:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE official_session_leases SET released_at = ? "
                        "WHERE cohort_id = ? AND scheduled_for = ? AND token_hash = ?",
                        (
                            datetime.now(UTC).isoformat(),
                            cohort_id,
                            scheduled_for.isoformat(),
                            token_hash,
                        ),
                    )


@dataclass(frozen=True)
class SnapshotCoverage:
    """A provenance envelope containing coverage only, with no invented records."""

    provenance: Provenance
    keys: frozenset[str]

    @property
    def covered_keys(self) -> frozenset[str]:
        return self.keys

    def is_empty(self) -> bool:
        return not self.keys


@dataclass(frozen=True)
class CohortSnapshot:
    """One immutable quote/data capture shared by every member in a run."""

    snapshot_id: str
    quote_snapshot_id: str
    captured_at: datetime
    quotes: Mapping[str, Quote]
    resources: strategy_registry.StrategyResources
    readiness_by_member: Mapping[str, DataReadiness]
    data_snapshot_ids: Mapping[str, str]
    operator_diagnostics: Mapping[str, object] = field(default_factory=dict)
    opening_bars: Mapping[str, OpeningBarEvidence] = field(default_factory=dict)
    """Validated opening-interval evidence keyed by symbol, for the *execution*
    session under a next-open methodology. Empty for close-marked cohorts, which
    execute against the quote snapshot and never consult it."""


SnapshotProvider = Callable[
    [tuple[SleeveConfig, ...], scheduling.ExchangeSession, str | None], CohortSnapshot
]
UniverseResolver = Callable[[SleeveConfig], list[str]]
StrategyBuilder = Callable[[SleeveConfig, list[str], strategy_registry.StrategyResources], Strategy]
EngineFactory = Callable[[SleeveConfig], PaperEngine]
EvaluationFactory = Callable[[SleeveConfig], EvaluationStore]
MethodologyResolver = Callable[[SleeveConfig], ExecutionMethodology]


class MethodologyConflictError(RuntimeError):
    """The cohort's members do not agree on one execution-timing methodology.

    A cohort exists to compare sleeves against one another under shared assumptions.
    Two members executing at different instants is not a cohort, so this fails the
    session closed rather than running a comparison that means nothing.
    """


def _default_methodology_resolver(cfg: SleeveConfig) -> ExecutionMethodology:
    return execution_timing.resolve_methodology(cfg.execution_methodology)


def _timing_fields(
    plan: SessionPlan | None,
    *,
    fallback: datetime,
    valuation_fallback: datetime | None = None,
) -> dict[str, Any]:
    """The observation's timing columns for one methodology.

    A close-marked cohort writes exactly the two columns it always wrote, with exactly
    the values it always wrote. That is deliberate rather than lazy: the signal and
    execution sessions coincide there, ``session_date`` and ``decision_time`` already
    say so, and leaving the new columns unset keeps every existing record and every
    idempotent re-record byte-identical to what is stored today.

    A next-open cohort writes all four instants and both session identities, because
    there the two sessions genuinely differ and a reader must not have to infer which
    is which.
    """
    valuation = fallback if valuation_fallback is None else valuation_fallback
    if plan is None or not plan.methodology.requires_opening_evidence:
        return {"decision_time": fallback, "valuation_time": valuation}
    return {
        "decision_time": plan.decision_utc,
        "valuation_time": plan.valuation_utc,
        "execution_methodology": plan.methodology.key,
        "signal_session_date": plan.signal_session_date,
        "execution_session_date": plan.execution_session_date,
        "signal_time": plan.signal_utc,
        "execution_time": plan.execution_utc,
    }


def _opening_fill_quotes(
    plan: SessionPlan,
    opening_bars: Mapping[str, OpeningBarEvidence],
    symbols: Sequence[str],
) -> dict[str, Quote]:
    """Turn validated opening bars into the quotes the paper engine fills against.

    The opening print is an auction result, not a two-sided market, so the spread and
    slippage the methodology declares are expressed *as* the bid/ask: a buy pays the
    modeled ask and a sell receives the modeled bid, which is exactly what
    :func:`schwab_trader.paper.fill_reference` already selects. ``mark`` stays the
    unadjusted print, so the sleeve is valued at what actually traded rather than at
    its own cost assumption.

    Only bars for the plan's execution session are used. A bar for any other session
    is dropped rather than substituted, and the caller's preflight has already refused
    the run in that case.
    """
    policy = plan.methodology.fill_policy
    assert policy is not None  # guaranteed by ExecutionMethodology's validator
    quotes: dict[str, Quote] = {}
    for symbol in symbols:
        evidence = opening_bars.get(symbol)
        if evidence is None or evidence.session_date != plan.execution_session_date:
            continue
        reference = evidence.reference_price
        quotes[symbol] = Quote(
            symbol=symbol,
            bid=policy.effective_price(OrderSide.SELL, reference),
            ask=policy.effective_price(OrderSide.BUY, reference),
            last=reference,
            mark=reference,
            quote_time=evidence.interval_start_at,
            trade_time=evidence.interval_start_at,
        )
    return quotes


def _default_strategy_builder(
    cfg: SleeveConfig,
    universe: list[str],
    resources: strategy_registry.StrategyResources,
) -> Strategy:
    if cfg.definition is None:
        raise strategy_registry.DefinitionMismatchError(
            f"Sleeve '{cfg.name}' has no persisted StrategyDefinition."
        )
    if cfg.configuration_hash != cfg.definition.configuration_hash:
        raise strategy_registry.DefinitionMismatchError(
            f"Sleeve '{cfg.name}' configuration hash does not match its definition."
        )
    return strategy_registry.reconstruct(cfg.definition, universe, resources=resources)


class SleeveRunOrchestrator:
    """Execute one scheduling decision against isolated paper sleeves."""

    def __init__(
        self,
        *,
        run_store: SleeveRunStore,
        sleeve_store: SleeveStore,
        kill_switch: KillSwitch,
        snapshot_provider: SnapshotProvider,
        universe_resolver: UniverseResolver,
        strategy_builder: StrategyBuilder = _default_strategy_builder,
        engine_factory: EngineFactory | None = None,
        evaluation_factory: EvaluationFactory | None = None,
        methodology_resolver: MethodologyResolver = _default_methodology_resolver,
    ) -> None:
        self.run_store = run_store
        self.sleeve_store = sleeve_store
        self.kill_switch = kill_switch
        self.snapshot_provider = snapshot_provider
        self.universe_resolver = universe_resolver
        self.strategy_builder = strategy_builder
        self.engine_factory = engine_factory or self._engine
        self.evaluation_factory = evaluation_factory or self._evaluation
        self.methodology_resolver = methodology_resolver
        self.last_preflight: cohort_preflight.PreflightResult | None = None
        """The most recent preflight verdict, for operator display only.

        Presentation state, never authorization: the durable run record and its errors
        remain the source of truth, and nothing reads this back to make a decision.
        """

    def _engine(self, cfg: SleeveConfig) -> PaperEngine:
        return PaperEngine(
            self.sleeve_store.paper_path(cfg.name),
            starting_cash=cfg.starting_cash,
            settle_t1=cfg.settlement_t1,
            leverage=cfg.leverage,
        )

    def _evaluation(self, cfg: SleeveConfig) -> EvaluationStore:
        return EvaluationStore(self.sleeve_store.eval_path(cfg.name))

    def resolve_plan(
        self,
        cohort_id: str,
        configs: Sequence[SleeveConfig],
        session: scheduling.ExchangeSession,
    ) -> SessionPlan:
        """Resolve the cohort's one signal/execution session pair for this session.

        Every member must name the same methodology. Deriving it per member would let
        a single cohort record two different experiments under one comparison, which
        is the mistake the cohort-wide preflight exists to prevent.
        """
        by_key: dict[str, list[str]] = {}
        for cfg in configs:
            by_key.setdefault(self.methodology_resolver(cfg).key, []).append(cfg.identity)
        if len(by_key) > 1:
            detail = "; ".join(
                f"{key}: {len(members)} member(s)" for key, members in sorted(by_key.items())
            )
            raise MethodologyConflictError(
                f"cohort members declare {len(by_key)} execution methodologies ({detail})"
            )
        methodology = (
            self.methodology_resolver(configs[0])
            if configs
            else execution_timing.MARK_TO_CLOSE_V1
        )
        execution_timing.ensure_methodology_allowed(cohort_id, methodology)
        policy = methodology.fill_policy
        if policy is not None and (
            policy.commission_per_share
            or policy.commission_per_order
            or policy.commission_minimum
        ):
            # The paper engine models no commission, so charging one here would be
            # recorded in the fill price and nowhere else. Refuse instead of quietly
            # understating costs; a commissioned methodology needs an engine that
            # books the charge, and that is a separate, reviewable change.
            raise MethodologyConflictError(
                f"methodology {methodology.key} declares commissions, which the paper "
                "engine does not yet model"
            )
        return execution_timing.plan_sessions(
            session.session_date,
            methodology=methodology,
            exchange=session.exchange,
        )

    def run(
        self,
        decision: scheduling.RunDecision,
        members: Sequence[SleeveConfig],
        *,
        now: datetime | None = None,
    ) -> SleeveRun:
        """Apply one official session while holding its database ownership lease."""
        with self.run_store.official_session(
            decision.cohort_id,
            decision.session.session_date,
        ) as acquired:
            if not acquired:
                key = decision.run_key or scheduling.run_key(decision.cohort_id, decision.session)
                existing = self.run_store.get_by_key(key)
                if existing is not None:
                    return existing
                raise SleeveRunConflictError("another runner owns this official cohort session")
            return self._run_owned(decision, members, now=now)

    def _run_owned(
        self,
        decision: scheduling.RunDecision,
        members: Sequence[SleeveConfig],
        *,
        now: datetime | None = None,
    ) -> SleeveRun:
        """Execute after official-session ownership has been acquired."""

        stamp = _utc(now)
        configs = tuple(members)
        expected = tuple(cfg.identity for cfg in configs)
        run = self.run_store.ensure_run(
            cohort_id=decision.cohort_id,
            session=decision.session,
            expected_members=expected,
            now=stamp,
        )

        # AWAITING_DATA is deliberately absent from the terminal set: it is the one
        # recorded outcome a later invocation is allowed to supersede.
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        if decision.status is scheduling.RunStatus.ALREADY_COMPLETED:
            return run
        if decision.status is scheduling.RunStatus.PENDING:
            return self.run_store.set_status(run.run_id, SleeveRunStatus.PENDING, now=stamp)
        if decision.status is scheduling.RunStatus.SKIPPED_CLOSED_SESSION:
            return self._skip_run(
                run,
                status=SleeveRunStatus.SKIPPED_CLOSED_SESSION,
                code="closed_session",
                message=decision.reason,
                now=stamp,
            )
        if decision.status is scheduling.RunStatus.MISSED:
            # A session that spent its whole window waiting for data must end as a
            # durable, notified result rather than retrying forever.
            if run.status is SleeveRunStatus.AWAITING_DATA:
                return self._expire_awaiting_data(run, configs, decision, now=stamp)
            return self._skip_run(
                run,
                status=SleeveRunStatus.MISSED,
                code="missed_session",
                message=decision.reason,
                now=stamp,
            )
        if not decision.should_run:
            return self.run_store.set_status(run.run_id, SleeveRunStatus.PENDING, now=stamp)

        if self.kill_switch.is_engaged():
            return self._skip_run(
                run,
                status=SleeveRunStatus.MISSED,
                code="kill_switch_engaged",
                message="The global kill switch was engaged before paper cohort execution.",
                now=stamp,
            )

        self.run_store.set_status(run.run_id, SleeveRunStatus.RUNNING, now=stamp)
        self._recover_interrupted(run, configs, stamp)
        refreshed = self.run_store.get(run.run_id)
        assert refreshed is not None
        run = refreshed
        pending_names = {
            member.sleeve_id for member in run.members if member.status is MemberRunStatus.PENDING
        }
        pending = tuple(cfg for cfg in configs if cfg.identity in pending_names)
        if not pending:
            return self.run_store.finalize(run.run_id, now=stamp)

        # A run that has never started a member has no paper state bound to its
        # snapshot, so a stale identity left by an `awaiting-data` verdict must not
        # constrain the retry. Only a partially executed run must reproduce what it
        # already used. Snapshot identity is content-derived, so it legitimately moves
        # when the awaited bar lands and even when quotes merely tick between polls;
        # pinning an unexecuted run to it would turn every retry into a hard failure.
        executed_any = any(member.status is not MemberRunStatus.PENDING for member in run.members)
        required_snapshot_id = run.snapshot_id if executed_any else None

        def fail_pending_closed(exc: Exception, *, retryable: bool) -> SleeveRun:
            error = SleeveRunError(
                code="snapshot_unavailable",
                message=f"{type(exc).__name__}: cohort snapshot could not be captured safely.",
                retryable=retryable,
            )
            for cfg in pending:
                self.run_store.finish_member(
                    run.run_id,
                    cfg.identity,
                    status=MemberRunStatus.FAILED,
                    error=error.model_copy(update={"member_id": cfg.identity}),
                    now=stamp,
                )
            return self.run_store.finalize(run.run_id, now=stamp)

        # The signal/execution session pair, resolved once for the whole cohort before
        # anything is fetched. A cohort that cannot state one methodology, or that would
        # apply new timing to a frozen experiment, must not reach the provider at all.
        try:
            plan = self.resolve_plan(decision.cohort_id, pending, decision.session)
        except Exception as exc:
            return self._fail_methodology(run, pending, decision.session, exc, now=stamp)

        try:
            snapshot = self.snapshot_provider(configs, decision.session, required_snapshot_id)
            if required_snapshot_id is not None and snapshot.snapshot_id != required_snapshot_id:
                raise SnapshotMismatchError(
                    "restart snapshot identity differs from the persisted cohort snapshot"
                )
        except Exception as exc:
            # Issue #109. A rejected refresh token is the one snapshot failure that is
            # provably harmless *here*: capture never started, so no paper account,
            # position, fill, cycle, decision, or official observation can have moved,
            # and no snapshot identity is bound to constrain the retry. Failing the
            # members closed would finalize a session that a two-minute `auth login`
            # can still record in full, and that record is unrecoverable afterwards.
            # Every other failure, and this one anywhere past this point, stays terminal.
            if (
                not executed_any
                and run.snapshot_id is None
                and is_retryable_pre_execution_failure(exc)
            ):
                return self._await_reauthentication(run, exc, now=stamp)
            return fail_pending_closed(exc, retryable=required_snapshot_id is None)

        # The all-or-nothing gate. Every member's data is judged together, before any
        # of them can change paper cash, positions, fills, cycles, or observations.
        # Under a next-open methodology that judgement includes the execution session's
        # opening evidence, so one member short of one opening bar stops the cohort.
        preflight = self._preflight(pending, decision.session, snapshot, plan)
        if preflight.structural:
            # Waiting cannot configure a capability. Fail now with the actionable
            # reason instead of burning the scheduling window and reporting `missed`.
            return self._fail_misconfigured(
                run, pending, decision.session, preflight, now=stamp, plan=plan
            )
        if not preflight.ready:
            if executed_any:
                # A crash mid-loop can leave members already executed. Such a run has
                # paper state bound to it and can never be all-or-nothing again, so it
                # resolves to its durable truth (partial, or failed if none completed)
                # rather than going back to waiting and later reporting a `missed` run
                # that mixes COMPLETED with DATA_NOT_READY members.
                return self._resolve_partially_executed(run, pending, preflight, now=stamp)
            return self._await_data(run, preflight, snapshot, now=stamp)

        # Only now does the identity become durable: the cohort is about to execute
        # against it, so from here on a resume must reproduce exactly this snapshot.
        try:
            run_symbols = tuple(symbol for cfg in configs for symbol in self.universe_resolver(cfg))
            self.run_store.set_snapshot(
                run.run_id,
                snapshot_id=snapshot.snapshot_id,
                quote_snapshot_id=snapshot.quote_snapshot_id,
                data_snapshot_ids=self._execution_data_snapshot_ids(snapshot, plan, run_symbols),
                # Replacement is permitted only while nothing has executed, which
                # closes the narrow window where a crash between this write and the
                # first `start_member` would otherwise strand the run permanently.
                allow_replace=not executed_any,
            )
        except Exception as exc:
            return fail_pending_closed(exc, retryable=not executed_any)

        for cfg in pending:
            if not self.run_store.start_member(run.run_id, cfg.identity, now=stamp):
                continue
            self._execute_member(run, cfg, decision.session, snapshot, stamp, plan)
        return self.run_store.finalize(run.run_id, now=stamp)

    def _preflight(
        self,
        pending: tuple[SleeveConfig, ...],
        session: scheduling.ExchangeSession,
        snapshot: CohortSnapshot,
        plan: SessionPlan | None = None,
    ) -> cohort_preflight.PreflightResult:
        """Judge every pending member's data against the target session at once."""
        quote_gaps = {
            cfg.identity: tuple(
                symbol for symbol in self.universe_resolver(cfg) if symbol not in snapshot.quotes
            )
            for cfg in pending
        }
        # The snapshot keys readiness by sleeve *name*; runs key members by identity.
        readiness = {
            cfg.identity: found
            for cfg in pending
            if (found := snapshot.readiness_by_member.get(cfg.name)) is not None
        }
        verdict = cohort_preflight.assess_preflight(
            readiness,
            session_date=session.session_date,
            members=tuple(cfg.identity for cfg in pending),
            quote_gaps=quote_gaps,
            extra_gaps=(
                *self._signal_quote_gaps(pending, snapshot, plan),
                *self._opening_evidence_gaps(pending, snapshot, plan),
            ),
        )
        self.last_preflight = verdict
        return verdict

    def _signal_quote_gaps(
        self,
        pending: tuple[SleeveConfig, ...],
        snapshot: CohortSnapshot,
        plan: SessionPlan | None,
    ) -> tuple[cohort_preflight.DataGap, ...]:
        """Reject quotes that were not captured from the signal session.

        A retry after T+1 opens must not turn the provider's current quote into the
        decision evidence. The production opening-bar integration is intentionally
        deferred, so this gate also ensures that later wiring must retain a frozen T
        quote snapshot rather than accidentally introducing look-ahead.
        """
        if plan is None or not plan.methodology.requires_opening_evidence:
            return ()
        signal_open, signal_close = market_calendar.session_bounds_utc(plan.signal_session_date)
        try:
            captured = _utc(snapshot.captured_at)
        except ValueError:
            captured = None
        gaps: list[cohort_preflight.DataGap] = []
        for cfg in pending:
            symbols = tuple(dict.fromkeys(self.universe_resolver(cfg)))
            if captured is None or captured < plan.decision_utc:
                gaps.append(
                    cohort_preflight.DataGap(
                        member_id=cfg.identity,
                        kind=cohort_preflight.SIGNAL_QUOTES_KIND,
                        reason="retrieved_before_session_close",
                        target_session=plan.signal_session_date,
                        uncovered_keys=symbols,
                        detail="The signal quote snapshot was captured before T closed.",
                    )
                )
                continue
            invalid: list[str] = []
            for symbol in symbols:
                quote = snapshot.quotes.get(symbol)
                if quote is None:
                    continue
                try:
                    quote_time = _utc(quote.quote_time)
                except ValueError:
                    quote_time = None
                if (
                    quote.symbol.strip().upper() != symbol.strip().upper()
                    or quote_time is None
                    or not signal_open <= quote_time <= signal_close
                ):
                    invalid.append(symbol)
            if invalid:
                gaps.append(
                    cohort_preflight.DataGap(
                        member_id=cfg.identity,
                        kind=cohort_preflight.SIGNAL_QUOTES_KIND,
                        reason="session_not_covered",
                        target_session=plan.signal_session_date,
                        uncovered_keys=tuple(invalid),
                        detail="Quote timestamps must belong to the signal session.",
                    )
                )
        return tuple(gaps)

    def _opening_evidence_gaps(
        self,
        pending: tuple[SleeveConfig, ...],
        snapshot: CohortSnapshot,
        plan: SessionPlan | None,
    ) -> tuple[cohort_preflight.DataGap, ...]:
        """One gap per member whose universe lacks usable execution-session opens.

        Non-structural by construction: at session ``T``'s close the ``T+1`` opening
        print genuinely does not exist yet, so the correct verdict is a retryable wait
        that the scheduler's existing deadline already bounds. There is no path here
        that substitutes the close, the previous open, or a neighbouring interval.
        """
        if plan is None or not plan.methodology.requires_opening_evidence:
            return ()
        gaps: list[cohort_preflight.DataGap] = []
        for cfg in pending:
            assessment = execution_timing.assess_opening_evidence(
                self.universe_resolver(cfg),
                snapshot.opening_bars,
                execution_session=plan.execution_session_date,
                interval_minutes=plan.methodology.opening_interval_minutes,
                exchange=plan.exchange,
            )
            if assessment.ready:
                continue
            gaps.append(
                cohort_preflight.DataGap(
                    member_id=cfg.identity,
                    kind=cohort_preflight.OPENING_BARS_KIND,
                    reason=assessment.reason,
                    target_session=plan.execution_session_date,
                    missing_keys=assessment.missing_symbols,
                    uncovered_keys=tuple(
                        sorted(
                            {
                                *assessment.mismatched_symbols,
                                *assessment.invalid_symbols,
                                *assessment.ambiguous_symbols,
                            }
                        )
                    ),
                    detail=assessment.detail,
                )
            )
        return tuple(gaps)

    def _fail_methodology(
        self,
        run: SleeveRun,
        pending: tuple[SleeveConfig, ...],
        session: scheduling.ExchangeSession,
        exc: Exception,
        *,
        now: datetime,
    ) -> SleeveRun:
        """Fail closed when the cohort cannot state one lawful execution methodology.

        Nothing has been fetched or executed, so this is still all-or-nothing: every
        member records a truthful ``MISSING`` observation and the run is not retried,
        because a configuration conflict cannot resolve itself by waiting.
        """
        reasons = ("execution_timing:methodology_invalid",)
        message = f"{type(exc).__name__}: {exc}"
        for cfg in pending:
            self._record_missing_observation(run, cfg, session, reasons, plan=None)
            self.run_store.finish_member(
                run.run_id,
                cfg.identity,
                status=MemberRunStatus.DATA_NOT_READY,
                error=SleeveRunError(
                    code="execution_methodology_invalid",
                    message=message,
                    member_id=cfg.identity,
                    capability="execution_timing",
                    retryable=False,
                    reasons=reasons,
                ),
                now=now,
            )
        return self.run_store.finalize(run.run_id, now=now)

    def _await_data(
        self,
        run: SleeveRun,
        preflight: cohort_preflight.PreflightResult,
        snapshot: CohortSnapshot,
        *,
        now: datetime,
    ) -> SleeveRun:
        """Record a retryable wait. No member is started and nothing is observed.

        The error is derived only from deterministic facts (member ids, data kinds,
        reason codes, session dates), so repeated scheduler invocations that reach the
        same verdict deduplicate into a single durable record instead of accumulating.
        """
        error = SleeveRunError(
            code=AWAITING_DATA_CODE,
            message=preflight.summary,
            capability=",".join(preflight.kinds),
            retryable=True,
            reasons=preflight.reason_codes,
            context=dict(snapshot.operator_diagnostics),
        )
        return self.run_store.set_status(
            run.run_id,
            SleeveRunStatus.AWAITING_DATA,
            error=error,
            now=now,
            terminal=False,
        )

    def _await_reauthentication(
        self,
        run: SleeveRun,
        exc: Exception,
        *,
        now: datetime,
    ) -> SleeveRun:
        """Park a session that cannot capture a snapshot until the operator authenticates.

        Reached only when nothing has started and no snapshot is bound, so this records
        the same kind of non-terminal wait the preflight records: no member is started,
        no observation is written, and no paper state is touched. The distinguishing
        ``awaiting_reauthentication`` code is what lets health, recovery, the dashboard,
        and the alert channel give the one instruction that resolves it.

        The message is fixed text plus the exception's *type* name. Authentication
        failures are exactly the class of error whose text can carry an authorization
        URL, callback value, or provider payload, so ``str(exc)`` is never persisted or
        displayed. Being fixed also makes the row deterministic, so repeated polls
        deduplicate into one durable transition and notify once.
        """
        error = SleeveRunError(
            code=AWAITING_REAUTH_CODE,
            message=(
                "Schwab reauthentication is required before the cohort snapshot can be "
                "captured. No member executed and nothing was recorded. Run "
                "'python -m schwab_trader auth login' on the runner machine; this "
                "session is still recordable until its scheduling deadline."
            ),
            capability="authentication",
            retryable=True,
            reasons=("authentication:reauthorization_required",),
            context={"failure": type(exc).__name__},
        )
        return self.run_store.set_status(
            run.run_id,
            SleeveRunStatus.AWAITING_DATA,
            error=error,
            now=now,
            terminal=False,
        )

    def _resolve_partially_executed(
        self,
        run: SleeveRun,
        pending: tuple[SleeveConfig, ...],
        preflight: cohort_preflight.PreflightResult,
        *,
        now: datetime,
    ) -> SleeveRun:
        """Close out a run whose all-or-nothing guarantee was already spent.

        Reached only after an interrupted attempt left some members executed. Their
        paper state is real, so the session's durable truth is what it managed to
        record; the remaining members are marked data-not-ready and nothing is replayed.
        """
        for cfg in pending:
            self.run_store.finish_member(
                run.run_id,
                cfg.identity,
                status=MemberRunStatus.DATA_NOT_READY,
                error=SleeveRunError(
                    code="data_not_ready",
                    message=(
                        "A previous attempt had already executed members, so this "
                        f"session could not wait for data. {preflight.summary}"
                    ),
                    member_id=cfg.identity,
                    capability=",".join(preflight.kinds),
                    retryable=False,
                ),
                now=now,
            )
        return self.run_store.finalize(run.run_id, now=now)

    def _fail_misconfigured(
        self,
        run: SleeveRun,
        pending: tuple[SleeveConfig, ...],
        session: scheduling.ExchangeSession,
        preflight: cohort_preflight.PreflightResult,
        *,
        now: datetime,
        plan: SessionPlan | None = None,
    ) -> SleeveRun:
        """Fail closed immediately on a gap that waiting can never resolve.

        Still all-or-nothing: no member executed, so each records a truthful ``MISSING``
        observation rather than a ``PARTIAL`` one implying a result was attempted.
        """
        reasons = tuple(sorted({gap.code for gap in preflight.structural_gaps}))
        for cfg in pending:
            self._record_missing_observation(run, cfg, session, reasons, plan=plan)
            self.run_store.finish_member(
                run.run_id,
                cfg.identity,
                status=MemberRunStatus.DATA_NOT_READY,
                error=SleeveRunError(
                    code="data_misconfigured",
                    message=preflight.summary,
                    member_id=cfg.identity,
                    capability=",".join(preflight.kinds),
                    retryable=False,
                    reasons=reasons,
                ),
                now=now,
            )
        return self.run_store.finalize(run.run_id, now=now)

    def _expire_awaiting_data(
        self,
        run: SleeveRun,
        configs: tuple[SleeveConfig, ...],
        decision: scheduling.RunDecision,
        *,
        now: datetime,
    ) -> SleeveRun:
        """Convert an exhausted wait into a durable missed result.

        Every member is recorded ``data-not-ready`` and given a ``MISSING`` official
        observation carrying the reason the wait never resolved. ``MISSING`` is the
        truthful status here: the session produced no result at all, and fabricating a
        zero return would silently bias the comparison this cohort exists to make.

        Both wait codes are read, so an expired *authentication* wait produces the same
        truthful missed outcome carrying its own reason rather than an empty one.
        """
        waiting = [error for error in run.errors if error.code in WAIT_ERROR_CODES]
        why = waiting[-1].message if waiting else decision.reason
        # What the session was actually blocked on, so the durable record does not say
        # "data" about a session whose data may have been fine and whose token was not.
        blocked_on = (
            "Authentication never completed"
            if waiting and waiting[-1].code == AWAITING_REAUTH_CODE
            else "Required data never became ready"
        )
        # The structured codes the preflight recorded, e.g. daily_bars:session_not_covered.
        # Falling back to `capability` would persist only the data *kind*, leaving this
        # observation the one place in the record that cannot be parsed like the rest.
        reasons = tuple(dict.fromkeys(code for error in waiting for code in error.reasons))
        by_identity = {cfg.identity: cfg for cfg in configs}
        session = decision.session
        # An expiring wait may still have a resolvable plan; when it does not (a bad
        # methodology is one reason the wait never cleared), the observation simply
        # records the session's own instants, exactly as it did before #79.
        try:
            plan: SessionPlan | None = self.resolve_plan(decision.cohort_id, configs, session)
        except Exception:
            plan = None
        for member in run.members:
            if member.status is not MemberRunStatus.PENDING:
                continue
            cfg = by_identity.get(member.sleeve_id)
            if cfg is not None:
                self._record_missing_observation(run, cfg, session, reasons, plan=plan)
            self.run_store.finish_member(
                run.run_id,
                member.sleeve_id,
                status=MemberRunStatus.DATA_NOT_READY,
                error=SleeveRunError(
                    code="data_deadline_exceeded",
                    message=f"{blocked_on} before the session deadline; {why}",
                    member_id=member.sleeve_id,
                    retryable=False,
                ),
                now=now,
            )
        return self.run_store.set_status(
            run.run_id,
            SleeveRunStatus.MISSED,
            error=SleeveRunError(
                code="data_deadline_exceeded",
                message=f"{decision.reason} The cohort was waiting throughout: {why}",
            ),
            now=now,
            terminal=True,
        )

    def _record_missing_observation(
        self,
        run: SleeveRun,
        cfg: SleeveConfig,
        session: scheduling.ExchangeSession,
        reasons: Sequence[str],
        *,
        plan: SessionPlan | None,
    ) -> None:
        """Persist a truthful ``MISSING`` observation for a session that never ran.

        ``readiness_reasons`` carries structured ``"<kind>:<reason>"`` codes only, the
        same shape every other observation uses. The human explanation belongs on the
        run's error record, not mixed into a list consumers parse: a prose sentence
        among the codes makes the whole field unparseable.

        ``snapshot_ids`` is deliberately empty. No member executed, so no snapshot was
        used, and attaching the identity the run merely *looked at* would imply a
        lineage that does not exist — with a different key set from the executed path's
        ``cohort_snapshot``, which downstream matched-lineage comparisons would then
        have to special-case.
        """
        fallback = session.decision_utc or run.started_at
        self.evaluation_factory(cfg).record_official_observation(
            OfficialDailyObservation(
                cohort_id=run.cohort_id,
                run_id=run.run_id,
                sleeve_id=cfg.identity,
                strategy=cfg.strategy,
                strategy_hash=cfg.configuration_hash,
                session_date=session.session_date,
                status=ObservationStatus.MISSING,
                snapshot_ids={},
                readiness_ready=False,
                readiness_reasons=tuple(dict.fromkeys(reasons)),
                **_timing_fields(plan, fallback=fallback),
            )
        )

    def _skip_run(
        self,
        run: SleeveRun,
        *,
        status: SleeveRunStatus,
        code: str,
        message: str,
        now: datetime,
    ) -> SleeveRun:
        for member in run.members:
            if member.status is MemberRunStatus.PENDING:
                error = SleeveRunError(
                    code=code,
                    message=message,
                    member_id=member.sleeve_id,
                )
                self.run_store.finish_member(
                    run.run_id,
                    member.sleeve_id,
                    status=MemberRunStatus.SKIPPED,
                    error=error,
                    now=now,
                )
        return self.run_store.set_status(
            run.run_id,
            status,
            error=SleeveRunError(code=code, message=message),
            now=now,
            terminal=True,
        )

    def _recover_interrupted(
        self,
        run: SleeveRun,
        configs: tuple[SleeveConfig, ...],
        now: datetime,
    ) -> None:
        by_identity = {cfg.identity: cfg for cfg in configs}
        for member in run.members:
            if member.status is not MemberRunStatus.RUNNING:
                continue
            cfg = by_identity[member.sleeve_id]
            official = self._official_for_run(cfg, run)
            if official is not None and official.status is ObservationStatus.OFFICIAL:
                self.run_store.finish_member(
                    run.run_id,
                    cfg.identity,
                    status=MemberRunStatus.COMPLETED,
                    now=now,
                )
                continue
            error = SleeveRunError(
                code="ambiguous_interrupted_member",
                message=(
                    "A previous attempt stopped after the member checkpoint and before an "
                    "official observation; it was not replayed to avoid duplicate paper fills."
                ),
                member_id=cfg.identity,
            )
            self.run_store.finish_member(
                run.run_id,
                cfg.identity,
                status=MemberRunStatus.INTERRUPTED,
                error=error,
                now=now,
            )

    def _official_for_run(
        self, cfg: SleeveConfig, run: SleeveRun
    ) -> OfficialDailyObservation | None:
        for observation in self.evaluation_factory(cfg).official_observations(limit=10_000):
            if (
                observation.cohort_id == run.cohort_id
                and observation.sleeve_id == cfg.identity
                and observation.session_date == run.scheduled_for
                and observation.run_id == run.run_id
            ):
                return observation
        return None

    def _execute_member(
        self,
        run: SleeveRun,
        cfg: SleeveConfig,
        session: scheduling.ExchangeSession,
        snapshot: CohortSnapshot,
        now: datetime,
        plan: SessionPlan,
    ) -> None:
        readiness = snapshot.readiness_by_member.get(cfg.name)
        if readiness is None:
            error = SleeveRunError(
                code="readiness_missing",
                message="No DataReadiness result was captured for this cohort member.",
                member_id=cfg.identity,
            )
            self.run_store.finish_member(
                run.run_id,
                cfg.identity,
                status=MemberRunStatus.DATA_NOT_READY,
                error=error,
                now=now,
            )
            return
        if not readiness.ready:
            # Defence in depth: the cohort preflight should already have refused, so
            # reaching here means a member's readiness changed under us. The reasons
            # are not restated here — _record_incomplete_observation derives them from
            # the readiness result, and passing them twice is what produced the
            # duplicated `daily_bars:stale` in the original incident record.
            error = SleeveRunError(
                code="data_not_ready",
                message="Required strategy data failed readiness checks.",
                member_id=cfg.identity,
                capability=",".join(sorted({item.kind.value for item in readiness.unready()})),
            )
            self._record_incomplete_observation(run, cfg, session, snapshot, readiness, (), plan)
            self.run_store.finish_member(
                run.run_id,
                cfg.identity,
                status=MemberRunStatus.DATA_NOT_READY,
                error=error,
                now=now,
            )
            return

        universe = self.universe_resolver(cfg)
        missing_quotes = sorted(symbol for symbol in universe if symbol not in snapshot.quotes)
        if missing_quotes:
            error = SleeveRunError(
                code="quote_coverage_incomplete",
                message="The shared quote snapshot did not cover the member universe.",
                member_id=cfg.identity,
                capability="quotes",
            )
            self._record_incomplete_observation(
                run,
                cfg,
                session,
                snapshot,
                readiness,
                [f"quotes:missing:{symbol}" for symbol in missing_quotes],
                plan,
            )
            self.run_store.finish_member(
                run.run_id,
                cfg.identity,
                status=MemberRunStatus.DATA_NOT_READY,
                error=error,
                now=now,
            )
            return

        try:
            strategy = self.strategy_builder(cfg, universe, snapshot.resources)
            engine = self.engine_factory(cfg)

            def quote_source(symbol: str) -> Quote:
                quote = snapshot.quotes.get(symbol)
                if quote is None:
                    raise QuoteError(f"No quote for {symbol} in the cohort snapshot.")
                return quote

            # Under a next-open methodology the strategy still decides on the signal
            # session's close quotes, but the order fills and the sleeve is marked at
            # the execution session's opening print. A symbol with no opening quote
            # raises rather than falling back to the close — the preflight has already
            # guaranteed full coverage, so reaching that point is a defect, not a case
            # to paper over with the stale price.
            execution_source: QuoteSource | None = None
            if plan.methodology.requires_opening_evidence:
                opening = _opening_fill_quotes(plan, snapshot.opening_bars, universe)

                def execution_quote(symbol: str) -> Quote:
                    quote = opening.get(symbol)
                    if quote is None:
                        raise QuoteError(
                            f"No {plan.execution_session_date.isoformat()} opening bar "
                            f"for {symbol} in the cohort snapshot."
                        )
                    return quote

                execution_source = execution_quote

            # ``AgentRunner`` receives the decision instant. Its fill/mark sources
            # carry the later execution timestamp, so strategy context cannot observe
            # T+1 merely to make the paper order's ``filled_at`` come out correctly.
            run_at = plan.decision_utc if plan else session.decision_utc or now
            report = AgentRunner(strategy, engine, quote_source).run_cycle(
                now=run_at,
                fill_source=execution_source,
                mark_source=execution_source,
            )
            evaluations = self.evaluation_factory(cfg)
            evaluations.record_cycle(report)
            evaluations.record_official_observation(
                self._official_observation(
                    run, cfg, session, snapshot, readiness, report, engine, plan
                )
            )
            self.run_store.finish_member(
                run.run_id,
                cfg.identity,
                status=MemberRunStatus.COMPLETED,
                now=now,
            )
        except Exception as exc:
            error = SleeveRunError(
                code="member_execution_failed",
                message=f"{type(exc).__name__}: paper member execution failed.",
                member_id=cfg.identity,
            )
            self._record_incomplete_observation(
                run,
                cfg,
                session,
                snapshot,
                readiness,
                ["execution:failed"],
                plan,
            )
            self.run_store.finish_member(
                run.run_id,
                cfg.identity,
                status=MemberRunStatus.FAILED,
                error=error,
                now=now,
            )

    @staticmethod
    def _execution_data_snapshot_ids(
        snapshot: CohortSnapshot,
        plan: SessionPlan | None,
        symbols: Sequence[str],
    ) -> dict[str, str]:
        """Data identities sufficient to reproduce the timing and opening inputs.

        Close-marked observations return the pre-#79 mapping byte-for-byte. A
        next-open observation additionally pins the full methodology hash (which
        includes fill costs) and the content digest of every opening bar it used.
        """
        identities = dict(snapshot.data_snapshot_ids)
        if plan is None or not plan.methodology.requires_opening_evidence:
            return identities
        identities["execution_methodology"] = plan.methodology.methodology_hash
        by_symbol: dict[str, list[OpeningBarEvidence]] = {}
        for key, evidence in snapshot.opening_bars.items():
            by_symbol.setdefault(key.strip().upper(), []).append(evidence)
        for symbol in sorted({item.strip().upper() for item in symbols if item.strip()}):
            found = by_symbol.get(symbol, [])
            if len(found) == 1:
                identities[f"opening_bar:{symbol}"] = found[0].evidence_digest
        return dict(sorted(identities.items()))

    @classmethod
    def _snapshot_ids(
        cls,
        snapshot: CohortSnapshot,
        plan: SessionPlan | None,
        symbols: Sequence[str],
    ) -> dict[str, str]:
        return {
            "cohort_snapshot": snapshot.snapshot_id,
            "quotes": snapshot.quote_snapshot_id,
            **cls._execution_data_snapshot_ids(snapshot, plan, symbols),
        }

    def _record_incomplete_observation(
        self,
        run: SleeveRun,
        cfg: SleeveConfig,
        session: scheduling.ExchangeSession,
        snapshot: CohortSnapshot,
        readiness: DataReadiness,
        reasons: Sequence[str],
        plan: SessionPlan | None = None,
    ) -> None:
        """Persist a ``PARTIAL`` observation.

        ``reasons`` carries only conditions the readiness result does not already
        describe (a quote-coverage gap, an execution failure). Readiness reasons are
        derived once, from the readiness result, and deduplicated — restating them
        here is what recorded ``daily_bars:stale`` twice per observation.
        """
        fallback = session.decision_utc or snapshot.captured_at
        readiness_ready, readiness_reasons, _ = summarize_readiness(readiness)
        self.evaluation_factory(cfg).record_official_observation(
            OfficialDailyObservation(
                cohort_id=run.cohort_id,
                run_id=run.run_id,
                sleeve_id=cfg.identity,
                strategy=cfg.strategy,
                strategy_hash=cfg.configuration_hash,
                session_date=session.session_date,
                status=ObservationStatus.PARTIAL,
                snapshot_ids=self._snapshot_ids(snapshot, plan, self.universe_resolver(cfg)),
                readiness_ready=readiness_ready,
                readiness_reasons=tuple(dict.fromkeys((*readiness_reasons, *reasons))),
                **_timing_fields(
                    plan,
                    fallback=fallback,
                    valuation_fallback=session.valuation_utc or fallback,
                ),
            )
        )

    @staticmethod
    def _modeled_cost(
        report: CycleReport,
        snapshot: CohortSnapshot,
        plan: SessionPlan | None,
    ) -> Decimal:
        """Adverse spread/slippage paid relative to the observed opening prints."""
        if plan is None or not plan.methodology.requires_opening_evidence:
            return Decimal(0)
        by_symbol = {
            key.strip().upper(): evidence for key, evidence in snapshot.opening_bars.items()
        }
        total = Decimal(0)
        for outcome in report.outcomes:
            if outcome.status != "FILLED" or outcome.fill_price is None:
                continue
            request = outcome.proposal.request
            evidence = by_symbol.get(request.symbol.strip().upper())
            if evidence is None:
                raise RuntimeError(f"filled {request.symbol} without persisted opening evidence")
            total += abs(outcome.fill_price - evidence.reference_price) * request.quantity
        return total

    def _official_observation(
        self,
        run: SleeveRun,
        cfg: SleeveConfig,
        session: scheduling.ExchangeSession,
        snapshot: CohortSnapshot,
        readiness: DataReadiness,
        report: CycleReport,
        engine: PaperEngine,
        plan: SessionPlan | None = None,
    ) -> OfficialDailyObservation:
        universe = self.universe_resolver(cfg)
        coverage = (
            Decimal(1)
            if not universe
            else Decimal(len([symbol for symbol in universe if symbol in snapshot.quotes]))
            / Decimal(len(universe))
        )
        turnover = sum(
            (
                abs(outcome.fill_price * outcome.proposal.request.quantity)
                for outcome in report.outcomes
                if outcome.fill_price is not None and outcome.status == "FILLED"
            ),
            Decimal(0),
        )
        total = report.valuation.total_value
        exposure = Decimal(0) if total == 0 else report.valuation.positions_value / total
        ready, reasons, _ = summarize_readiness(readiness)
        return OfficialDailyObservation(
            cohort_id=run.cohort_id,
            run_id=run.run_id,
            sleeve_id=cfg.identity,
            strategy=cfg.strategy,
            strategy_hash=cfg.configuration_hash,
            session_date=session.session_date,
            status=ObservationStatus.OFFICIAL,
            total_value=total,
            return_pct=report.valuation.total_return_pct,
            exposure=exposure,
            num_positions=len(engine.positions()),
            turnover=turnover,
            modeled_cost=self._modeled_cost(report, snapshot, plan),
            num_filled=report.num_filled,
            num_rejected=report.num_rejected,
            quote_coverage=coverage,
            snapshot_ids=self._snapshot_ids(snapshot, plan, universe),
            readiness_ready=ready,
            readiness_reasons=reasons,
            **_timing_fields(
                plan,
                fallback=session.decision_utc or report.now,
                valuation_fallback=session.valuation_utc or report.now,
            ),
        )


__all__ = [
    "TERMINAL_RUN_STATUSES",
    "CohortSnapshot",
    "MemberRunStatus",
    "SleeveRun",
    "SleeveRunConflictError",
    "SleeveRunError",
    "SleeveRunMember",
    "SleeveRunOrchestrator",
    "SleeveRunStatus",
    "SleeveRunStore",
    "SnapshotCoverage",
    "SnapshotMismatchError",
    "active_errors",
]
