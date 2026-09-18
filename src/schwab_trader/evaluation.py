"""Evaluation harness: persist agent decision cycles and track performance.

Every agent cycle is recorded - a portfolio snapshot (cash, positions value,
total value, realized/unrealized P&L, return) plus each decision and its outcome.
That gives an equity curve over time and a simple scorecard so a strategy can be
graded before it is ever trusted with real money.

Two distinct series live side by side:

- ``agent_cycles`` is the *detailed intraday* record. A sleeve may run many cycles
  in one session (see the intraday strategies), so this table is high-frequency and
  is never the basis for a matched cross-sleeve comparison on its own.
- ``official_daily_observations`` is the *one official daily metric series* per
  sleeve. Exactly one row exists per (cohort, sleeve, session) no matter how many
  intraday cycles fed it, it carries the cohort/run/sleeve lineage and data-readiness
  needed for later matched comparisons, and a session that did not produce data is
  stored with an explicit ``status`` rather than fabricated as a zero return.

This is a local SQLite store; it performs no network calls.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from schwab_trader import metrics
from schwab_trader.agent import CycleReport
from schwab_trader.data_readiness import DataReadiness

# Bumped whenever the local schema gains tables/columns. Old databases created at an
# earlier version stay readable: the migration only adds tables (never drops or
# rewrites), and PRAGMA user_version records how far a file has been upgraded.
SCHEMA_VERSION = 2


class CycleRecord(BaseModel):
    id: int
    ts: datetime
    strategy: str
    num_proposals: int
    num_filled: int
    num_rejected: int
    cash: Decimal
    positions_value: Decimal
    total_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    starting_cash: Decimal
    return_pct: Decimal


class EvalSummary(BaseModel):
    cycles: int
    trades_filled: int
    starting_cash: Decimal
    latest_value: Decimal
    total_return_pct: Decimal
    realized_pnl: Decimal
    max_drawdown_pct: Decimal
    sharpe: Decimal | None
    first_ts: datetime | None
    last_ts: datetime | None


class ObservationStatus(StrEnum):
    """Whether an official daily observation carries a real, complete result.

    ``MISSING`` and ``PARTIAL`` exist precisely so a session that produced no (or
    incomplete) data is recorded as a status rather than fabricated as a zero
    return, which would silently bias any downstream performance comparison.
    """

    OFFICIAL = "official"  # complete: a real value and return were produced
    PARTIAL = "partial"  # some inputs were missing/stale; result may be absent
    MISSING = "missing"  # the session produced no result at all


def official_observation_key(cohort_id: str, sleeve_id: str, session_date: date) -> str:
    """Stable idempotency key for one sleeve's official daily observation.

    Deliberately excludes the run id and any intraday timestamp: a session has
    exactly one official observation per (cohort, sleeve), regardless of how many
    intraday runs or cycles produced it. Re-recording the same official run is
    therefore a no-op instead of a duplicate row.
    """
    return f"{cohort_id}|{sleeve_id}|{session_date.isoformat()}"


def summarize_readiness(readiness: DataReadiness) -> tuple[bool, tuple[str, ...], dict[str, str]]:
    """Flatten a :class:`DataReadiness` into (ready, reasons, snapshot_ids).

    Reasons are limited to the *unready* requirements as ``"<kind>:<reason>"`` so
    an observation can persist why a session was only partial without embedding the
    whole readiness object. Snapshot ids are copied for later matched lineage.

    Reasons are deduplicated while preserving order. Two requirements of the same kind
    failing the same way is one condition an operator needs to know about, not two, and
    a repeated code makes the persisted record harder to read without adding anything.
    """
    reasons = tuple(
        dict.fromkeys(
            f"{req.kind.value}:{reason.value}"
            for req in readiness.unready()
            for reason in req.reasons
        )
    )
    return readiness.ready, reasons, dict(readiness.snapshot_ids)


class OfficialDailyObservation(BaseModel):
    """One official daily metric observation for a sleeve, with run lineage.

    Identity and lineage (``cohort_id``/``run_id``/``sleeve_id``/``strategy_hash``)
    plus the decision and valuation times are always required; the performance and
    exposure fields are optional so a ``MISSING``/``PARTIAL`` session can be stored
    truthfully. The ``observation_key`` is derived, not supplied, so it cannot drift
    from the identity that defines daily uniqueness.
    """

    model_config = ConfigDict(frozen=True)

    cohort_id: str
    run_id: str
    sleeve_id: str
    strategy: str
    strategy_hash: str
    session_date: date
    """The **signal** session: the one whose settled evidence produced the decision,
    and the one the run and the idempotency key are built from. Unchanged in meaning."""

    decision_time: datetime
    valuation_time: datetime
    execution_methodology: str = ""
    """Registered :mod:`schwab_trader.execution_timing` key. Empty means the
    close-marked model, which is what every observation recorded before #79 ran."""

    signal_session_date: date | None = None
    """The session the decision evidence is settled through. Equals ``session_date``;
    persisted separately so a reader never has to infer which role the date plays."""

    execution_session_date: date | None = None
    """The session the simulated fill occurred in. Differs from ``session_date`` only
    under a next-open methodology."""

    signal_time: datetime | None = None
    """Last instant of evidence the decision was allowed to use."""

    execution_time: datetime | None = None
    """Instant the simulated fill was modeled to occur."""

    status: ObservationStatus
    total_value: Decimal | None = None
    return_pct: Decimal | None = None
    benchmark_value: Decimal | None = None
    exposure: Decimal | None = None
    num_positions: int | None = None
    turnover: Decimal | None = None
    modeled_cost: Decimal | None = None
    num_filled: int = 0
    num_rejected: int = 0
    quote_coverage: Decimal | None = None
    snapshot_ids: dict[str, str] = {}
    readiness_ready: bool | None = None
    readiness_reasons: tuple[str, ...] = ()

    @property
    def observation_key(self) -> str:
        return official_observation_key(self.cohort_id, self.sleeve_id, self.session_date)

    @model_validator(mode="after")
    def _check_status_consistency(self) -> OfficialDailyObservation:
        if self.status is ObservationStatus.OFFICIAL and (
            self.total_value is None or self.return_pct is None
        ):
            raise ValueError("an official observation must carry a total value and a return")
        if self.status is ObservationStatus.MISSING and (
            self.total_value is not None or self.return_pct is not None
        ):
            raise ValueError("a missing session cannot carry a total value or return")
        return self

    @model_validator(mode="after")
    def _check_session_lineage(self) -> OfficialDailyObservation:
        """The two sessions must agree with the date the observation is keyed on.

        ``session_date`` *is* the signal session, so recording a different one would
        make the idempotency key describe a session the record does not claim. An
        execution session before the signal session would be look-ahead in reverse.
        """
        if self.signal_session_date is not None and self.signal_session_date != self.session_date:
            raise ValueError("signal_session_date must equal session_date")
        if (
            self.execution_session_date is not None
            and self.execution_session_date < self.session_date
        ):
            raise ValueError("execution_session_date must not precede the signal session")
        lineage = (
            self.signal_session_date,
            self.execution_session_date,
            self.signal_time,
            self.execution_time,
        )
        if self.execution_methodology:
            if any(value is None for value in lineage):
                raise ValueError(
                    "a separated execution methodology requires both sessions and "
                    "all four timestamps"
                )
            assert self.signal_time is not None and self.execution_time is not None
            if self.decision_time != self.signal_time:
                raise ValueError("decision_time must equal signal_time")
            if self.valuation_time != self.execution_time:
                raise ValueError("valuation_time must equal execution_time")
            if self.execution_time < self.decision_time:
                raise ValueError("execution_time must not precede decision_time")
            if self.status is ObservationStatus.OFFICIAL:
                methodology_hash = self.snapshot_ids.get("execution_methodology", "")
                if len(methodology_hash) != 64:
                    raise ValueError(
                        "an official separated-timing observation must persist its methodology hash"
                    )
        elif any(value is not None for value in lineage):
            raise ValueError("close-marked observations must leave separated timing fields unset")
        return self


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy TEXT NOT NULL,
    num_proposals INTEGER NOT NULL,
    num_filled INTEGER NOT NULL,
    num_rejected INTEGER NOT NULL,
    cash TEXT NOT NULL,
    positions_value TEXT NOT NULL,
    total_value TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    starting_cash TEXT NOT NULL,
    return_pct TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL,
    side TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    limit_price TEXT NOT NULL,
    status TEXT NOT NULL,
    fill_price TEXT,
    rationale TEXT
);
CREATE TABLE IF NOT EXISTS official_daily_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key TEXT NOT NULL UNIQUE,
    cohort_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sleeve_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,
    session_date TEXT NOT NULL,
    decision_time TEXT NOT NULL,
    valuation_time TEXT NOT NULL,
    execution_methodology TEXT NOT NULL DEFAULT '',
    signal_session_date TEXT,
    execution_session_date TEXT,
    signal_time TEXT,
    execution_time TEXT,
    status TEXT NOT NULL,
    total_value TEXT,
    return_pct TEXT,
    benchmark_value TEXT,
    exposure TEXT,
    num_positions INTEGER,
    turnover TEXT,
    modeled_cost TEXT,
    num_filled INTEGER NOT NULL,
    num_rejected INTEGER NOT NULL,
    quote_coverage TEXT,
    snapshot_ids TEXT NOT NULL,
    readiness_ready INTEGER,
    readiness_reasons TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


