"""Transactional, resumable import from immutable SQLite backup snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, inspect, select
from sqlalchemy.orm import Session

from schwab_trader.evaluation import official_observation_key
from schwab_trader.storage.database import Database
from schwab_trader.storage.identity import (
    LEGACY_NAMESPACE_ID,
    LEGACY_NAMESPACE_NAME,
    sleeve_scope,
    stable_id,
    stable_sleeve_id,
    standalone_paper_sleeve_id,
)
from schwab_trader.storage.inventory import InventoryReport
from schwab_trader.storage.schema import (
    Base,
    Cohort,
    CohortMember,
    CohortRun,
    CohortRunMember,
    DailyPriceBar,
    EvaluationCycle,
    EvaluationDecision,
    IntradayPriceBar,
    MigrationRun,
    MigrationSource,
    MigrationTableResult,
    OfficialDailyObservation,
    PaperAccount,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PaperUnsettledCash,
    PromotionVerdict,
    ResearchStrategySpec,
    SecFact,
    Sleeve,
    StorageNamespace,
    UsageEvent,
)
from schwab_trader.storage.snapshots import SnapshotSet, verify_snapshot_set

_EMPTY_JSON_HASH = hashlib.sha256(b"{}").hexdigest()


class MigrationConflictError(RuntimeError):
    """Destination state conflicts with immutable source identity."""


class MigrationVerificationError(RuntimeError):
    """Post-copy verification found a count, checksum, or invariant mismatch."""


@dataclass(frozen=True)
class ImportedTable:
    source_table: str
    destination_table: str
    row_count: int
    checksum: str


@dataclass(frozen=True)
class MigrationOutcome:
    migration_id: str
    status: str
    sources_completed: int
    verification_status: str


@dataclass(frozen=True)
class MigrationPreflight:
    migration_id: str
    eligible_sources: int
    source_tables: int
    source_rows: int
    destination_schema_ready: bool
    resumable: bool


class _Digest:
    def __init__(self) -> None:
        self.count = 0
        self._hash = hashlib.sha256()

    def add(self, payload: dict[str, object]) -> None:
        self._hash.update(
            json.dumps(
                _canonical(payload),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        )
        self._hash.update(b"\n")
        self.count += 1

    @property
    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _canonical(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        if value == 0:
            return {"decimal": "0"}
        return {"decimal": format(value.normalize(), "f")}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return {"legacy-naive": value.isoformat()}
        return {"timestamp": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return str(value)


def _json(value: object, *, default: object) -> object:
    if value is None or value == "":
        return default
    parsed = json.loads(str(value))
    return parsed


def _dict_json(value: object) -> dict[str, Any]:
    parsed = _json(value, default={})
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object in migration source")
    return parsed


def _list_json(value: object) -> list[Any]:
    parsed = _json(value, default=[])
    if not isinstance(parsed, list):
        raise ValueError("expected a JSON array in migration source")
    return parsed


def _value(row: sqlite3.Row, name: str, default: object = None) -> object:
    # sqlite3.Row membership checks values, not column names; keys() is required.
    return row[name] if name in row.keys() else default  # noqa: SIM118


def _timestamp(
    value: object,
    *,
    require_aware: bool = False,
) -> tuple[datetime | None, str | None]:
    if value is None or value == "":
        return None, None
    raw = str(value)
    stamp = datetime.fromisoformat(raw)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        if require_aware:
            raise ValueError("source timestamp requires an explicit timezone")
        return None, raw
    return stamp.astimezone(UTC), raw


def _date(value: object) -> date:
    return date.fromisoformat(str(value))


def _decimal(value: object | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _source_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _rows(conn: sqlite3.Connection, table: str) -> Iterator[sqlite3.Row]:
    primary_key = [
        str(row["name"])
        for row in conn.execute(f'PRAGMA table_info("{table}")')
        if int(row["pk"]) > 0
    ]
    order_by = ", ".join(f'"{column}"' for column in primary_key) or "rowid"
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order_by}')
    while batch := cursor.fetchmany(4096):
        yield from batch


def _ensure_namespace(
    session: Session,
    namespace_id: str,
    name: str,
    kind: str,
    *,
    source_identity: str | None,
) -> StorageNamespace:
    row = session.get(StorageNamespace, namespace_id)
    if row is None:
        row = StorageNamespace(
            namespace_id=namespace_id,
            name=name,
            kind=kind,
            source_identity=source_identity,
            created_at=datetime.now(UTC),
            immutable_metadata={},
        )
        session.add(row)
        session.flush()
        return row
    if (
        row.name != name
        or row.kind != kind
        or row.source_identity != source_identity
    ):
        raise MigrationConflictError("storage namespace conflicts with destination")
    return row


def _ensure_legacy_namespace(session: Session) -> StorageNamespace:
    return _ensure_namespace(
        session,
        LEGACY_NAMESPACE_ID,
        LEGACY_NAMESPACE_NAME,
        "legacy-import",
        source_identity="authoritative-laptop-sqlite",
    )


def _ensure_cohort(
    session: Session,
    cohort_id: str,
    *,
    source_path: str | None,
) -> Cohort:
    existing = session.get(Cohort, cohort_id)
    if existing is not None:
        return existing
    namespace_id = stable_id("namespace", "official-cohorts")
    _ensure_namespace(
        session,
        namespace_id,
        "official-cohorts",
        "cohort",
        source_identity=None,
    )
    cohort = Cohort(
        cohort_id=cohort_id,
        namespace_id=namespace_id,
        name=cohort_id,
        created_at=None,
        start_session=None,
        status="legacy-import-unmanifested",
        starting_cash_per_sleeve=None,
        settlement_model=None,
        leverage=None,
        benchmark_sleeve_name=None,
        decision_schedule=None,
        cost_model_id=None,
        manifest_json={},
        manifest_hash=_EMPTY_JSON_HASH,
        source_path=source_path,
    )
    session.add(cohort)
    session.flush()
    return cohort


def _manifest_values(payload: dict[str, Any], source_path: str) -> dict[str, object]:
    cohort_raw = payload.get("cohort")
    if not isinstance(cohort_raw, dict):
        raise ValueError("cohort manifest is missing cohort metadata")
    cohort_id = str(cohort_raw.get("cohort_id") or "").strip()
    if not cohort_id:
        raise ValueError("cohort manifest is missing cohort_id")
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    created, _ = _timestamp(cohort_raw.get("created_at"))
    return {
        "cohort_id": cohort_id,
        "name": str(cohort_raw.get("name") or cohort_id),
        "created_at": created,
        "start_session": (
            _date(cohort_raw["start_session"])
            if cohort_raw.get("start_session") is not None
            else None
        ),
        "status": str(cohort_raw.get("status") or "active"),
        "starting_cash_per_sleeve": _decimal(
            cohort_raw.get("starting_cash_per_sleeve")
        ),
        "settlement_model": (
            str(cohort_raw["settlement_model"])
            if cohort_raw.get("settlement_model") is not None
            else None
        ),
        "leverage": _decimal(cohort_raw.get("leverage")),
        "benchmark_sleeve_name": (
            str(cohort_raw["benchmark_sleeve"])
            if cohort_raw.get("benchmark_sleeve") is not None
            else None
        ),
        "decision_schedule": (
            str(cohort_raw["decision_schedule"])
            if cohort_raw.get("decision_schedule") is not None
            else None
        ),
        "cost_model_id": (
            str(cohort_raw["cost_model_id"])
            if cohort_raw.get("cost_model_id") is not None
            else None
        ),
        "manifest_json": payload,
        "manifest_hash": hashlib.sha256(encoded.encode()).hexdigest(),
        "source_path": source_path,
    }


def _import_manifest(
    session: Session,
    path: Path,
    source_path: str,
) -> list[ImportedTable]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("cohort manifest must be a JSON object")
    values = _manifest_values(payload, source_path)
    cohort_id = str(values["cohort_id"])
    namespace_id = stable_id("namespace", "official-cohorts")
    _ensure_namespace(
        session,
        namespace_id,
        "official-cohorts",
        "cohort",
        source_identity=None,
    )
    existing = session.get(Cohort, cohort_id)
    if existing is None:
        session.add(Cohort(namespace_id=namespace_id, **values))
    elif existing.manifest_hash == _EMPTY_JSON_HASH:
        for field, value in values.items():
            setattr(existing, field, value)
        existing.namespace_id = namespace_id
    elif existing.manifest_hash != values["manifest_hash"]:
        raise MigrationConflictError("cohort manifest conflicts with destination")
    digest = _Digest()
    digest.add(values)
    return [
        ImportedTable(
            source_table="cohort_manifest",
            destination_table="cohorts",
            row_count=digest.count,
            checksum=digest.hexdigest,
        )
    ]


def _registry_source_identity(name: str) -> str:
    return f"data/sleeves/registry.sqlite3:{name}"


def _sleeve_values(row: sqlite3.Row, source_path: str) -> dict[str, object]:
    name = str(row["name"])
    cohort_id = str(_value(row, "cohort_id", "") or "").strip()
    namespace_id = (
        stable_id("namespace", "official-cohorts")
        if cohort_id
        else LEGACY_NAMESPACE_ID
    )
    scope_key = sleeve_scope(
        namespace_id=namespace_id,
        cohort_id=cohort_id or None,
    )
    source_identity = _registry_source_identity(name)
    created_at, source_created_at = _timestamp(row["created_at"])
    definition_raw = _value(row, "definition_json")
    definition = (
        _dict_json(definition_raw)
        if definition_raw is not None and definition_raw != ""
        else None
    )
    return {
        "sleeve_id": stable_sleeve_id(
            source_identity=source_identity,
            scope_key=scope_key,
            name=name,
        ),
        "namespace_id": namespace_id,
        "cohort_id": cohort_id or None,
        "scope_key": scope_key,
        "name": name,
        "original_name": name,
        "source_identity": source_identity,
        "source_sleeve_id": (
            str(_value(row, "sleeve_id"))
            if _value(row, "sleeve_id") not in {None, ""}
            else None
        ),
        "source_path": source_path,
        "strategy": str(row["strategy"]),
        "universe": [
            symbol for symbol in str(row["universe_csv"]).split(",") if symbol
        ],
        "starting_cash": Decimal(str(row["starting_cash"])),
        "max_positions": int(row["max_positions"]),
        "max_position_fraction": Decimal(str(row["max_position_fraction"])),
        "settlement_t1": bool(_value(row, "settlement_t1", 0)),
        "leverage": Decimal(str(_value(row, "leverage", "1"))),
        "factor": str(_value(row, "factor", "") or ""),
        "strategy_definition": definition,
        "configuration_hash": str(
            _value(row, "configuration_hash", "") or ""
        ),
        "decision_frequency": str(
            _value(row, "decision_frequency", "") or ""
        ),
        "decision_time": str(_value(row, "decision_time", "") or ""),
        "execution_methodology": str(
            _value(row, "execution_methodology", "") or ""
        ),
        "created_at": created_at,
        "source_created_at": source_created_at,
    }


def _same(existing: object, values: dict[str, object]) -> bool:
    return all(
        _canonical(getattr(existing, field)) == _canonical(value)
        for field, value in values.items()
    )


def _ensure_sleeve_row(
    session: Session,
    values: dict[str, object],
) -> Sleeve:
    sleeve_id = str(values["sleeve_id"])
    existing = session.get(Sleeve, sleeve_id)
    if existing is not None:
        if not _same(existing, values):
            raise MigrationConflictError("sleeve identity conflicts with destination")
        return existing
    scoped = session.scalar(
        select(Sleeve).where(
            Sleeve.scope_key == values["scope_key"],
            Sleeve.name == values["name"],
        )
    )
    if scoped is not None:
        raise MigrationConflictError("sleeve name conflicts within migration scope")
    sleeve = Sleeve(**values)
    session.add(sleeve)
    session.flush()
    return sleeve


def _ensure_membership(
    session: Session,
    cohort_id: str,
    sleeve_id: str,
    *,
    configuration_hash: str,
    source_path: str | None,
    ordinal: int | None = None,
) -> CohortMember:
    existing = session.get(CohortMember, (cohort_id, sleeve_id))
    if existing is not None:
        if existing.configuration_hash != configuration_hash:
            raise MigrationConflictError("cohort membership hash conflicts with destination")
        return existing
    if ordinal is None:
        maximum = session.scalar(
            select(func.max(CohortMember.ordinal)).where(
                CohortMember.cohort_id == cohort_id
            )
        )
        ordinal = int(maximum) + 1 if maximum is not None else 0
    member = CohortMember(
        cohort_id=cohort_id,
        sleeve_id=sleeve_id,
        ordinal=ordinal,
        role=None,
        configuration_hash=configuration_hash,
        source_path=source_path,
    )
    session.add(member)
    session.flush()
    return member


def _import_registry(
    session: Session,
    conn: sqlite3.Connection,
    source_path: str,
) -> list[ImportedTable]:
    _ensure_legacy_namespace(session)
    digest = _Digest()
    for source_row in _rows(conn, "sleeves"):
        values = _sleeve_values(source_row, source_path)
        cohort_id = values["cohort_id"]
        if isinstance(cohort_id, str):
            _ensure_cohort(session, cohort_id, source_path=source_path)
        sleeve = _ensure_sleeve_row(session, values)
        if sleeve.cohort_id:
            _ensure_membership(
                session,
                sleeve.cohort_id,
                sleeve.sleeve_id,
                configuration_hash=sleeve.configuration_hash,
                source_path=source_path,
            )
        digest.add(values)
    return [
        ImportedTable(
            source_table="sleeves",
            destination_table="sleeves",
            row_count=digest.count,
            checksum=digest.hexdigest,
        )
    ]


def _source_sleeve(
    session: Session,
    source_path: str,
    *,
    starting_cash: Decimal,
    strategy: str = "legacy-import",
) -> Sleeve:
    _ensure_legacy_namespace(session)
    if source_path in {"data/paper.sqlite3", "data/agent_eval.sqlite3"}:
        name = "standalone-paper"
        source_identity = "data/paper.sqlite3"
        sleeve_id = standalone_paper_sleeve_id()
    else:
        parts = Path(source_path).parts
        if len(parts) < 4:
            raise ValueError("sleeve source path has no sleeve directory")
        name = parts[-2]
        source_identity = _registry_source_identity(name)
        candidates = list(
            session.scalars(
                select(Sleeve).where(Sleeve.source_identity == source_identity)
            )
        )
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise MigrationConflictError("source sleeve identity is ambiguous")
        scope_key = sleeve_scope(
            namespace_id=LEGACY_NAMESPACE_ID,
            cohort_id=None,
        )
        sleeve_id = stable_sleeve_id(
            source_identity=source_identity,
            scope_key=scope_key,
            name=name,
        )
    existing = session.get(Sleeve, sleeve_id)
    if existing is not None:
        return existing
    scope_key = sleeve_scope(namespace_id=LEGACY_NAMESPACE_ID, cohort_id=None)
    return _ensure_sleeve_row(
        session,
        {
            "sleeve_id": sleeve_id,
            "namespace_id": LEGACY_NAMESPACE_ID,
            "cohort_id": None,
            "scope_key": scope_key,
            "name": name,
            "original_name": name,
            "source_identity": source_identity,
            "source_sleeve_id": None,
            "source_path": source_path,
            "strategy": strategy,
            "universe": [],
            "starting_cash": starting_cash,
            "max_positions": 0,
            "max_position_fraction": Decimal(0),
            "settlement_t1": False,
            "leverage": Decimal(1),
            "factor": "",
            "strategy_definition": None,
            "configuration_hash": "",
            "decision_frequency": "",
            "decision_time": "",
            "execution_methodology": "",
            "created_at": None,
            "source_created_at": None,
        },
    )


def _paper_account_values(
    row: sqlite3.Row,
    sleeve_id: str,
    source_path: str,
) -> dict[str, object]:
    created_at, source_created_at = _timestamp(row["created_at"])
    return {
        "sleeve_id": sleeve_id,
        "starting_cash": Decimal(str(row["starting_cash"])),
        "cash": Decimal(str(row["cash"])),
        "realized_pnl": Decimal(str(row["realized_pnl"])),
        "created_at": created_at,
        "source_created_at": source_created_at,
        "last_accrual": (
            _date(_value(row, "last_accrual"))
            if _value(row, "last_accrual") not in {None, ""}
            else None
        ),
        "source_path": source_path,
    }


def _import_paper(
    session: Session,
    conn: sqlite3.Connection,
    source_path: str,
) -> list[ImportedTable]:
    account_row = conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone()
    if account_row is None:
        raise ValueError("paper source has no singleton account")
    starting_cash = Decimal(str(account_row["starting_cash"]))
    sleeve = _source_sleeve(
        session,
        source_path,
        starting_cash=starting_cash,
    )
    imported: list[ImportedTable] = []

    account_values = _paper_account_values(
        account_row, sleeve.sleeve_id, source_path
    )
    existing_account = session.get(PaperAccount, sleeve.sleeve_id)
    if existing_account is None:
        session.add(PaperAccount(**account_values))
    elif not _same(existing_account, account_values):
        raise MigrationConflictError("paper account conflicts with destination")
    digest = _Digest()
    digest.add(account_values)
    imported.append(
        ImportedTable("paper_account", "paper_accounts", digest.count, digest.hexdigest)
    )

    digest = _Digest()
    for row in _rows(conn, "paper_positions"):
        values = {
            "sleeve_id": sleeve.sleeve_id,
            "symbol": str(row["symbol"]),
            "quantity": int(row["quantity"]),
            "avg_cost": Decimal(str(row["avg_cost"])),
            "source_path": source_path,
        }
        existing_position = session.get(
            PaperPosition, (sleeve.sleeve_id, str(row["symbol"]))
        )
        if existing_position is None:
            session.add(PaperPosition(**values))
        elif not _same(existing_position, values):
            raise MigrationConflictError("paper position conflicts with destination")
        digest.add(values)
    imported.append(
        ImportedTable(
            "paper_positions", "paper_positions", digest.count, digest.hexdigest
        )
    )

    digest = _Digest()
    for row in _rows(conn, "paper_orders"):
        created_at, source_created_at = _timestamp(row["created_at"])
        filled_at, source_filled_at = _timestamp(row["filled_at"])
        values = {
            "sleeve_id": sleeve.sleeve_id,
            "source_path": source_path,
            "source_order_id": int(row["id"]),
            "side": str(row["side"]),
            "symbol": str(row["symbol"]),
            "quantity": int(row["quantity"]),
            "limit_price": Decimal(str(row["limit_price"])),
            "status": str(row["status"]),
            "reason": row["reason"],
            "fill_price": _decimal(row["fill_price"]),
            "created_at": created_at,
            "source_created_at": source_created_at,
            "filled_at": filled_at,
            "source_filled_at": source_filled_at,
        }
        existing_order = session.scalar(
            select(PaperOrder).where(
                PaperOrder.sleeve_id == sleeve.sleeve_id,
                PaperOrder.source_path == source_path,
                PaperOrder.source_order_id == int(row["id"]),
            )
        )
        if existing_order is None:
            existing_order = PaperOrder(**values)
            session.add(existing_order)
            session.flush()
            if values["fill_price"] is not None and values["status"] == "FILLED":
                session.add(
                    PaperFill(
                        paper_order_id=existing_order.paper_order_id,
                        fill_sequence=1,
                        quantity=int(str(values["quantity"])),
                        price=values["fill_price"],
                        filled_at=filled_at,
                        source_filled_at=source_filled_at,
                    )
                )
        elif not _same(existing_order, values):
            raise MigrationConflictError("paper order conflicts with destination")
        digest.add(values)
    imported.append(
        ImportedTable("paper_orders", "paper_orders", digest.count, digest.hexdigest)
    )

    if _table_exists(conn, "paper_unsettled"):
        digest = _Digest()
        for row in _rows(conn, "paper_unsettled"):
            values = {
                "sleeve_id": sleeve.sleeve_id,
                "source_path": source_path,
                "source_unsettled_id": int(row["id"]),
                "amount": Decimal(str(row["amount"])),
                "settle_date": _date(row["settle_date"]),
            }
            existing_unsettled = session.scalar(
                select(PaperUnsettledCash).where(
                    PaperUnsettledCash.sleeve_id == sleeve.sleeve_id,
                    PaperUnsettledCash.source_path == source_path,
                    PaperUnsettledCash.source_unsettled_id == int(row["id"]),
                )
            )
            if existing_unsettled is None:
                session.add(PaperUnsettledCash(**values))
            elif not _same(existing_unsettled, values):
                raise MigrationConflictError(
                    "paper unsettled cash conflicts with destination"
                )
            digest.add(values)
        imported.append(
            ImportedTable(
                "paper_unsettled",
                "paper_unsettled_cash",
                digest.count,
                digest.hexdigest,
            )
        )
    return imported


def _cycle_values(
    row: sqlite3.Row,
    sleeve_id: str,
    source_path: str,
) -> dict[str, object]:
    observed_at, source_ts = _timestamp(row["ts"])
    return {
        "sleeve_id": sleeve_id,
        "source_path": source_path,
        "source_cycle_id": int(row["id"]),
        "observed_at": observed_at,
        "source_ts": source_ts,
        "strategy": str(row["strategy"]),
        "num_proposals": int(row["num_proposals"]),
        "num_filled": int(row["num_filled"]),
        "num_rejected": int(row["num_rejected"]),
        "cash": Decimal(str(row["cash"])),
        "positions_value": Decimal(str(row["positions_value"])),
        "total_value": Decimal(str(row["total_value"])),
        "realized_pnl": Decimal(str(row["realized_pnl"])),
        "unrealized_pnl": Decimal(str(row["unrealized_pnl"])),
        "starting_cash": Decimal(str(row["starting_cash"])),
        "return_pct": Decimal(str(row["return_pct"])),
    }


def _ensure_observation_run(
    session: Session,
    *,
    cohort_id: str,
    run_id: str,
    sleeve: Sleeve,
    session_date: date,
    decision_time: datetime,
    source_path: str,
) -> None:
    _ensure_cohort(session, cohort_id, source_path=source_path)
    _ensure_membership(
        session,
        cohort_id,
        sleeve.sleeve_id,
        configuration_hash=sleeve.configuration_hash,
        source_path=source_path,
    )
    run = session.get(CohortRun, run_id)
    if run is None:
        run = CohortRun(
            run_id=run_id,
            run_key=f"legacy-observation:{cohort_id}:{session_date.isoformat()}",
            cohort_id=cohort_id,
            session_id=f"legacy:{session_date.isoformat()}",
            scheduled_for=session_date,
            expected_members=[sleeve.sleeve_id],
            completed_members=[sleeve.sleeve_id],
            snapshot_id=None,
            quote_snapshot_id=None,
            data_snapshot_ids={},
            started_at=decision_time,
            completed_at=decision_time,
            status="completed",
            errors=[],
            source_path=source_path,
        )
        session.add(run)
        session.flush()
    elif run.run_key.startswith("legacy-observation:"):
        if (
            run.cohort_id != cohort_id
            or run.scheduled_for != session_date
        ):
            raise MigrationConflictError(
                "legacy observation run identity conflicts with destination"
            )
        if sleeve.sleeve_id not in run.expected_members:
            run.expected_members = [*run.expected_members, sleeve.sleeve_id]
            run.completed_members = [*run.completed_members, sleeve.sleeve_id]
    elif (
        run.cohort_id != cohort_id
        or run.scheduled_for != session_date
        or sleeve.sleeve_id not in run.expected_members
    ):
        raise MigrationConflictError(
            "official observation references a conflicting durable run"
        )
    member = session.get(CohortRunMember, (run_id, sleeve.sleeve_id))
    if member is None:
        ordinal = session.scalar(
            select(func.max(CohortRunMember.ordinal)).where(
                CohortRunMember.run_id == run_id
            )
        )
        session.add(
            CohortRunMember(
                run_id=run_id,
                sleeve_id=sleeve.sleeve_id,
                source_sleeve_id=None,
                cohort_id=cohort_id,
                ordinal=int(ordinal) + 1 if ordinal is not None else 0,
                status="completed",
                started_at=decision_time,
                completed_at=decision_time,
                error=None,
                source_path=source_path,
            )
        )


def _observation_values(
    row: sqlite3.Row,
    sleeve: Sleeve,
    source_path: str,
) -> dict[str, object]:
    cohort_id = str(row["cohort_id"])
    session_date = _date(row["session_date"])
    decision_time, _ = _timestamp(row["decision_time"], require_aware=True)
    valuation_time, _ = _timestamp(row["valuation_time"], require_aware=True)
    recorded_at, source_recorded_at = _timestamp(row["recorded_at"])
    assert decision_time is not None
    assert valuation_time is not None
    # Execution-timing lineage (#79). A source database written before those columns
    # existed simply has none of them, and an absent value is the truthful record: it
    # was produced by the close-marked model, where the signal and execution sessions
    # coincide. Nothing is invented to fill the gap.
    columns = set(row.keys())

    def optional(name: str) -> object:
        return row[name] if name in columns else None

    signal_time, _ = _timestamp(optional("signal_time"))
    execution_time, _ = _timestamp(optional("execution_time"))
    signal_session = optional("signal_session_date")
    execution_session = optional("execution_session_date")
    return {
        "source_observation_id": int(row["id"]),
        "observation_key": official_observation_key(
            cohort_id, sleeve.sleeve_id, session_date
        ),
        "source_observation_key": str(row["observation_key"]),
        "cohort_id": cohort_id,
        "run_id": str(row["run_id"]),
        "sleeve_id": sleeve.sleeve_id,
        "strategy": str(row["strategy"]),
        "strategy_hash": str(row["strategy_hash"]),
        "session_date": session_date,
        "decision_time": decision_time,
        "valuation_time": valuation_time,
        "execution_methodology": str(optional("execution_methodology") or ""),
        "signal_session_date": (
            None if signal_session is None else _date(signal_session)
        ),
        "execution_session_date": (
            None if execution_session is None else _date(execution_session)
        ),
        "signal_time": signal_time,
        "execution_time": execution_time,
        "status": str(row["status"]),
        "total_value": _decimal(row["total_value"]),
        "return_pct": _decimal(row["return_pct"]),
        "benchmark_value": _decimal(row["benchmark_value"]),
        "exposure": _decimal(row["exposure"]),
        "num_positions": (
            int(row["num_positions"]) if row["num_positions"] is not None else None
        ),
        "turnover": _decimal(row["turnover"]),
        "modeled_cost": _decimal(row["modeled_cost"]),
        "num_filled": int(row["num_filled"]),
        "num_rejected": int(row["num_rejected"]),
        "quote_coverage": _decimal(row["quote_coverage"]),
        "snapshot_ids": _dict_json(row["snapshot_ids"]),
        "readiness_ready": (
            bool(row["readiness_ready"])
            if row["readiness_ready"] is not None
            else None
        ),
        "readiness_reasons": _list_json(row["readiness_reasons"]),
        "recorded_at": recorded_at,
        "source_recorded_at": source_recorded_at,
        "source_path": source_path,
    }


def _import_evaluation(
    session: Session,
    conn: sqlite3.Connection,
    source_path: str,
) -> list[ImportedTable]:
    first_cycle = conn.execute(
        "SELECT starting_cash, strategy FROM agent_cycles ORDER BY id LIMIT 1"
    ).fetchone()
    starting_cash = (
        Decimal(str(first_cycle["starting_cash"]))
        if first_cycle is not None
        else Decimal(0)
    )
    strategy = str(first_cycle["strategy"]) if first_cycle is not None else "legacy-import"
    sleeve = _source_sleeve(
        session,
        source_path,
        starting_cash=starting_cash,
        strategy=strategy,
    )
    imported: list[ImportedTable] = []
    cycle_ids: dict[int, int] = {}

    digest = _Digest()
    for row in _rows(conn, "agent_cycles"):
        values = _cycle_values(row, sleeve.sleeve_id, source_path)
        source_id = int(row["id"])
        existing_cycle = session.scalar(
            select(EvaluationCycle).where(
                EvaluationCycle.sleeve_id == sleeve.sleeve_id,
                EvaluationCycle.source_path == source_path,
                EvaluationCycle.source_cycle_id == source_id,
            )
        )
        if existing_cycle is None:
            existing_cycle = EvaluationCycle(**values)
            session.add(existing_cycle)
            session.flush()
        elif not _same(existing_cycle, values):
            raise MigrationConflictError("evaluation cycle conflicts with destination")
        cycle_ids[source_id] = existing_cycle.cycle_id
        digest.add(values)
    imported.append(
        ImportedTable(
            "agent_cycles", "evaluation_cycles", digest.count, digest.hexdigest
        )
    )

    digest = _Digest()
    for row in _rows(conn, "agent_decisions"):
        source_cycle_id = int(row["cycle_id"])
        destination_cycle_id = cycle_ids.get(source_cycle_id)
        if destination_cycle_id is None:
            raise ValueError("evaluation decision references a missing cycle")
        digest_values = {
            "source_cycle_id": source_cycle_id,
            "source_decision_id": int(row["id"]),
            "side": str(row["side"]),
            "symbol": str(row["symbol"]),
            "quantity": int(row["quantity"]),
            "limit_price": Decimal(str(row["limit_price"])),
            "status": str(row["status"]),
            "fill_price": _decimal(row["fill_price"]),
            "rationale": row["rationale"],
        }
        values = {
            "cycle_id": destination_cycle_id,
            "source_path": source_path,
            **{key: value for key, value in digest_values.items() if key != "source_cycle_id"},
        }
        existing_decision = session.scalar(
            select(EvaluationDecision).where(
                EvaluationDecision.source_path == source_path,
                EvaluationDecision.source_decision_id == int(row["id"]),
            )
        )
        if existing_decision is None:
            session.add(EvaluationDecision(**values))
        else:
            comparable = {
                **values,
                "cycle_id": existing_decision.cycle_id,
            }
            if not _same(existing_decision, comparable):
                raise MigrationConflictError(
                    "evaluation decision conflicts with destination"
                )
        digest.add(digest_values)
    imported.append(
        ImportedTable(
            "agent_decisions",
            "evaluation_decisions",
            digest.count,
            digest.hexdigest,
        )
    )

    if _table_exists(conn, "official_daily_observations"):
        digest = _Digest()
        for row in _rows(conn, "official_daily_observations"):
            values = _observation_values(row, sleeve, source_path)
            _ensure_observation_run(
                session,
                cohort_id=str(values["cohort_id"]),
                run_id=str(values["run_id"]),
                sleeve=sleeve,
                session_date=values["session_date"],  # type: ignore[arg-type]
                decision_time=values["decision_time"],  # type: ignore[arg-type]
                source_path=source_path,
            )
            existing_observation = session.scalar(
                select(OfficialDailyObservation).where(
                    OfficialDailyObservation.observation_key
                    == values["observation_key"]
                )
            )
            if existing_observation is None:
                session.add(OfficialDailyObservation(**values))
            elif not _same(existing_observation, values):
                raise MigrationConflictError(
                    "official observation conflicts with destination"
                )
            digest.add(values)
        imported.append(
            ImportedTable(
                "official_daily_observations",
                "official_daily_observations",
                digest.count,
                digest.hexdigest,
            )
        )
    return imported


def _resolve_member(
    session: Session,
    cohort_id: str,
    source_identity: str,
) -> Sleeve:
    exact = session.get(Sleeve, source_identity)
    if exact is not None:
        return exact
    matches = list(
        session.scalars(
            select(Sleeve).where(
                (
                    (Sleeve.source_sleeve_id == source_identity)
                    | (Sleeve.name == source_identity)
                ),
                (Sleeve.cohort_id == cohort_id) | (Sleeve.cohort_id.is_(None)),
            )
        )
    )
    if len(matches) != 1:
        raise MigrationConflictError(
            "cohort run member cannot be resolved to one stable sleeve"
        )
    return matches[0]


def _import_runs(
    session: Session,
    conn: sqlite3.Connection,
    source_path: str,
) -> list[ImportedTable]:
    run_digest = _Digest()
    member_digest = _Digest()
    for row in _rows(conn, "sleeve_runs"):
        cohort_id = str(row["cohort_id"])
        _ensure_cohort(session, cohort_id, source_path=source_path)
        expected_source = [str(item) for item in _list_json(row["expected_members"])]
        completed_source = [str(item) for item in _list_json(row["completed_members"])]
        resolved = {
            source: _resolve_member(session, cohort_id, source)
            for source in expected_source
        }
        unknown_completed = set(completed_source) - resolved.keys()
        if unknown_completed:
            raise ValueError("completed run members are not expected members")
        expected = [resolved[source].sleeve_id for source in expected_source]
        completed = [resolved[source].sleeve_id for source in completed_source]
        for sleeve in resolved.values():
            _ensure_membership(
                session,
                cohort_id,
                sleeve.sleeve_id,
                configuration_hash=sleeve.configuration_hash,
                source_path=source_path,
            )
        started_at, _ = _timestamp(row["started_at"], require_aware=True)
        completed_at, _ = _timestamp(row["completed_at"], require_aware=True)
        assert started_at is not None
        values = {
            "run_id": str(row["run_id"]),
            "run_key": str(row["run_key"]),
            "cohort_id": cohort_id,
            "session_id": str(row["session_id"]),
            "scheduled_for": _date(row["scheduled_for"]),
            "expected_members": expected,
            "completed_members": completed,
            "snapshot_id": row["snapshot_id"],
            "quote_snapshot_id": row["quote_snapshot_id"],
            "data_snapshot_ids": _dict_json(row["data_snapshot_ids"]),
            "started_at": started_at,
            "completed_at": completed_at,
            "status": str(row["status"]),
            "errors": _list_json(row["errors"]),
            "source_path": source_path,
        }
        existing_run = session.get(CohortRun, values["run_id"])
        if existing_run is None:
            session.add(CohortRun(**values))
            session.flush()
        elif not _same(existing_run, values):
            raise MigrationConflictError("cohort run conflicts with destination")
        run_digest.add(values)

    for row in _rows(conn, "sleeve_run_members"):
        run = session.get(CohortRun, str(row["run_id"]))
        if run is None:
            raise ValueError("run member references a missing run")
        sleeve = _resolve_member(session, run.cohort_id, str(row["sleeve_id"]))
        started_at, _ = _timestamp(row["started_at"], require_aware=True)
        completed_at, _ = _timestamp(row["completed_at"], require_aware=True)
        error = (
            _dict_json(row["error"])
            if row["error"] is not None and row["error"] != ""
            else None
        )
        values = {
            "run_id": run.run_id,
            "sleeve_id": sleeve.sleeve_id,
            "source_sleeve_id": str(row["sleeve_id"]),
            "cohort_id": run.cohort_id,
            "ordinal": int(row["ordinal"]),
            "status": str(row["status"]),
            "started_at": started_at,
            "completed_at": completed_at,
            "error": error,
            "source_path": source_path,
        }
        existing_member = session.get(
            CohortRunMember, (run.run_id, sleeve.sleeve_id)
        )
        if existing_member is None:
            session.add(CohortRunMember(**values))
        elif not _same(existing_member, values):
            raise MigrationConflictError("cohort run member conflicts with destination")
        member_digest.add(values)
    return [
        ImportedTable(
            "sleeve_runs", "cohort_runs", run_digest.count, run_digest.hexdigest
        ),
        ImportedTable(
            "sleeve_run_members",
            "cohort_run_members",
            member_digest.count,
            member_digest.hexdigest,
        ),
    ]


def _import_json_history(
    session: Session,
    conn: sqlite3.Connection,
    source_path: str,
    *,
    kind: str,
) -> list[ImportedTable]:
    digest = _Digest()
    if kind == "research":
        for row in _rows(conn, "strategy_specs"):
            payload = _dict_json(row["spec_json"])
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            created_at, source_created_at = _timestamp(row["created_at"])
            values = {
                "source_path": source_path,
                "source_spec_id": int(row["id"]),
                "created_at": created_at,
                "source_created_at": source_created_at,
                "model": str(row["model"]),
                "specification": payload,
                "specification_hash": hashlib.sha256(encoded.encode()).hexdigest(),
            }
            session.add(ResearchStrategySpec(**values))
            digest.add(values)
        return [
            ImportedTable(
                "strategy_specs",
                "research_strategy_specs",
                digest.count,
                digest.hexdigest,
            )
        ]
    for row in _rows(conn, "promotion_verdicts"):
        payload = _dict_json(row["verdict_json"])
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        created_at, source_created_at = _timestamp(row["created_at"])
        values = {
            "source_path": source_path,
            "source_verdict_id": int(row["id"]),
            "strategy": str(row["strategy"]),
            "universe": str(row["universe"]),
            "created_at": created_at,
            "source_created_at": source_created_at,
            "verdict": payload,
            "verdict_hash": hashlib.sha256(encoded.encode()).hexdigest(),
        }
        session.add(PromotionVerdict(**values))
        digest.add(values)
    return [
        ImportedTable(
            "promotion_verdicts",
            "promotion_verdicts",
            digest.count,
            digest.hexdigest,
        )
    ]


def _bulk_import(
    session: Session,
    conn: sqlite3.Connection,
    *,
    source_table: str,
    destination_model: type[Any],
    destination_table: str,
    mapper: Callable[[sqlite3.Row], dict[str, object]],
) -> ImportedTable:
    digest = _Digest()
    batch: list[dict[str, object]] = []
    for row in _rows(conn, source_table):
        values = mapper(row)
        batch.append(values)
        digest.add(values)
        if len(batch) >= 5000:
            session.execute(insert(destination_model), batch)
            batch.clear()
    if batch:
        session.execute(insert(destination_model), batch)
    return ImportedTable(
        source_table,
        destination_table,
        digest.count,
        digest.hexdigest,
    )


def _import_dataset(
    session: Session,
    conn: sqlite3.Connection,
    source_path: str,
    *,
    kind: str,
) -> list[ImportedTable]:
    if kind == "daily-prices":
        result = _bulk_import(
            session,
            conn,
            source_table="daily_bars",
            destination_model=DailyPriceBar,
            destination_table="daily_price_bars",
            mapper=lambda row: {
                "symbol": str(row["symbol"]),
                "day": _date(row["day"]),
                "open": _decimal(row["open"]),
                "high": _decimal(row["high"]),
                "low": _decimal(row["low"]),
                "close": Decimal(str(row["close"])),
                "volume": int(row["volume"]),
                "source_path": source_path,
            },
        )
    elif kind == "intraday-prices":
        def intraday(row: sqlite3.Row) -> dict[str, object]:
            observed_at, source_ts = _timestamp(row["ts"])
            return {
                "symbol": str(row["symbol"]),
                "minutes": int(row["minutes"]),
                "timestamp_key": str(row["ts"]),
                "observed_at": observed_at,
                "source_ts": source_ts or str(row["ts"]),
                "open": _decimal(row["open"]),
                "high": _decimal(row["high"]),
                "low": _decimal(row["low"]),
                "close": Decimal(str(row["close"])),
                "volume": int(row["volume"]),
                "source_path": source_path,
            }

        result = _bulk_import(
            session,
            conn,
            source_table="intraday_bars",
            destination_model=IntradayPriceBar,
            destination_table="intraday_price_bars",
            mapper=intraday,
        )
    elif kind == "sec":
        result = _bulk_import(
            session,
            conn,
            source_table="sec_facts",
            destination_model=SecFact,
            destination_table="sec_facts",
            mapper=lambda row: {
                "ticker": str(row["ticker"]),
                "cik": int(row["cik"]),
                "concept": str(row["concept"]),
                "unit": str(row["unit"]),
                "period_start": (
                    _date(row["period_start"])
                    if row["period_start"] is not None
                    else None
                ),
                "period_end": _date(row["period_end"]),
                "value": Decimal(str(row["value"])),
                "fiscal_year": (
                    int(row["fiscal_year"])
                    if row["fiscal_year"] is not None
                    else None
                ),
                "fiscal_period": row["fiscal_period"],
                "form": row["form"],
                "filed": _date(row["filed"]),
                "accession": str(row["accession"]),
                "frame": row["frame"],
                "source_path": source_path,
            },
        )
    else:
        def usage(row: sqlite3.Row) -> dict[str, object]:
            occurred_at, source_ts = _timestamp(row["ts"])
            return {
                "source_path": source_path,
                "source_event_id": int(row["id"]),
                "occurred_at": occurred_at,
                "source_ts": source_ts,
                "kind": str(row["kind"]),
                "model": str(row["model"]),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
                "cache_read_tokens": int(row["cache_read_tokens"]),
                "cache_write_tokens": int(row["cache_write_tokens"]),
                "web_searches": int(row["web_searches"]),
                "cost": Decimal(str(row["cost"])),
            }

        result = _bulk_import(
            session,
            conn,
            source_table="usage_events",
            destination_model=UsageEvent,
            destination_table="usage_events",
            mapper=usage,
        )
    return [result]


def _import_source(
    session: Session,
    path: Path,
    *,
    source_path: str,
    kind: str,
) -> list[ImportedTable]:
    if kind == "cohort-manifest":
        return _import_manifest(session, path, source_path)
    conn = _source_connection(path)
    try:
        if kind == "registry":
            return _import_registry(session, conn, source_path)
        if kind == "runs":
            return _import_runs(session, conn, source_path)
        if kind == "paper":
            return _import_paper(session, conn, source_path)
        if kind == "evaluation":
            return _import_evaluation(session, conn, source_path)
        if kind in {"research", "promotion"}:
            return _import_json_history(
                session, conn, source_path, kind=kind
            )
        return _import_dataset(session, conn, source_path, kind=kind)
    finally:
        conn.close()


def _priority(kind: str) -> int:
    return {
        "cohort-manifest": 0,
        "registry": 1,
        "paper": 2,
        "runs": 3,
        "evaluation": 4,
        "research": 5,
        "promotion": 6,
        "usage": 7,
        "daily-prices": 8,
        "intraday-prices": 9,
        "sec": 10,
    }[kind]


def preflight_migration(
    database: Database,
    inventory: InventoryReport,
) -> MigrationPreflight:
    """Read-only destination conflict check for ``storage migrate --dry-run``."""
    if not inventory.eligible_sources:
        raise MigrationConflictError("no eligible migration sources were discovered")
    missing = set(Base.metadata.tables) - set(inspect(database.engine).get_table_names())
    if missing:
        raise MigrationConflictError(
            "destination schema is not ready; run the reviewed Alembic upgrade"
        )
    ineligible_required = [
        source
        for source in inventory.sources
        if not source.eligible
        and not (
            source.kind == "usage"
            and source.eligibility_reason
            == "optional usage database contains no history"
        )
    ]
    if ineligible_required:
        raise MigrationConflictError(
            "one or more discovered migration sources has an unsupported schema"
        )

    resumable = False
    with database.session() as session:
        existing = session.get(MigrationRun, inventory.source_set_hash)
        if existing is not None:
            if existing.source_set_hash != inventory.source_set_hash:
                raise MigrationConflictError(
                    "destination migration identity conflicts with source set"
                )
            resumable = True
        else:
            for source in inventory.eligible_sources:
                for model in _DESTINATION_MODELS.values():
                    if session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.source_path == source.relative_path)
                    ):
                        raise MigrationConflictError(
                            "destination contains unexplained records for a source path"
                        )

    return MigrationPreflight(
        migration_id=inventory.source_set_hash,
        eligible_sources=len(inventory.eligible_sources),
        source_tables=sum(
            len(source.tables) for source in inventory.eligible_sources
        ),
        source_rows=sum(
            sum(source.table_counts.values())
            for source in inventory.eligible_sources
        ),
        destination_schema_ready=True,
        resumable=resumable,
    )


def _initialize_migration(
    database: Database,
    snapshot: SnapshotSet,
    *,
    code_revision: str,
) -> None:
    with database.session() as session:
        existing = session.get(MigrationRun, snapshot.migration_id)
        if existing is None:
            session.add(
                MigrationRun(
                    migration_id=snapshot.migration_id,
                    source_set_hash=snapshot.source_set_hash,
                    code_revision=code_revision,
                    started_at=datetime.now(UTC),
                    completed_at=None,
                    status="running",
                    backup_path_hash=hashlib.sha256(
                        snapshot.migration_id.encode()
                    ).hexdigest(),
                    verification_status=None,
                )
            )
        elif existing.source_set_hash != snapshot.source_set_hash:
            raise MigrationConflictError(
                "migration id conflicts with a different source set"
            )
        for source in snapshot.sources:
            key = (snapshot.migration_id, source.relative_path)
            row = session.get(MigrationSource, key)
            if row is None:
                session.add(
                    MigrationSource(
                        migration_id=snapshot.migration_id,
                        source_path=source.relative_path,
                        source_hash=source.source_hash,
                        snapshot_hash=source.snapshot_hash,
                        content_hash=source.content_hash,
                        schema_version=source.schema_version,
                        table_counts=source.table_counts,
                        table_checksums=source.table_checksums,
                        status="pending",
                        started_at=None,
                        completed_at=None,
                        error_code=None,
                    )
                )
            elif (
                row.source_hash != source.source_hash
                or row.snapshot_hash != source.snapshot_hash
                or row.content_hash != source.content_hash
            ):
                raise MigrationConflictError(
                    "migration source hashes conflict with prior attempt"
                )


def execute_migration(
    database: Database,
    snapshot_root: Path,
    snapshot: SnapshotSet,
    *,
    code_revision: str,
) -> MigrationOutcome:
    """Import every snapshot source once, transactionally and in dependency order."""
    verify_snapshot_set(snapshot_root, snapshot)
    _initialize_migration(database, snapshot, code_revision=code_revision)
    migration = None
    with database.session() as session:
        migration = session.get(MigrationRun, snapshot.migration_id)
        assert migration is not None
        if migration.status == "completed":
            return MigrationOutcome(
                migration_id=migration.migration_id,
                status=migration.status,
                sources_completed=len(snapshot.sources),
                verification_status=migration.verification_status or "unknown",
            )

    sources = sorted(
        snapshot.sources,
        key=lambda source: (_priority(source.kind), source.relative_path),
    )
    for source in sources:
        try:
            with database.session() as session:
                state = session.scalar(
                    select(MigrationSource)
                    .where(
                        MigrationSource.migration_id == snapshot.migration_id,
                        MigrationSource.source_path == source.relative_path,
                    )
                    .with_for_update()
                )
                assert state is not None
                if state.status == "completed":
                    continue
                state.status = "running"
                state.started_at = datetime.now(UTC)
                path = (
                    snapshot_root
                    / "sources"
                    / Path(source.relative_path)
                )
                imported = _import_source(
                    session,
                    path,
                    source_path=source.relative_path,
                    kind=source.kind,
                )
                for result in imported:
                    session.merge(
                        MigrationTableResult(
                            migration_id=snapshot.migration_id,
                            source_path=source.relative_path,
                            source_table=result.source_table,
                            destination_table=result.destination_table,
                            source_count=result.row_count,
                            destination_count=result.row_count,
                            source_checksum=result.checksum,
                            destination_checksum=result.checksum,
                            verified_at=datetime.now(UTC),
                            status="pending-verification",
                        )
                    )
                state.status = "completed"
                state.completed_at = datetime.now(UTC)
                state.error_code = None
        except Exception as exc:
            with database.session() as session:
                state = session.get(
                    MigrationSource,
                    (snapshot.migration_id, source.relative_path),
                )
                assert state is not None
                state.status = "failed"
                state.error_code = type(exc).__name__
                migration = session.get(MigrationRun, snapshot.migration_id)
                assert migration is not None
                migration.status = "failed"
            raise MigrationConflictError(
                f"migration source {source.relative_path} failed closed"
            ) from exc

    verification = verify_migration(database, snapshot.migration_id)
    with database.session() as session:
        migration = session.get(MigrationRun, snapshot.migration_id)
        assert migration is not None
        migration.verification_status = verification
        if verification == "passed":
            migration.status = "completed"
            migration.completed_at = datetime.now(UTC)
        else:
            migration.status = "verification-failed"
    if verification != "passed":
        raise MigrationVerificationError("migration verification failed")
    return MigrationOutcome(
        migration_id=snapshot.migration_id,
        status="completed",
        sources_completed=len(snapshot.sources),
        verification_status=verification,
    )


def _check_foreign_keys(database: Database) -> bool:
    with database.engine.connect() as connection:
        if database.dialect == "sqlite":
            return not list(connection.exec_driver_sql("PRAGMA foreign_key_check"))
        count = connection.execute(
            select(func.count())
            .select_from(OfficialDailyObservation)
            .where(
                ~OfficialDailyObservation.sleeve_id.in_(
                    select(Sleeve.sleeve_id)
                )
            )
        ).scalar_one()
        return int(count) == 0


_DESTINATION_MODELS: dict[str, type[Any]] = {
    "cohorts": Cohort,
    "sleeves": Sleeve,
    "paper_accounts": PaperAccount,
    "paper_positions": PaperPosition,
    "paper_orders": PaperOrder,
    "paper_unsettled_cash": PaperUnsettledCash,
    "evaluation_cycles": EvaluationCycle,
    "evaluation_decisions": EvaluationDecision,
    "official_daily_observations": OfficialDailyObservation,
    "cohort_runs": CohortRun,
    "cohort_run_members": CohortRunMember,
    "research_strategy_specs": ResearchStrategySpec,
    "promotion_verdicts": PromotionVerdict,
    "usage_events": UsageEvent,
    "daily_price_bars": DailyPriceBar,
    "intraday_price_bars": IntradayPriceBar,
    "sec_facts": SecFact,
}

_DIGEST_FIELDS: dict[str, tuple[str, ...]] = {
    "cohorts": (
        "cohort_id",
        "name",
        "created_at",
        "start_session",
        "status",
        "starting_cash_per_sleeve",
        "settlement_model",
        "leverage",
        "benchmark_sleeve_name",
        "decision_schedule",
        "cost_model_id",
        "manifest_json",
        "manifest_hash",
        "source_path",
    ),
    "sleeves": (
        "sleeve_id",
        "namespace_id",
        "cohort_id",
        "scope_key",
        "name",
        "original_name",
        "source_identity",
        "source_sleeve_id",
        "source_path",
        "strategy",
        "universe",
        "starting_cash",
        "max_positions",
        "max_position_fraction",
        "settlement_t1",
        "leverage",
        "factor",
        "strategy_definition",
        "configuration_hash",
        "decision_frequency",
        "decision_time",
        "execution_methodology",
        "created_at",
        "source_created_at",
    ),
    "paper_accounts": (
        "sleeve_id",
        "starting_cash",
        "cash",
        "realized_pnl",
        "created_at",
        "source_created_at",
        "last_accrual",
        "source_path",
    ),
    "paper_positions": (
        "sleeve_id",
        "symbol",
        "quantity",
        "avg_cost",
        "source_path",
    ),
    "paper_orders": (
        "sleeve_id",
        "source_path",
        "source_order_id",
        "side",
        "symbol",
        "quantity",
        "limit_price",
        "status",
        "reason",
        "fill_price",
        "created_at",
        "source_created_at",
        "filled_at",
        "source_filled_at",
    ),
    "paper_unsettled_cash": (
        "sleeve_id",
        "source_path",
        "source_unsettled_id",
        "amount",
        "settle_date",
    ),
    "evaluation_cycles": (
        "sleeve_id",
        "source_path",
        "source_cycle_id",
        "observed_at",
        "source_ts",
        "strategy",
        "num_proposals",
        "num_filled",
        "num_rejected",
        "cash",
        "positions_value",
        "total_value",
        "realized_pnl",
        "unrealized_pnl",
        "starting_cash",
        "return_pct",
    ),
    "official_daily_observations": (
        "source_observation_id",
        "observation_key",
        "source_observation_key",
        "cohort_id",
        "run_id",
        "sleeve_id",
        "strategy",
        "strategy_hash",
        "session_date",
        "decision_time",
        "valuation_time",
        "execution_methodology",
        "signal_session_date",
        "execution_session_date",
        "signal_time",
        "execution_time",
        "status",
        "total_value",
        "return_pct",
        "benchmark_value",
        "exposure",
        "num_positions",
        "turnover",
        "modeled_cost",
        "num_filled",
        "num_rejected",
        "quote_coverage",
        "snapshot_ids",
        "readiness_ready",
        "readiness_reasons",
        "recorded_at",
        "source_recorded_at",
        "source_path",
    ),
    "cohort_runs": (
        "run_id",
        "run_key",
        "cohort_id",
        "session_id",
        "scheduled_for",
        "expected_members",
        "completed_members",
        "snapshot_id",
        "quote_snapshot_id",
        "data_snapshot_ids",
        "started_at",
        "completed_at",
        "status",
        "errors",
        "source_path",
    ),
    "cohort_run_members": (
        "run_id",
        "sleeve_id",
        "source_sleeve_id",
        "cohort_id",
        "ordinal",
        "status",
        "started_at",
        "completed_at",
        "error",
        "source_path",
    ),
    "research_strategy_specs": (
        "source_path",
        "source_spec_id",
        "created_at",
        "source_created_at",
        "model",
        "specification",
        "specification_hash",
    ),
    "promotion_verdicts": (
        "source_path",
        "source_verdict_id",
        "strategy",
        "universe",
        "created_at",
        "source_created_at",
        "verdict",
        "verdict_hash",
    ),
    "usage_events": (
        "source_path",
        "source_event_id",
        "occurred_at",
        "source_ts",
        "kind",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "web_searches",
        "cost",
    ),
    "daily_price_bars": (
        "symbol",
        "day",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "source_path",
    ),
    "intraday_price_bars": (
        "symbol",
        "minutes",
        "timestamp_key",
        "observed_at",
        "source_ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "source_path",
    ),
    "sec_facts": (
        "ticker",
        "cik",
        "concept",
        "unit",
        "period_start",
        "period_end",
        "value",
        "fiscal_year",
        "fiscal_period",
        "form",
        "filed",
        "accession",
        "frame",
        "source_path",
    ),
}

_DIGEST_ORDER: dict[str, tuple[str, ...]] = {
    "cohorts": ("cohort_id",),
    "sleeves": ("name",),
    "paper_accounts": ("sleeve_id",),
    "paper_positions": ("symbol",),
    "paper_orders": ("source_order_id",),
    "paper_unsettled_cash": ("source_unsettled_id",),
    "evaluation_cycles": ("source_cycle_id",),
    "official_daily_observations": ("source_observation_id",),
    "cohort_runs": ("run_id",),
    "cohort_run_members": ("run_id", "source_sleeve_id"),
    "research_strategy_specs": ("source_spec_id",),
    "promotion_verdicts": ("source_verdict_id",),
    "usage_events": ("source_event_id",),
    "daily_price_bars": ("symbol", "day"),
    "intraday_price_bars": ("symbol", "minutes", "timestamp_key"),
    "sec_facts": (
        "ticker",
        "concept",
        "unit",
        "period_end",
        "filed",
        "accession",
    ),
}


def _destination_digest(
    session: Session,
    destination_table: str,
    source_path: str,
) -> tuple[int, str]:
    digest = _Digest()
    if destination_table == "evaluation_decisions":
        statement = (
            select(EvaluationDecision, EvaluationCycle.source_cycle_id)
            .join(
                EvaluationCycle,
                EvaluationCycle.cycle_id == EvaluationDecision.cycle_id,
            )
            .where(EvaluationDecision.source_path == source_path)
            .order_by(EvaluationDecision.source_decision_id)
            .execution_options(yield_per=4096)
        )
        for decision, source_cycle_id in session.execute(statement):
            digest.add(
                {
                    "source_cycle_id": source_cycle_id,
                    "source_decision_id": decision.source_decision_id,
                    "side": decision.side,
                    "symbol": decision.symbol,
                    "quantity": decision.quantity,
                    "limit_price": decision.limit_price,
                    "status": decision.status,
                    "fill_price": decision.fill_price,
                    "rationale": decision.rationale,
                }
            )
        return digest.count, digest.hexdigest

    model = _DESTINATION_MODELS[destination_table]
    source_column = model.source_path
    order_columns = [
        getattr(model, field)
        for field in _DIGEST_ORDER[destination_table]
    ]
    statement = (
        select(model)
        .where(source_column == source_path)
        .order_by(*order_columns)
        .execution_options(yield_per=4096)
    )
    for row in session.scalars(statement):
        digest.add(
            {
                field: getattr(row, field)
                for field in _DIGEST_FIELDS[destination_table]
            }
        )
    return digest.count, digest.hexdigest


def verify_migration(database: Database, migration_id: str) -> str:
    """Verification-only rerun: counts, constraints, financial and uniqueness invariants."""
    failures: list[str] = []
    with database.session() as session:
        migration = session.get(MigrationRun, migration_id)
        if migration is None:
            raise KeyError(migration_id)
        results = list(
            session.scalars(
                select(MigrationTableResult).where(
                    MigrationTableResult.migration_id == migration_id
                )
            )
        )
        sources = list(
            session.scalars(
                select(MigrationSource).where(
                    MigrationSource.migration_id == migration_id
                )
            )
        )
        results_by_source: dict[str, set[str]] = {}
        for result in results:
            results_by_source.setdefault(result.source_path, set()).add(
                result.source_table
            )
        for source in sources:
            expected_tables = set(source.table_counts)
            if not expected_tables and source.source_path.endswith(".json"):
                expected_tables = {"cohort_manifest"}
            if source.status != "completed":
                failures.append(f"{source.source_path}:source-not-completed")
            if results_by_source.get(source.source_path, set()) != expected_tables:
                failures.append(f"{source.source_path}:table-coverage")
        for result in results:
            destination_count, destination_checksum = _destination_digest(
                session,
                result.destination_table,
                result.source_path,
            )
            result.destination_count = destination_count
            result.destination_checksum = destination_checksum
            result.verified_at = datetime.now(UTC)
            if destination_count != result.source_count:
                result.status = "count-mismatch"
                failures.append(
                    f"{result.source_path}:{result.destination_table}:count"
                )
            elif destination_checksum != result.source_checksum:
                result.status = "checksum-mismatch"
                failures.append(
                    f"{result.source_path}:{result.destination_table}:checksum"
                )
            else:
                result.status = "passed"

        duplicate_runs = session.execute(
            select(CohortRun.cohort_id, CohortRun.scheduled_for, func.count())
            .group_by(CohortRun.cohort_id, CohortRun.scheduled_for)
            .having(func.count() > 1)
        ).first()
        duplicate_observations = session.execute(
            select(
                OfficialDailyObservation.cohort_id,
                OfficialDailyObservation.sleeve_id,
                OfficialDailyObservation.session_date,
                func.count(),
            )
            .group_by(
                OfficialDailyObservation.cohort_id,
                OfficialDailyObservation.sleeve_id,
                OfficialDailyObservation.session_date,
            )
            .having(func.count() > 1)
        ).first()
        if duplicate_runs is not None:
            failures.append("duplicate-official-runs")
        if duplicate_observations is not None:
            failures.append("duplicate-official-observations")

        accounts = list(session.scalars(select(PaperAccount)))
        for account in accounts:
            positions = list(
                session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.sleeve_id == account.sleeve_id
                    )
                )
            )
            unsettled = list(
                session.scalars(
                    select(PaperUnsettledCash).where(
                        PaperUnsettledCash.sleeve_id == account.sleeve_id
                    )
                )
            )
            if any(position.quantity < 0 for position in positions):
                failures.append("negative-paper-position")
            if any(Decimal(item.amount) < 0 for item in unsettled):
                failures.append("negative-unsettled-cash")

        imported_orders = session.scalars(
            select(PaperOrder).where(PaperOrder.source_path.is_not(None))
        )
        for order in imported_orders:
            fills = list(
                session.scalars(
                    select(PaperFill).where(
                        PaperFill.paper_order_id == order.paper_order_id
                    )
                )
            )
            expected_fill = order.status == "FILLED" and order.fill_price is not None
            if expected_fill:
                if len(fills) != 1:
                    failures.append("paper-fill-cardinality")
                elif (
                    fills[0].quantity != order.quantity
                    or fills[0].price != order.fill_price
                    or fills[0].filled_at != order.filled_at
                    or fills[0].source_filled_at != order.source_filled_at
                ):
                    failures.append("paper-fill-values")
            elif fills:
                failures.append("unexpected-paper-fill")

        runs = list(
            session.scalars(select(CohortRun).where(CohortRun.source_path.is_not(None)))
        )
        for run in runs:
            expected = set(run.expected_members)
            completed = set(run.completed_members)
            members = set(
                session.scalars(
                    select(CohortRunMember.sleeve_id).where(
                        CohortRunMember.run_id == run.run_id
                    )
                )
            )
            if not completed.issubset(expected):
                failures.append("cohort-run-completed-not-expected")
            if members != expected:
                failures.append("cohort-run-member-set")

        for cohort in session.scalars(
            select(Cohort).where(Cohort.source_path.is_not(None))
        ):
            encoded = json.dumps(
                cohort.manifest_json,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if hashlib.sha256(encoded.encode()).hexdigest() != cohort.manifest_hash:
                failures.append("cohort-manifest-hash")

        for specification in session.scalars(
            select(ResearchStrategySpec).where(
                ResearchStrategySpec.source_path.is_not(None)
            )
        ):
            encoded = json.dumps(
                specification.specification,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if (
                hashlib.sha256(encoded.encode()).hexdigest()
                != specification.specification_hash
            ):
                failures.append("research-specification-hash")

        for verdict in session.scalars(
            select(PromotionVerdict).where(PromotionVerdict.source_path.is_not(None))
        ):
            encoded = json.dumps(
                verdict.verdict,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if hashlib.sha256(encoded.encode()).hexdigest() != verdict.verdict_hash:
                failures.append("promotion-verdict-hash")

        orphan_decision = session.scalar(
            select(func.count())
            .select_from(EvaluationDecision)
            .where(
                ~EvaluationDecision.cycle_id.in_(
                    select(EvaluationCycle.cycle_id)
                )
            )
        )
        if int(orphan_decision or 0):
            failures.append("orphan-evaluation-decision")

    if not _check_foreign_keys(database):
        failures.append("foreign-key-check")
    status = "failed" if failures else "passed"
    with database.session() as session:
        migration = session.get(MigrationRun, migration_id)
        assert migration is not None
        migration.verification_status = status
    return status
