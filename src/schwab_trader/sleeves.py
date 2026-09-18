"""Named paper sleeves for running strategies in parallel and comparing them.

Each sleeve is a fully isolated experiment: its own :class:`~schwab_trader.paper.PaperEngine`
(cash + positions) and its own :class:`~schwab_trader.evaluation.EvaluationStore`
(cycle history), plus a stored config saying which strategy it runs. Running a
cycle on every sleeve against the same quotes, then comparing their equity curves,
is a clean head-to-head test of strategies - and the seed of an autonomous system
that lets strategies compete and keeps the winner.

This module owns the registry and the on-disk layout only; it performs no network
calls. The registry is SQLite; each sleeve's data lives under ``<dir>/<name>/``.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from schwab_trader.experiments import StrategyDefinition
from schwab_trader.paper import PaperEngine
from schwab_trader.storage.identity import sleeve_scope, stable_sleeve_id

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class SleeveExists(Exception):
    """Raised when creating a sleeve whose name is already registered."""


class SleeveNameError(Exception):
    """Raised when a sleeve name is not filesystem-safe."""


class SleeveConfig(BaseModel):
    sleeve_id: str = ""
    name: str
    original_name: str = ""
    namespace_id: str = "local-sqlite"
    strategy: str
    universe: list[str]  # empty = fall back to the configured/default universe
    starting_cash: Decimal
    max_positions: int
    max_position_fraction: Decimal
    created_at: datetime
    settlement_t1: bool = False  # model T+1 settled-cash (new sleeves opt in)
    leverage: Decimal = Decimal(1)  # 1 = cash account; 2 = Reg T margin (buying power)
    factor: str = ""  # for the 'fundamental' strategy: which factor to rank by
    # Versioned reproducibility identity (#25/#29). Legacy sleeves predate this and
    # leave ``definition`` unset; they are labeled non-reproducible rather than faked.
    definition: StrategyDefinition | None = None
    cohort_id: str = ""  # experiment cohort this sleeve belongs to ('' = unassigned)
    configuration_hash: str = ""  # SHA-256 of the definition; '' when no definition
    decision_frequency: str = ""  # official cadence metadata, denormalized for queries
    decision_time: str = ""  # official cadence metadata (ISO local time)
    execution_methodology: str = ""
    """Registered :mod:`schwab_trader.execution_timing` key, e.g.
    ``signal-t-close-execute-t1-open/v1``. Empty means the close-marked model every
    sleeve ran before that module existed, so existing rows keep their exact meaning.

    Deliberately separate from ``settlement_t1``, which models settled *cash* and is
    unrelated to when a decision becomes a fill."""

    @property
    def reproducible(self) -> bool:
        """True when a complete, versioned StrategyDefinition can be reconstructed.

        A legacy record whose full parameters were never captured is labeled
        non-reproducible instead of being silently treated as reproducible.
        """
        return self.definition is not None

    @property
    def identity(self) -> str:
        """Stable persistence identity, falling back for pre-identity fixtures."""
        # Local SQLite keeps its historical name-keyed paper/evaluation layout.
        # PostgreSQL configs carry a non-local namespace and use ``sleeve_id``.
        if self.namespace_id == "local-sqlite":
            return self.name
        return self.sleeve_id or self.name


def resolve_cohort_start(
    store: SleeveStore,
    cohort_id: str,
    members: Sequence[SleeveConfig],
) -> date | None:
    """When the cohort was first owed an official run.

    Precedence matters, and this is the *single* place it is decided. The persisted
    ``start_session`` is authoritative and exists whether or not that first run ever
    happened. Falling back to the earliest member's creation date still predates any
    run, so a first session the scheduler missed entirely is caught.

    Deriving the boundary from recorded runs or observations would not catch it,
    because a session that never ran leaves nothing to derive from - and a freshly
    created cohort, which has no runs at all, would report having no start session
    even though its start is sitting in the database.
    """
    persisted = store.cohort_start_session(cohort_id)
    if persisted is not None:
        return persisted
    created = [config.created_at.date() for config in members]
    return min(created) if created else None


class SleeveStore:
    """SQLite registry of sleeves plus their on-disk paper/eval store layout."""

    def __init__(self, sleeves_dir: Path) -> None:
        self.dir = sleeves_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.dir / "registry.sqlite3"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.registry_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sleeves (
                    name TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    universe_csv TEXT NOT NULL,
                    starting_cash TEXT NOT NULL,
                    max_positions INTEGER NOT NULL,
                    max_position_fraction TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Migration: add newer columns to older registries (safe defaults).
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sleeves)")}
            if "settlement_t1" not in columns:
                conn.execute(
                    "ALTER TABLE sleeves ADD COLUMN settlement_t1 INTEGER NOT NULL DEFAULT 0"
                )
            if "leverage" not in columns:
                conn.execute("ALTER TABLE sleeves ADD COLUMN leverage TEXT NOT NULL DEFAULT '1'")
            if "factor" not in columns:
                conn.execute("ALTER TABLE sleeves ADD COLUMN factor TEXT NOT NULL DEFAULT ''")
            # Migration (#29): persist the versioned strategy definition and cohort
            # identity. ``definition_json`` is nullable so legacy rows stay readable and
            # are labeled non-reproducible; the denormalized columns fail closed to ''.
            if "definition_json" not in columns:
                conn.execute("ALTER TABLE sleeves ADD COLUMN definition_json TEXT")
            if "configuration_hash" not in columns:
                conn.execute(
                    "ALTER TABLE sleeves ADD COLUMN configuration_hash TEXT NOT NULL DEFAULT ''"
                )
            if "cohort_id" not in columns:
                conn.execute("ALTER TABLE sleeves ADD COLUMN cohort_id TEXT NOT NULL DEFAULT ''")
            if "decision_frequency" not in columns:
                conn.execute(
                    "ALTER TABLE sleeves ADD COLUMN decision_frequency TEXT NOT NULL DEFAULT ''"
                )
            if "decision_time" not in columns:
                conn.execute(
                    "ALTER TABLE sleeves ADD COLUMN decision_time TEXT NOT NULL DEFAULT ''"
                )
            if "sleeve_id" not in columns:
                conn.execute("ALTER TABLE sleeves ADD COLUMN sleeve_id TEXT")
            # Migration (#79): which execution-timing methodology the sleeve runs. The
            # '' default is the close-marked model, which is what every existing row
            # actually ran, so no historical sleeve changes meaning.
            if "execution_methodology" not in columns:
                conn.execute(
                    "ALTER TABLE sleeves ADD COLUMN execution_methodology TEXT NOT NULL DEFAULT ''"
                )
            rows = conn.execute("SELECT name, cohort_id, sleeve_id FROM sleeves").fetchall()
            for row in rows:
                if row["sleeve_id"]:
                    continue
                scope = sleeve_scope(
                    namespace_id="local-sqlite",
                    cohort_id=row["cohort_id"] or None,
                )
                identity = stable_sleeve_id(
                    source_identity="local-sqlite-registry",
                    scope_key=scope,
                    name=row["name"],
                )
                conn.execute(
                    "UPDATE sleeves SET sleeve_id = ? WHERE name = ?",
                    (identity, row["name"]),
                )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sleeves_sleeve_id ON sleeves (sleeve_id)"
            )

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _NAME_RE.match(name):
            raise SleeveNameError(
                f"Invalid sleeve name '{name}'. Use letters, digits, '-' or '_' (max 40)."
            )

    def paper_path(self, name: str) -> Path:
        return self.dir / name / "paper.sqlite3"

    def eval_path(self, name: str) -> Path:
        return self.dir / name / "eval.sqlite3"

    def create(
        self,
        name: str,
        *,
        strategy: str,
        universe: list[str],
        starting_cash: Decimal,
        max_positions: int,
        max_position_fraction: Decimal,
        settlement_t1: bool = False,
        leverage: Decimal = Decimal(1),
        factor: str = "",
        definition: StrategyDefinition | None = None,
        cohort_id: str = "",
        execution_methodology: str = "",
    ) -> SleeveConfig:
        """Register a sleeve and initialize its paper engine at ``starting_cash``.

        When ``definition`` is supplied it is persisted verbatim so the exact
        :class:`~schwab_trader.experiments.StrategyDefinition` can be reconstructed
        later; its configuration hash and cadence are denormalized for querying.
        """
        self._validate_name(name)
        if self.get(name) is not None:
            raise SleeveExists(f"Sleeve '{name}' already exists.")

        normalized_cohort = cohort_id.strip()
        scope = sleeve_scope(
            namespace_id="local-sqlite",
            cohort_id=normalized_cohort or None,
        )
        config = SleeveConfig(
            sleeve_id=stable_sleeve_id(
                source_identity="local-sqlite-registry",
                scope_key=scope,
                name=name,
            ),
            name=name,
            original_name=name,
            strategy=strategy,
            universe=[s.strip().upper() for s in universe if s.strip()],
            starting_cash=starting_cash,
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
            created_at=datetime.now(UTC),
            settlement_t1=settlement_t1,
            leverage=leverage,
            factor=factor,
            definition=definition,
            cohort_id=normalized_cohort,
            configuration_hash=definition.configuration_hash if definition else "",
            decision_frequency=definition.decision_frequency if definition else "",
            decision_time=definition.decision_time.isoformat() if definition else "",
            execution_methodology=execution_methodology.strip(),
        )
        definition_json = config.definition.model_dump_json() if config.definition else None
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sleeves (name, strategy, universe_csv, starting_cash, "
                "max_positions, max_position_fraction, created_at, settlement_t1, leverage, "
                "factor, definition_json, configuration_hash, cohort_id, decision_frequency, "
                "decision_time, sleeve_id, execution_methodology) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    config.name,
                    config.strategy,
                    ",".join(config.universe),
                    str(config.starting_cash),
                    config.max_positions,
                    str(config.max_position_fraction),
                    config.created_at.isoformat(),
                    int(config.settlement_t1),
                    str(config.leverage),
                    config.factor,
                    definition_json,
                    config.configuration_hash,
                    config.cohort_id,
                    config.decision_frequency,
                    config.decision_time,
                    config.sleeve_id,
                    config.execution_methodology,
                ),
            )
        # Initialize the paper engine so the account exists at the chosen cash.
        PaperEngine(
            self.paper_path(name),
            starting_cash=starting_cash,
            settle_t1=settlement_t1,
            leverage=leverage,
        )
        return config

    def get(self, name: str) -> SleeveConfig | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sleeves WHERE name = ?", (name,)).fetchone()
        return self._row_to_config(row) if row is not None else None

    def resolve(self, reference: str, *, cohort_id: str | None = None) -> SleeveConfig | None:
        """Resolve a stable id or local unique name, optionally checking the cohort."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sleeves WHERE sleeve_id = ?",
                (reference,),
            ).fetchone()
            if row is None:
                if cohort_id is None:
                    row = conn.execute(
                        "SELECT * FROM sleeves WHERE name = ?",
                        (reference,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM sleeves WHERE name = ? AND cohort_id = ?",
                        (reference, cohort_id),
                    ).fetchone()
        return self._row_to_config(row) if row is not None else None

    def list(self) -> list[SleeveConfig]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sleeves ORDER BY created_at, name").fetchall()
        return [self._row_to_config(row) for row in rows]

    def cohort_start_session(self, cohort_id: str) -> date | None:
        """The cohort's persisted first official session, when one is recorded.

        The local layout stores sleeves, not a cohort manifest, so there is nothing
        authoritative to return. Callers must fall back rather than infer a start
        from recorded runs, which would hide a first session that never ran.
        """
        del cohort_id
        return None

    def remove(self, name: str, *, delete_data: bool = True) -> bool:
        """Unregister a sleeve. Returns True if it existed."""
        config = self.resolve(name)
        if config is None:
            return False
        with self._connect() as conn:
            conn.execute("DELETE FROM sleeves WHERE name = ?", (config.name,))
        if delete_data:
            shutil.rmtree(self.dir / config.name, ignore_errors=True)
        return True

    def _row_to_config(self, row: sqlite3.Row) -> SleeveConfig:
        universe_csv = row["universe_csv"]
        columns = row.keys()
        definition_json = row["definition_json"]
        definition = (
            StrategyDefinition.model_validate_json(definition_json) if definition_json else None
        )
        return SleeveConfig(
            sleeve_id=row["sleeve_id"] or "",
            name=row["name"],
            original_name=row["name"],
            strategy=row["strategy"],
            universe=[s for s in universe_csv.split(",") if s],
            starting_cash=Decimal(row["starting_cash"]),
            max_positions=row["max_positions"],
            max_position_fraction=Decimal(row["max_position_fraction"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            settlement_t1=bool(row["settlement_t1"]),
            leverage=Decimal(row["leverage"]),
            factor=row["factor"],
            definition=definition,
            cohort_id=row["cohort_id"],
            configuration_hash=row["configuration_hash"],
            decision_frequency=row["decision_frequency"],
            decision_time=row["decision_time"],
            # Fixture rows built by older tests may predate the column entirely; an
            # absent column means the close-marked model, same as an empty value.
            execution_methodology=(
                row["execution_methodology"] or "" if "execution_methodology" in columns else ""
            ),
        )
