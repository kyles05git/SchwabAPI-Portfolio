#!/usr/bin/env python
"""Preview, create, and inspect the Paper Sleeves First cohort.

The command is deliberately paper-only. It creates isolated ``PaperEngine`` records
through ``SleeveStore`` and persists complete strategy definitions made by the
central strategy registry. It never constructs a Schwab client, reads an account,
or calls an order, approval, cancellation, replacement, or reconciliation path.

Preview is the default safety posture: ``preview`` performs read-only EDGAR coverage
inspection and zero persistent writes. ``create`` prints the same complete preview
before it creates any record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from schwab_trader import (
    data_contracts,
    data_readiness,
    market_calendar,
    scheduling,
    strategy_registry,
    universes,
)
from schwab_trader.config import get_settings
from schwab_trader.experiments import ExperimentCohort, StrategyDefinition
from schwab_trader.sleeve_runs import SnapshotCoverage
from schwab_trader.sleeves import SleeveConfig
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

SCHEMA_VERSION = 1
STARTING_CASH = Decimal("10000.00")
SETTLEMENT_MODEL = "T+1"
LEVERAGE = Decimal("1")
BENCHMARK_SLEEVE = "bench-spy"
COST_MODEL_ID = "paper-engine-v1-no-modeled-cost"
DECISION_FREQUENCY = "daily"
DECISION_TIME = time(16, 0)
DECISION_SCHEDULE = "XNYS session close (13:00 ET early closes; otherwise 16:00 ET)"
VALUATION_SCHEDULE = DECISION_SCHEDULE
CADENCE = "every XNYS trading session"
COHORT_STATUS = "active"
COHORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CORE_MEMBER_NAMES = (
    "control-cash",
    "bench-spy",
    "sector-momentum",
    "trend-large",
    "low-vol-large",
    "momentum-large",
)
CONDITIONAL_MEMBER_NAME = "value-momentum-edgar"


class BootstrapConflictError(RuntimeError):
    """Existing immutable paper-cohort state differs from the requested definition."""


class ConditionalStatus(StrEnum):
    INCLUDED = "included"
    OMITTED = "omitted"


class ConditionalEdgarDecision(BaseModel):
    """Persisted explanation for the conditional first-cohort member."""

    model_config = ConfigDict(frozen=True)

    member: str = CONDITIONAL_MEMBER_NAME
    status: ConditionalStatus
    reason: str
    reason_codes: tuple[str, ...]
    coverage: float
    snapshot_id: str | None = None


class CohortBootstrapManifest(BaseModel):
    """Durable cohort metadata not represented by the sleeve registry columns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    cohort: ExperimentCohort
    valuation_schedule: str
    cadence: str
    benchmark_policy: str
    configuration_hashes: dict[str, str]
    conditional_edgar: ConditionalEdgarDecision


@dataclass(frozen=True)
class SleeveSpec:
    """One exact sleeve record to persist through ``SleeveStore``."""

    name: str
    role: str
    strategy: str
    universe_label: str
    universe: tuple[str, ...]
    max_positions: int
    max_position_fraction: Decimal
    factor: str
    definition: StrategyDefinition

    def preview(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "strategy": self.strategy,
            "universe_label": self.universe_label,
            "universe": list(self.universe),
            "starting_cash": str(STARTING_CASH),
            "settlement_model": SETTLEMENT_MODEL,
            "settlement_t1": True,
            "leverage": str(LEVERAGE),
            "max_positions": self.max_positions,
            "max_position_fraction": str(self.max_position_fraction),
            "factor": self.factor,
            "configuration_hash": self.definition.configuration_hash,
            "definition": self.definition.model_dump(mode="json"),
        }