#: Columns added to ``official_daily_observations`` by #79, as
#: ``(column_name, DDL)``. Applied one at a time to an existing database so a store
#: created under any earlier schema converges on the current one without a rebuild.
_OBSERVATION_ADDITIONS: tuple[tuple[str, str], ...] = (
    ("execution_methodology", "execution_methodology TEXT NOT NULL DEFAULT ''"),
    ("signal_session_date", "signal_session_date TEXT"),
    ("execution_session_date", "execution_session_date TEXT"),
    ("signal_time", "signal_time TEXT"),
    ("execution_time", "execution_time TEXT"),
)


def _opt_decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _str_or_none(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _iso_or_none(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


class EvaluationStore:
    """SQLite store for agent cycle snapshots and decisions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        # Additive, idempotent migration: every statement is CREATE ... IF NOT EXISTS,
        # so a pre-v2 database (only agent_cycles/agent_decisions) keeps its rows and
        # simply gains the official-observation table. user_version records progress.
        with self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            conn.executescript(_SCHEMA)
            # Migration (#79): a database created before the execution-timing columns
            # existed keeps every row untouched and simply gains nullable columns. The
            # NULLs are truthful — those observations were recorded under the
            # close-marked model, which is exactly what an absent methodology means.
            existing = conn.execute("PRAGMA table_info(official_daily_observations)")
            columns = {row["name"] for row in existing}
            for name, ddl in _OBSERVATION_ADDITIONS:
                if name not in columns:
                    conn.execute(f"ALTER TABLE official_daily_observations ADD COLUMN {ddl}")
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def record_cycle(self, report: CycleReport) -> int:
        """Persist a cycle snapshot and its decisions; return the cycle id."""
        valuation = report.valuation
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO agent_cycles (
                    ts, strategy, num_proposals, num_filled, num_rejected, cash,
                    positions_value, total_value, realized_pnl, unrealized_pnl,
                    starting_cash, return_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.now.isoformat(),
                    report.strategy,
                    len(report.outcomes),
                    report.num_filled,
                    report.num_rejected,
                    str(valuation.cash),
                    str(valuation.positions_value),
                    str(valuation.total_value),
                    str(valuation.realized_pnl),
                    str(valuation.unrealized_pnl),
                    str(valuation.starting_cash),
                    str(valuation.total_return_pct),
                ),
            )
            cycle_id = int(cursor.lastrowid or 0)
            for outcome in report.outcomes:
                request = outcome.proposal.request
                conn.execute(
                    """
                    INSERT INTO agent_decisions (
                        cycle_id, side, symbol, quantity, limit_price, status,
                        fill_price, rationale
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cycle_id,
                        request.side.value,
                        request.symbol,
                        request.quantity,
                        str(request.limit_price),
                        outcome.status,
                        str(outcome.fill_price) if outcome.fill_price is not None else None,
                        outcome.proposal.rationale,
                    ),
                )
        return cycle_id

    def recent_cycles(self, limit: int = 20) -> list[CycleRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_cycles ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_cycle(row) for row in rows]

    def _row_to_cycle(self, row: sqlite3.Row) -> CycleRecord:
        return CycleRecord(
            id=row["id"],
            ts=datetime.fromisoformat(row["ts"]),
            strategy=row["strategy"],
            num_proposals=row["num_proposals"],
            num_filled=row["num_filled"],
            num_rejected=row["num_rejected"],
            cash=Decimal(row["cash"]),
            positions_value=Decimal(row["positions_value"]),
            total_value=Decimal(row["total_value"]),
            realized_pnl=Decimal(row["realized_pnl"]),
            unrealized_pnl=Decimal(row["unrealized_pnl"]),
            starting_cash=Decimal(row["starting_cash"]),
            return_pct=Decimal(row["return_pct"]),
        )

    def equity_curve(self, limit: int = 200) -> list[tuple[datetime, Decimal]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, total_value FROM agent_cycles ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        points = [(datetime.fromisoformat(row["ts"]), Decimal(row["total_value"])) for row in rows]
        points.reverse()
        return points

    def summary(self) -> EvalSummary:
        with self._connect() as conn:
            count_row = conn.execute("SELECT COUNT(*) AS n FROM agent_cycles").fetchone()
            cycles = int(count_row["n"])
            if cycles == 0:
                return EvalSummary(
                    cycles=0,
                    trades_filled=0,
                    starting_cash=Decimal(0),
                    latest_value=Decimal(0),
                    total_return_pct=Decimal(0),
                    realized_pnl=Decimal(0),
                    max_drawdown_pct=Decimal(0),
                    sharpe=None,
                    first_ts=None,
                    last_ts=None,
                )
            latest = conn.execute(
                "SELECT total_value, starting_cash, realized_pnl, return_pct, ts "
                "FROM agent_cycles ORDER BY id DESC LIMIT 1"
            ).fetchone()
            first_ts = conn.execute(
                "SELECT ts FROM agent_cycles ORDER BY id ASC LIMIT 1"
            ).fetchone()["ts"]
            trades = int(
                conn.execute(
                    "SELECT COALESCE(SUM(num_filled), 0) AS n FROM agent_cycles"
                ).fetchone()["n"]
            )
        # Risk metrics from the recorded equity curve (starting cash + each cycle).
        curve = self.equity_curve(limit=10_000)
        values = [Decimal(latest["starting_cash"]), *(v for _, v in curve)]
        max_dd = metrics.max_drawdown_pct(values)
        sharpe = metrics.sharpe(metrics.simple_returns([v for _, v in curve]))
        return EvalSummary(
            cycles=cycles,
            trades_filled=trades,
            starting_cash=Decimal(latest["starting_cash"]),
            latest_value=Decimal(latest["total_value"]),
            total_return_pct=Decimal(latest["return_pct"]),
            realized_pnl=Decimal(latest["realized_pnl"]),
            max_drawdown_pct=max_dd,
            sharpe=Decimal(str(round(sharpe, 2))) if sharpe is not None else None,
            first_ts=datetime.fromisoformat(first_ts),
            last_ts=datetime.fromisoformat(latest["ts"]),
        )

    # ---- Official daily observations (the one daily metric series per sleeve) ----

    def record_official_observation(self, obs: OfficialDailyObservation) -> int:
        """Persist one official daily observation idempotently; return its row id.

        Keyed on ``observation_key`` (cohort, sleeve, session), so re-recording the
        same official run - or promoting a later intraday cycle on the same day - is
        a no-op that returns the existing row id rather than a duplicate.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO official_daily_observations (
                    observation_key, cohort_id, run_id, sleeve_id, strategy,
                    strategy_hash, session_date, decision_time, valuation_time,
                    execution_methodology, signal_session_date, execution_session_date,
                    signal_time, execution_time, status,
                    total_value, return_pct, benchmark_value, exposure, num_positions,
                    turnover, modeled_cost, num_filled, num_rejected, quote_coverage,
                    snapshot_ids, readiness_ready, readiness_reasons, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obs.observation_key,
                    obs.cohort_id,
                    obs.run_id,
                    obs.sleeve_id,
                    obs.strategy,
                    obs.strategy_hash,
                    obs.session_date.isoformat(),
                    obs.decision_time.isoformat(),
                    obs.valuation_time.isoformat(),
                    obs.execution_methodology,
                    _iso_or_none(obs.signal_session_date),
                    _iso_or_none(obs.execution_session_date),
                    _iso_or_none(obs.signal_time),
                    _iso_or_none(obs.execution_time),
                    obs.status.value,
                    _str_or_none(obs.total_value),
                    _str_or_none(obs.return_pct),
                    _str_or_none(obs.benchmark_value),
                    _str_or_none(obs.exposure),
                    obs.num_positions,
                    _str_or_none(obs.turnover),
                    _str_or_none(obs.modeled_cost),
                    obs.num_filled,
                    obs.num_rejected,
                    _str_or_none(obs.quote_coverage),
                    json.dumps(obs.snapshot_ids, sort_keys=True),
                    None if obs.readiness_ready is None else int(obs.readiness_ready),
                    json.dumps(list(obs.readiness_reasons)),
                    datetime.now(UTC).isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT id FROM official_daily_observations WHERE observation_key = ?",
                (obs.observation_key,),
            ).fetchone()
        return int(row["id"])

    def official_observations(self, limit: int = 200) -> list[OfficialDailyObservation]:
        """Return official daily observations in session order (oldest first)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM official_daily_observations "
                "ORDER BY session_date ASC, id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_official(row) for row in rows]

    def official_equity_curve(self, limit: int = 10_000) -> list[tuple[date, Decimal]]:
        """Chronological (date, value) points for sessions that produced a value.

        Sessions stored as MISSING/PARTIAL without a value are skipped rather than
        contributing a fabricated zero, so a matched comparison never sees a phantom
        flat day.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_date, total_value FROM official_daily_observations "
                "WHERE total_value IS NOT NULL ORDER BY session_date ASC, id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            (date.fromisoformat(row["session_date"]), Decimal(row["total_value"])) for row in rows
        ]

    def _row_to_official(self, row: sqlite3.Row) -> OfficialDailyObservation:
        readiness_ready = row["readiness_ready"]
        columns = row.keys()

        def optional(name: str) -> str | None:
            # A store whose file predates #79 and was opened read-only elsewhere can
            # still lack these columns; absent and NULL both mean the same thing.
            return row[name] if name in columns else None

        signal_session = optional("signal_session_date")
        execution_session = optional("execution_session_date")
        signal_time = optional("signal_time")
        execution_time = optional("execution_time")
        return OfficialDailyObservation(
            cohort_id=row["cohort_id"],
            run_id=row["run_id"],
            sleeve_id=row["sleeve_id"],
            strategy=row["strategy"],
            strategy_hash=row["strategy_hash"],
            session_date=date.fromisoformat(row["session_date"]),
            decision_time=datetime.fromisoformat(row["decision_time"]),
            valuation_time=datetime.fromisoformat(row["valuation_time"]),
            execution_methodology=optional("execution_methodology") or "",
            signal_session_date=(
                None if signal_session is None else date.fromisoformat(signal_session)
            ),
            execution_session_date=(
                None if execution_session is None else date.fromisoformat(execution_session)
            ),
            signal_time=(None if signal_time is None else datetime.fromisoformat(signal_time)),
            execution_time=(
                None if execution_time is None else datetime.fromisoformat(execution_time)
            ),
            status=ObservationStatus(row["status"]),
            total_value=_opt_decimal(row["total_value"]),
            return_pct=_opt_decimal(row["return_pct"]),
            benchmark_value=_opt_decimal(row["benchmark_value"]),
            exposure=_opt_decimal(row["exposure"]),
            num_positions=row["num_positions"],
            turnover=_opt_decimal(row["turnover"]),
            modeled_cost=_opt_decimal(row["modeled_cost"]),
            num_filled=row["num_filled"],
            num_rejected=row["num_rejected"],
            quote_coverage=_opt_decimal(row["quote_coverage"]),
            snapshot_ids=json.loads(row["snapshot_ids"]),
            readiness_ready=None if readiness_ready is None else bool(readiness_ready),
            readiness_reasons=tuple(json.loads(row["readiness_reasons"])),
        )
