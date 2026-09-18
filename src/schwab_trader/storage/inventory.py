"""Sanitized source discovery, row counts, hashes, and normalized checksums."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_TOP_LEVEL = {
    "paper.sqlite3": "paper",
    "agent_eval.sqlite3": "evaluation",
    "research.sqlite3": "research",
    "prices.sqlite3": "daily-prices",
    "intraday.sqlite3": "intraday-prices",
    "sec.sqlite3": "sec",
    "promotion.sqlite3": "promotion",
    "usage.sqlite3": "usage",
}

_EXPECTED_TABLES: dict[str, frozenset[str]] = {
    "registry": frozenset({"sleeves"}),
    "runs": frozenset({"sleeve_runs", "sleeve_run_members"}),
    "paper": frozenset(
        {"paper_account", "paper_positions", "paper_orders", "paper_unsettled"}
    ),
    "evaluation": frozenset(
        {"agent_cycles", "agent_decisions", "official_daily_observations"}
    ),
    "research": frozenset({"strategy_specs"}),
    "daily-prices": frozenset({"daily_bars"}),
    "intraday-prices": frozenset({"intraday_bars"}),
    "sec": frozenset({"sec_facts"}),
    "promotion": frozenset({"promotion_verdicts"}),
    "usage": frozenset({"usage_events"}),
}

_REQUIRED_TABLES: dict[str, frozenset[str]] = {
    **_EXPECTED_TABLES,
    "paper": frozenset({"paper_account", "paper_positions", "paper_orders"}),
    "evaluation": frozenset({"agent_cycles", "agent_decisions"}),
}

_DECIMAL_COLUMNS: dict[str, frozenset[str]] = {
    "sleeves": frozenset(
        {"starting_cash", "max_position_fraction", "leverage"}
    ),
    "paper_account": frozenset({"starting_cash", "cash", "realized_pnl"}),
    "paper_positions": frozenset({"avg_cost"}),
    "paper_orders": frozenset({"limit_price", "fill_price"}),
    "paper_unsettled": frozenset({"amount"}),
    "agent_cycles": frozenset(
        {
            "cash",
            "positions_value",
            "total_value",
            "realized_pnl",
            "unrealized_pnl",
            "starting_cash",
            "return_pct",
        }
    ),
    "agent_decisions": frozenset({"limit_price", "fill_price"}),
    "official_daily_observations": frozenset(
        {
            "total_value",
            "return_pct",
            "benchmark_value",
            "exposure",
            "turnover",
            "modeled_cost",
            "quote_coverage",
        }
    ),
    "daily_bars": frozenset({"open", "high", "low", "close"}),
    "intraday_bars": frozenset({"open", "high", "low", "close"}),
    "sec_facts": frozenset({"value"}),
    "usage_events": frozenset({"cost"}),
}

_JSON_COLUMNS: dict[str, frozenset[str]] = {
    "sleeves": frozenset({"definition_json"}),
    "official_daily_observations": frozenset(
        {"snapshot_ids", "readiness_reasons"}
    ),
    "sleeve_runs": frozenset(
        {
            "expected_members",
            "completed_members",
            "data_snapshot_ids",
            "errors",
        }
    ),
    "sleeve_run_members": frozenset({"error"}),
    "strategy_specs": frozenset({"spec_json"}),
    "promotion_verdicts": frozenset({"verdict_json"}),
}

_TIMESTAMP_COLUMNS: dict[str, frozenset[str]] = {
    "sleeves": frozenset({"created_at"}),
    "paper_account": frozenset({"created_at"}),
    "paper_orders": frozenset({"created_at", "filled_at"}),
    "agent_cycles": frozenset({"ts"}),
    "official_daily_observations": frozenset(
        {
            "decision_time",
            "valuation_time",
            "signal_time",
            "execution_time",
            "recorded_at",
        }
    ),
    "sleeve_runs": frozenset({"started_at", "completed_at"}),
    "sleeve_run_members": frozenset({"started_at", "completed_at"}),
    "strategy_specs": frozenset({"created_at"}),
    "promotion_verdicts": frozenset({"created_at"}),
    "usage_events": frozenset({"ts"}),
    "intraday_bars": frozenset({"ts"}),
}

_DATE_COLUMNS: dict[str, frozenset[str]] = {
    "paper_account": frozenset({"last_accrual"}),
    "paper_unsettled": frozenset({"settle_date"}),
    "official_daily_observations": frozenset(
        {"session_date", "signal_session_date", "execution_session_date"}
    ),
    "sleeve_runs": frozenset({"scheduled_for"}),
    "daily_bars": frozenset({"day"}),
    "sec_facts": frozenset({"period_start", "period_end", "filed"}),
}


class SourceType(StrEnum):
    SQLITE = "sqlite"
    COHORT_MANIFEST = "cohort-manifest"


class TableInventory(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    row_count: int
    checksum: str
    ddl: str


class SourceInventory(BaseModel):
    model_config = ConfigDict(frozen=True)

    relative_path: str
    source_type: SourceType
    kind: str
    byte_size: int
    source_hash: str
    content_hash: str
    schema_version: int | None = None
    tables: tuple[TableInventory, ...] = ()
    eligible: bool
    eligibility_reason: str

    @property
    def table_counts(self) -> dict[str, int]:
        return {table.name: table.row_count for table in self.tables}

    @property
    def table_checksums(self) -> dict[str, str]:
        return {table.name: table.checksum for table in self.tables}


class InventoryReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_root: str = Field(exclude=True)
    generated_at: datetime
    source_set_hash: str
    sources: tuple[SourceInventory, ...]
    excluded_categories: tuple[str, ...]

    @property
    def eligible_sources(self) -> tuple[SourceInventory, ...]:
        return tuple(source for source in self.sources if source.eligible)

    def sanitized_payload(self) -> dict[str, Any]:
        """JSON-safe report with no absolute path or record values."""
        return self.model_dump(mode="json", exclude={"source_root"})


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path, root: Path) -> str:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValueError("discovered source escapes the configured data root") from exc


def discover_sources(root: Path) -> list[tuple[Path, str, SourceType]]:
    """Discover exactly the allow-listed migration sources."""
    root = root.resolve()
    data = root / "data"
    discovered: list[tuple[Path, str, SourceType]] = []
    for filename, kind in _TOP_LEVEL.items():
        path = data / filename
        if path.is_file():
            discovered.append((path, kind, SourceType.SQLITE))
    sleeves_dir = data / "sleeves"
    for filename, kind in (("registry.sqlite3", "registry"), ("runs.sqlite3", "runs")):
        path = sleeves_dir / filename
        if path.is_file():
            discovered.append((path, kind, SourceType.SQLITE))
    if sleeves_dir.is_dir():
        for child in sleeves_dir.iterdir():
            if not child.is_dir() or child.name == "cohorts":
                continue
            for filename, kind in (
                ("paper.sqlite3", "paper"),
                ("eval.sqlite3", "evaluation"),
            ):
                path = child / filename
                if path.is_file():
                    discovered.append((path, kind, SourceType.SQLITE))
        cohort_dir = sleeves_dir / "cohorts"
        if cohort_dir.is_dir():
            for path in cohort_dir.glob("*.json"):
                if path.is_file():
                    discovered.append(
                        (path, "cohort-manifest", SourceType.COHORT_MANIFEST)
                    )
    discovered.sort(key=lambda item: _relative(item[0], root))
    return discovered


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _decimal_text(value: object) -> str:
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError("non-finite decimal in source data")
    if decimal == 0:
        return "0"
    return format(decimal.normalize(), "f")


def _normalize_timestamp(value: object) -> str:
    raw = str(value)
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return f"unparsed:{raw}"
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return f"legacy-naive:{raw}"
    return stamp.astimezone(UTC).isoformat()


def normalize_value(table: str, column: str, value: object) -> object:
    """Normalize a scalar for checksums without exposing its value."""
    if value is None:
        return None
    if column in _DECIMAL_COLUMNS.get(table, frozenset()):
        try:
            return {"decimal": _decimal_text(value)}
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid decimal in {table}.{column}") from exc
    if column in _JSON_COLUMNS.get(table, frozenset()):
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {table}.{column}") from exc
        return {"json": parsed}
    if column in _TIMESTAMP_COLUMNS.get(table, frozenset()):
        return {"timestamp": _normalize_timestamp(value)}
    if column in _DATE_COLUMNS.get(table, frozenset()):
        try:
            return {"date": date.fromisoformat(str(value)).isoformat()}
        except ValueError:
            return {"date-unparsed": str(value)}
    if isinstance(value, bytes):
        return {"bytes-sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, int):
        return {"integer": str(value)}
    if isinstance(value, float):
        return {"decimal": _decimal_text(value)}
    return {"text": str(value)}


def normalized_row(
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> list[object]:
    return [
        normalize_value(table, column, value)
        for column, value in zip(columns, values, strict=True)
    ]


def _table_checksum(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    primary_key: tuple[str, ...],
) -> str:
    selected = ", ".join(_quote_identifier(column) for column in columns)
    order = primary_key or columns
    ordering = ", ".join(_quote_identifier(column) for column in order)
    cursor = conn.execute(
        f"SELECT {selected} FROM {_quote_identifier(table)} ORDER BY {ordering}"
    )
    digest = hashlib.sha256()
    while rows := cursor.fetchmany(4096):
        for row in rows:
            payload = normalized_row(table, columns, tuple(row))
            digest.update(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            )
            digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_inventory(path: Path, root: Path, kind: str) -> SourceInventory:
    relative = _relative(path, root)
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        schema_rows = conn.execute(
            "SELECT name, sql FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables: list[TableInventory] = []
        for schema_row in schema_rows:
            table = str(schema_row["name"])
            column_rows = conn.execute(
                f"PRAGMA table_info({_quote_identifier(table)})"
            ).fetchall()
            columns = tuple(str(row["name"]) for row in column_rows)
            primary_key = tuple(
                str(row["name"])
                for row in sorted(column_rows, key=lambda item: int(item["pk"]))
                if int(row["pk"]) > 0
            )
            count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
            )
            checksum = _table_checksum(conn, table, columns, primary_key)
            tables.append(
                TableInventory(
                    name=table,
                    columns=columns,
                    primary_key=primary_key,
                    row_count=count,
                    checksum=checksum,
                    ddl=str(schema_row["sql"] or ""),
                )
            )
    finally:
        conn.close()

    table_names = {table.name for table in tables}
    expected = _EXPECTED_TABLES[kind]
    required = _REQUIRED_TABLES[kind]
    unknown = table_names - expected
    missing = required - table_names
    eligible = not unknown and not missing
    reason = "eligible"
    if unknown:
        reason = f"unsupported tables: {', '.join(sorted(unknown))}"
    elif missing:
        reason = f"missing required tables: {', '.join(sorted(missing))}"
    elif kind == "usage":
        usage_count = next(
            table.row_count for table in tables if table.name == "usage_events"
        )
        if usage_count == 0:
            eligible = False
            reason = "optional usage database contains no history"
    content_payload = {
        "schema_version": schema_version,
        "tables": [
            {
                "name": table.name,
                "columns": table.columns,
                "primary_key": table.primary_key,
                "rows": table.row_count,
                "checksum": table.checksum,
                "ddl": table.ddl,
            }
            for table in tables
        ],
    }
    content_hash = hashlib.sha256(
        json.dumps(
            content_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return SourceInventory(
        relative_path=relative,
        source_type=SourceType.SQLITE,
        kind=kind,
        byte_size=path.stat().st_size,
        source_hash=file_sha256(path),
        content_hash=content_hash,
        schema_version=schema_version,
        tables=tuple(tables),
        eligible=eligible,
        eligibility_reason=reason,
    )


def _manifest_inventory(path: Path, root: Path) -> SourceInventory:
    relative = _relative(path, root)
    raw = path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    eligible = True
    reason = "eligible"
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("cohort"), dict):
            raise ValueError
        cohort = payload["cohort"]
        if not isinstance(cohort.get("cohort_id"), str) or not cohort["cohort_id"].strip():
            raise ValueError
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        content_hash = hashlib.sha256(canonical).hexdigest()
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        eligible = False
        reason = "invalid cohort manifest shape"
        content_hash = source_hash
    return SourceInventory(
        relative_path=relative,
        source_type=SourceType.COHORT_MANIFEST,
        kind="cohort-manifest",
        byte_size=len(raw),
        source_hash=source_hash,
        content_hash=content_hash,
        eligible=eligible,
        eligibility_reason=reason,
    )


def build_inventory(root: Path) -> InventoryReport:
    """Build a sanitized report without writing to any source or destination."""
    root = root.resolve()
    sources: list[SourceInventory] = []
    for path, kind, source_type in discover_sources(root):
        if source_type is SourceType.SQLITE:
            sources.append(_sqlite_inventory(path, root, kind))
        else:
            sources.append(_manifest_inventory(path, root))
    source_set = [
        (source.relative_path, source.content_hash)
        for source in sources
        if source.eligible
    ]
    source_set_hash = hashlib.sha256(
        json.dumps(source_set, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return InventoryReport(
        source_root=str(root),
        generated_at=datetime.now(UTC),
        source_set_hash=source_set_hash,
        sources=tuple(sources),
        excluded_categories=(
            "secrets and environment files",
            "OAuth and token files",
            "state, approval, tax-lot, reconciliation, and safety ledgers",
            "kill switch",
            "logs, caches, temporary backtests, and generated files",
        ),
    )