@dataclass(frozen=True)
class BootstrapPlan:
    """Pure desired state plus the current conditional-readiness decision."""

    manifest: CohortBootstrapManifest
    selected_specs: tuple[SleeveSpec, ...]
    all_specs: tuple[SleeveSpec, ...]
    readiness: data_readiness.DataReadiness

    def preview(self, *, mode: str) -> dict[str, Any]:
        return {
            "mode": mode,
            "persistent_writes": "none" if mode == "dry-run" else "create-if-absent",
            "safety": {
                "paper_only": True,
                "brokerage_account_access": False,
                "synthetic_history_or_backfill": False,
                "capital_explanation": (
                    "$10,000 is assigned independently to every paper sleeve. "
                    "It is not divided from, reserved in, or linked to the real account."
                ),
            },
            "cohort": self.manifest.cohort.model_dump(mode="json"),
            "valuation_schedule": self.manifest.valuation_schedule,
            "cadence": self.manifest.cadence,
            "benchmark_policy": self.manifest.benchmark_policy,
            "cost_model_id": self.manifest.cohort.cost_model_id,
            "conditional_edgar": self.manifest.conditional_edgar.model_dump(mode="json"),
            "members": [spec.preview() for spec in self.selected_specs],
        }


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of applying an already-previewed plan."""

    manifest_path: Path
    created: tuple[str, ...]
    existing: tuple[str, ...]
    conditional_note: str


@dataclass(frozen=True)
class _Template:
    name: str
    role: str
    strategy: str
    universe_label: str
    max_positions: int
    max_position_fraction: Decimal
    factor: str = ""


_TEMPLATES = (
    _Template(
        "control-cash",
        "cash and accounting control",
        "hold",
        "SPY",
        1,
        Decimal("1"),
    ),
    _Template(
        "bench-spy",
        "common market benchmark",
        "buy-hold",
        "SPY",
        1,
        Decimal("1"),
    ),
    _Template(
        "sector-momentum",
        "liquid sector-rotation candidate",
        "momentum",
        "sector-etfs",
        8,
        Decimal("0.125"),
    ),
    _Template(
        "trend-large",
        "large-cap trend and risk-control candidate",
        "trend",
        "large-cap",
        8,
        Decimal("0.125"),
    ),
    _Template(
        "low-vol-large",
        "large-cap drawdown and diversification candidate",
        "low-vol",
        "large-cap",
        8,
        Decimal("0.125"),
    ),
    _Template(
        "momentum-large",
        "large-cap cross-sectional momentum candidate",
        "momentum",
        "large-cap",
        8,
        Decimal("0.125"),
    ),
    _Template(
        CONDITIONAL_MEMBER_NAME,
        "conditional EDGAR value-momentum challenger",
        "value-momentum",
        "large-cap",
        8,
        Decimal("0.125"),
        "earnings-yield",
    ),
)


def _universe(label: str) -> tuple[str, ...]:
    if label == "SPY":
        return ("SPY",)
    resolved = universes.get_preset(label)
    if resolved is None:  # pragma: no cover - templates are constants
        raise ValueError(f"Unknown universe preset {label!r}.")
    return tuple(resolved)


def _make_spec(template: _Template) -> SleeveSpec:
    symbols = _universe(template.universe_label)
    parameters = strategy_registry.sleeve_parameter_values(
        template.strategy,
        max_positions=template.max_positions,
        max_position_fraction=template.max_position_fraction,
        factor=template.factor,
    )
    definition = strategy_registry.make_definition(
        template.strategy,
        universe_definition={
            "preset": template.universe_label,
            "symbols": list(symbols),
        },
        parameters=parameters,
        strategy_version="1",
        benchmark_symbol_or_sleeve=BENCHMARK_SLEEVE,
        decision_frequency=DECISION_FREQUENCY,
        decision_time=DECISION_TIME,
        long_only=True,
        leverage_allowed=False,
    )
    return SleeveSpec(
        name=template.name,
        role=template.role,
        strategy=template.strategy,
        universe_label=template.universe_label,
        universe=symbols,
        max_positions=template.max_positions,
        max_position_fraction=template.max_position_fraction,
        factor=template.factor,
        definition=definition,
    )


def _readonly_edgar_probe(
    sec_db_path: Path,
    *,
    start_session: date,
    captured_at: datetime,
) -> tuple[data_readiness.SourceProbe, str]:
    """Inspect EDGAR coverage with SQLite read-only mode and no schema initialization."""

    if not sec_db_path.is_file():
        return data_readiness.SourceProbe.missing(), "EDGAR database is absent."
    try:
        uri = f"{sec_db_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sec_facts WHERE filed <= ?",
                    (start_session.isoformat(),),
                ).fetchone()[0]
            )
            covered = {
                str(row[0]).upper()
                for row in conn.execute(
                    "SELECT DISTINCT ticker FROM sec_facts WHERE filed <= ? ORDER BY ticker",
                    (start_session.isoformat(),),
                )
            }
        stat = sec_db_path.stat()
    except (OSError, sqlite3.DatabaseError):
        return (
            data_readiness.SourceProbe.of(None),
            "EDGAR database is unreadable or does not have the expected fact schema.",
        )

    identity = {
        "start_session": start_session.isoformat(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "total_facts": total,
        "covered": sorted(covered),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    batch = SnapshotCoverage(
        provenance=data_contracts.Provenance(
            source="sec-edgar-store",
            snapshot_id=f"sec-edgar:{digest}",
            retrieved_at=captured_at,
            as_of=captured_at,
            available_at=captured_at,
            timing=data_contracts.TimingPolicy.POINT_IN_TIME,
            vintage_safe=True,
        ),
        keys=frozenset(covered),
    )
    return data_readiness.SourceProbe.of(batch), f"EDGAR contains {total} eligible facts."


def _conditional_decision(
    readiness: data_readiness.DataReadiness,
    *,
    detail: str,
) -> ConditionalEdgarDecision:
    requirement = readiness.requirements[0]
    codes = tuple(reason.value for reason in requirement.reasons)
    if readiness.ready:
        reason = (
            f"{detail} Fundamental readiness passed for "
            f"{len(requirement.present_keys)}/{len(requirement.required_keys)} symbols."
        )
        status = ConditionalStatus.INCLUDED
    else:
        missing = (
            f" Missing symbols: {', '.join(requirement.missing_keys)}."
            if requirement.missing_keys
            else ""
        )
        reason = (
            f"{detail} Fundamental readiness failed ({', '.join(codes)}); "
            f"{CONDITIONAL_MEMBER_NAME} will be omitted. No price-only substitute "
            f"will be created.{missing}"
        )
        status = ConditionalStatus.OMITTED
    return ConditionalEdgarDecision(
        status=status,
        reason=reason,
        reason_codes=codes,
        coverage=requirement.coverage,
        snapshot_id=requirement.snapshot_id,
    )


def _validate_start_session(start_session: date, *, now_et: datetime) -> None:
    session = scheduling.session_for_date(start_session)
    if not session.is_trading_day:
        holiday = market_calendar.holiday_name(start_session)
        suffix = f" ({holiday})" if holiday else ""
        raise ValueError(f"{start_session.isoformat()} is not an XNYS trading session{suffix}.")
    if start_session < now_et.date():
        raise ValueError(
            "start_session must not be historical; choose an explicit current or "
            "future XNYS session. The bootstrap never backfills performance."
        )
    if (
        start_session == now_et.date()
        and session.close_et is not None
        and now_et >= session.close_et
    ):
        raise ValueError(
            "start_session has already closed; choose the next XNYS trading session "
            "instead of creating an ambiguous missed first observation."
        )


def build_bootstrap_plan(
    *,
    start_session: date,
    sec_db_path: Path,
    cohort_id: str | None = None,
    now: datetime | None = None,
    now_et: datetime | None = None,
    edgar_probe: data_readiness.SourceProbe | None = None,
) -> BootstrapPlan:
    """Build the exact first-cohort desired state without writing anything."""

    captured_at = (now or datetime.now(UTC)).astimezone(UTC)
    eastern_now = now_et or market_calendar.eastern_now()
    _validate_start_session(start_session, now_et=eastern_now)
    identity = cohort_id or f"paper-first-{start_session.isoformat()}"
    if not COHORT_ID_RE.fullmatch(identity):
        raise ValueError("cohort_id must contain only letters, digits, '-' or '_' (max 64).")

    specs = tuple(_make_spec(template) for template in _TEMPLATES)
    conditional = next(spec for spec in specs if spec.name == CONDITIONAL_MEMBER_NAME)
    probe_detail = "Injected offline readiness probe."
    probe = edgar_probe
    if probe is None:
        probe, probe_detail = _readonly_edgar_probe(
            sec_db_path,
            start_session=start_session,
            captured_at=captured_at,
        )
    readiness = data_readiness.evaluate_readiness(
        [
            (
                data_readiness.DataRequirement(
                    kind=data_readiness.DataKind.FUNDAMENTALS,
                    keys=conditional.universe,
                    require_vintage_safe=True,
                ),
                probe,
            )
        ],
        now=captured_at,
    )
    conditional_decision = _conditional_decision(readiness, detail=probe_detail)
    selected = tuple(
        spec for spec in specs if spec.name != CONDITIONAL_MEMBER_NAME or readiness.ready
    )
    cohort = ExperimentCohort(
        cohort_id=identity,
        name=f"Paper Sleeves First - {start_session.isoformat()}",
        created_at=captured_at,
        start_session=start_session,
        starting_cash_per_sleeve=STARTING_CASH,
        settlement_model=SETTLEMENT_MODEL,
        leverage=LEVERAGE,
        benchmark_sleeve=BENCHMARK_SLEEVE,
        decision_schedule=DECISION_SCHEDULE,
        cost_model_id=COST_MODEL_ID,
        member_sleeves=tuple(spec.name for spec in selected),
        status=COHORT_STATUS,
    )
    manifest = CohortBootstrapManifest(
        schema_version=SCHEMA_VERSION,
        cohort=cohort,
        valuation_schedule=VALUATION_SCHEDULE,
        cadence=CADENCE,
        benchmark_policy=(
            "bench-spy is the common sleeve benchmark; every definition references it."
        ),
        configuration_hashes={spec.name: spec.definition.configuration_hash for spec in selected},
        conditional_edgar=conditional_decision,
    )
    return BootstrapPlan(
        manifest=manifest,
        selected_specs=selected,
        all_specs=specs,
        readiness=readiness,
    )


def manifest_path(sleeves_dir: Path, cohort_id: str) -> Path:
    return sleeves_dir / "cohorts" / f"{cohort_id}.json"


def load_manifest(sleeves_dir: Path, cohort_id: str) -> CohortBootstrapManifest | None:
    """Load a stored manifest without creating a directory or database."""

    path = manifest_path(sleeves_dir, cohort_id)
    if not path.is_file():
        return None
    return CohortBootstrapManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _manifest_identity(manifest: CohortBootstrapManifest) -> dict[str, Any]:
    cohort = manifest.cohort.model_dump(mode="json", exclude={"created_at", "member_sleeves"})
    return {
        "schema_version": manifest.schema_version,
        "cohort": cohort,
        "valuation_schedule": manifest.valuation_schedule,
        "cadence": manifest.cadence,
        "benchmark_policy": manifest.benchmark_policy,
    }


def _expected_config(spec: SleeveSpec, cohort_id: str) -> dict[str, Any]:
    return {
        "name": spec.name,
        "strategy": spec.strategy,
        "universe": list(spec.universe),
        "starting_cash": STARTING_CASH,
        "max_positions": spec.max_positions,
        "max_position_fraction": spec.max_position_fraction,
        "settlement_t1": True,
        "leverage": LEVERAGE,
        "factor": spec.factor,
        "definition": spec.definition,
        "cohort_id": cohort_id,
        "configuration_hash": spec.definition.configuration_hash,
        "decision_frequency": spec.definition.decision_frequency,
        "decision_time": spec.definition.decision_time.isoformat(),
    }


def _config_differences(
    config: SleeveConfig,
    spec: SleeveSpec,
    cohort_id: str,
) -> list[str]:
    expected = _expected_config(spec, cohort_id)
    actual = {key: getattr(config, key) for key in expected}
    return [key for key, value in expected.items() if actual[key] != value]


def _write_manifest(path: Path, manifest: CohortBootstrapManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(manifest.model_dump_json(indent=2))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _stored_specs(
    manifest: CohortBootstrapManifest,
    all_specs: tuple[SleeveSpec, ...],
) -> tuple[SleeveSpec, ...]:
    by_name = {spec.name: spec for spec in all_specs}
    unknown = [name for name in manifest.cohort.member_sleeves if name not in by_name]
    if unknown:
        raise BootstrapConflictError(
            f"Stored cohort has unknown members {unknown}; create a new cohort."
        )
    specs = tuple(by_name[name] for name in manifest.cohort.member_sleeves)
    expected_hashes = {spec.name: spec.definition.configuration_hash for spec in specs}
    if manifest.configuration_hashes != expected_hashes:
        raise BootstrapConflictError(
            "Stored cohort configuration hashes differ from the current complete "
            "definitions; create a new cohort instead of mutating this one."
        )
    return specs


def apply_bootstrap(
    plan: BootstrapPlan,
    *,
    sleeves_dir: Path,
    require_edgar: bool = False,
) -> BootstrapResult:
    """Create the plan idempotently after the caller has rendered its preview."""

    if require_edgar and not plan.readiness.ready:
        raise BootstrapConflictError(
            f"{CONDITIONAL_MEMBER_NAME} is required but EDGAR readiness failed: "
            f"{plan.manifest.conditional_edgar.reason}"
        )

    settings = get_settings().model_copy(update={"sleeves_dir": sleeves_dir})
    store = storage_factory.sleeve_store(settings)
    cohort_id = plan.manifest.cohort.cohort_id
    path = manifest_path(sleeves_dir, cohort_id)
    if isinstance(store, SqlAlchemySleeveStore):
        stored_payload = store.cohort_manifest(cohort_id)
        stored = (
            CohortBootstrapManifest.model_validate(stored_payload)
            if stored_payload is not None
            else None
        )
    else:
        stored = load_manifest(sleeves_dir, cohort_id)
    if stored is not None and _manifest_identity(stored) != _manifest_identity(plan.manifest):
        raise BootstrapConflictError(
            f"Cohort {cohort_id!r} already exists with conflicting immutable settings. "
            "Choose a new --cohort-id; the existing cohort was not changed."
        )

    configs = store.list()
    cohort_configs = [config for config in configs if config.cohort_id == cohort_id]

    if stored is None:
        if cohort_configs:
            raise BootstrapConflictError(
                f"Cohort {cohort_id!r} has sleeve records but no bootstrap manifest. "
                "It cannot be adopted safely; choose a new --cohort-id."
            )
        specs = plan.selected_specs
        manifest = plan.manifest
    else:
        specs = _stored_specs(stored, plan.all_specs)
        manifest = stored

    expected_names = {spec.name for spec in specs}
    unexpected = sorted(
        config.name for config in cohort_configs if config.name not in expected_names
    )
    if unexpected:
        raise BootstrapConflictError(
            f"Cohort {cohort_id!r} contains unexpected members {unexpected}. "
            "Choose a new cohort; no running cohort is mutated."
        )

    conflicts: dict[str, list[str]] = {}
    existing: list[str] = []
    missing: list[SleeveSpec] = []
    for spec in specs:
        config = store.resolve(spec.name, cohort_id=cohort_id)
        if config is None:
            missing.append(spec)
            continue
        differences = _config_differences(config, spec, cohort_id)
        if differences:
            conflicts[spec.name] = differences
        else:
            existing.append(spec.name)
    if conflicts:
        details = "; ".join(
            f"{name}: {', '.join(fields)}" for name, fields in sorted(conflicts.items())
        )
        raise BootstrapConflictError(
            f"Existing sleeve configuration conflicts with the requested immutable "
            f"cohort ({details}). Choose a new --cohort-id; nothing was changed."
        )
    if stored is not None and missing:
        raise BootstrapConflictError(
            f"Stored cohort {cohort_id!r} is missing members "
            f"{[spec.name for spec in missing]}. Do not repair a potentially running "
            "cohort in place; investigate and create a new cohort if necessary."
        )

    created: list[str] = []
    created_ids: list[str] = []
    try:
        for spec in missing:
            config = store.create(
                spec.name,
                strategy=spec.strategy,
                universe=list(spec.universe),
                starting_cash=STARTING_CASH,
                max_positions=spec.max_positions,
                max_position_fraction=spec.max_position_fraction,
                settlement_t1=True,
                leverage=LEVERAGE,
                factor=spec.factor,
                definition=spec.definition,
                cohort_id=cohort_id,
            )
            created.append(spec.name)
            created_ids.append(config.identity)
        if stored is None:
            if isinstance(store, SqlAlchemySleeveStore):
                store.upsert_cohort_manifest(
                    manifest.model_dump(mode="json"),
                )
            else:
                _write_manifest(path, manifest)
    except Exception:
        rollback_targets = (
            created_ids
            if isinstance(store, SqlAlchemySleeveStore)
            else created
        )
        for reference in reversed(rollback_targets):
            store.remove(reference)
        raise

    if stored is not None:
        note = (
            f"Stored conditional decision remains {stored.conditional_edgar.status.value}; "
            "readiness changes do not mutate this cohort."
        )
    else:
        note = manifest.conditional_edgar.reason
    return BootstrapResult(
        manifest_path=(
            Path("shared-storage") / "cohorts" / cohort_id
            if isinstance(store, SqlAlchemySleeveStore)
            else path
        ),
        created=tuple(created),
        existing=tuple(existing),
        conditional_note=note,
    )


def inspect_cohort(*, sleeves_dir: Path, cohort_id: str) -> dict[str, Any]:
    """Return exact stored cohort metadata, definitions, and denormalized hashes."""

    settings = get_settings().model_copy(update={"sleeves_dir": sleeves_dir})
    store = storage_factory.sleeve_store(settings)
    if isinstance(store, SqlAlchemySleeveStore):
        payload = store.cohort_manifest(cohort_id)
        manifest = (
            CohortBootstrapManifest.model_validate(payload)
            if payload is not None
            else None
        )
    else:
        manifest = load_manifest(sleeves_dir, cohort_id)
    if manifest is None:
        raise FileNotFoundError(f"No bootstrap manifest exists for cohort {cohort_id!r}.")
    records: list[dict[str, Any]] = []
    for name in manifest.cohort.member_sleeves:
        config = store.resolve(name, cohort_id=cohort_id)
        if config is None:
            records.append({"name": name, "missing": True})
            continue
        records.append(
            {
                "name": config.name,
                "cohort_id": config.cohort_id,
                "strategy": config.strategy,
                "universe": config.universe,
                "starting_cash": str(config.starting_cash),
                "settlement_t1": config.settlement_t1,
                "leverage": str(config.leverage),
                "configuration_hash": config.configuration_hash,
                "definition": (
                    config.definition.model_dump(mode="json")
                    if config.definition is not None
                    else None
                ),
            }
        )
    return {
        "manifest_path": str(
            Path("shared-storage") / "cohorts" / cohort_id
            if isinstance(store, SqlAlchemySleeveStore)
            else manifest_path(sleeves_dir, cohort_id)
        ),
        "manifest": manifest.model_dump(mode="json"),
        "stored_sleeves": records,
    }


def _add_storage_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sleeves-dir",
        type=Path,
        default=Path("./data/sleeves"),
        help="Paper sleeve directory (default: ./data/sleeves).",
    )
    parser.add_argument(
        "--sec-db",
        type=Path,
        default=Path("./data/sec.sqlite3"),
        help="Existing local EDGAR fact database (read-only during preview).",
    )


def _add_plan_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--start-session",
        type=date.fromisoformat,
        required=True,
        metavar="YYYY-MM-DD",
        help="Explicit current/future XNYS trading session. Historical dates are rejected.",
    )
    parser.add_argument(
        "--cohort-id",
        default="",
        help="Stable cohort ID (default: paper-first-<start-session>).",
    )
    parser.add_argument(
        "--require-edgar",
        action="store_true",
        help="Fail instead of omitting value-momentum-edgar when readiness is false.",
    )
    _add_storage_options(parser)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely bootstrap the first parallel hypothetical paper cohort."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser(
        "preview",
        aliases=["dry-run"],
        help="Print the complete desired state and perform zero persistent writes.",
    )
    _add_plan_options(preview)
    create = commands.add_parser(
        "create",
        help="Print the complete preview, then idempotently create missing records.",
    )
    _add_plan_options(create)
    inspect = commands.add_parser(
        "inspect",
        help="Print the exact stored manifest, definitions, and hashes.",
    )
    inspect.add_argument("--cohort-id", required=True)
    _add_storage_options(inspect)
    return parser


def _render(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            _render(inspect_cohort(sleeves_dir=args.sleeves_dir, cohort_id=args.cohort_id))
            return 0

        plan = build_bootstrap_plan(
            start_session=args.start_session,
            sec_db_path=args.sec_db,
            cohort_id=args.cohort_id or None,
        )
        mode = "apply" if args.command == "create" else "dry-run"
        _render(plan.preview(mode=mode))
        if args.command != "create":
            if args.require_edgar and not plan.readiness.ready:
                print(
                    f"BLOCKED: {plan.manifest.conditional_edgar.reason}",
                    file=sys.stderr,
                )
                return 2
            print("DRY RUN COMPLETE: zero persistent writes were performed.")
            return 0

        result = apply_bootstrap(
            plan,
            sleeves_dir=args.sleeves_dir,
            require_edgar=args.require_edgar,
        )
        _render(
            {
                "result": "created" if result.created else "already-exists",
                "created": list(result.created),
                "existing": list(result.existing),
                "manifest_path": str(result.manifest_path),
                "conditional_note": result.conditional_note,
                "safety": (
                    "Paper records only; no brokerage account or live-trading function "
                    "was accessed."
                ),
            }
        )
        return 0
    except (
        BootstrapConflictError,
        FileNotFoundError,
        OSError,
        sqlite3.DatabaseError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
