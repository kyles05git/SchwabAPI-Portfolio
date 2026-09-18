"""Preview, create, and inspect the immutable five-sleeve ``challenger-v1`` cohort.

This is the bootstrap for the second official paper cohort. It is a *sibling* of
``scripts/bootstrap_paper_cohort.py``, never a replacement: that script owns
``paper-first-2026-07-27`` and ``paper-first-2026-07-28``, whose templates, identities,
definitions, and hashes must not move. Nothing here reads, writes, renames, supersedes,
or reuses either of them, and :data:`~schwab_trader.execution_timing.PROTECTED_LEGACY_COHORTS`
is checked explicitly so a mistyped id cannot reach one.

Every number comes from :mod:`schwab_trader.strategies.contract` — the frozen
``challenger-v1`` specification reviewed in #91. Nothing is re-typed and nothing is
retuned here; this module decides *identity and persistence*, not policy.

The five sleeves, each an independently funded $10,000 paper account:

======================================= ======================== ==================
sleeve                                  strategy                 rebalance cadence
======================================= ======================== ==================
``control-cash``                        registered ``hold``      never trades
``bench-spy``                           registered ``buy-hold``  buys once, holds
``dual-momentum-v1``                    #92                      monthly
``quality-profitability-v1``            #93                      monthly
``short-term-mean-reversion-v1``        #94                      daily
======================================= ======================== ==================

Five sleeves at $10,000 each is five separate simulations, **not** $50,000 of pooled or real
capital. None of it is reserved in, divided from, or linked to the real brokerage
account, and no code path here constructs a Schwab client, reads an account, or touches
an order, approval, cancellation, or reconciliation path.

Safety posture, in the order the operations are meant to be used:

:func:`build_plan`
    Pure. No I/O, no storage, no clock beyond the injected ``now``. Produces the exact
    desired state so it can be reviewed before anything exists.
:func:`preview`
    Read-only rendering of a plan. Performs **zero** persistent writes, so running it
    repeatedly is free and changes nothing.
:func:`create`
    Idempotent create-if-absent. Every conflict is detected *before* the first record is
    written, and a failure part-way through rolls back what this call created — so the
    operation either produces the whole cohort or produces nothing.

There is deliberately no force, bypass, repair, partial-create, or backfill option. A
cohort whose stored configuration disagrees with the requested one is a new experiment
and needs a new id; mutating a running experiment in place is the failure mode this
module exists to prevent.

**Shared storage is required to run this cohort alongside July 28.** ``challenger-v1``
freezes two sleeve names, ``control-cash`` and ``bench-spy``, that
``paper-first-2026-07-28`` also uses. The shared registry scopes a sleeve by
``(namespace, cohort, name)`` and keeps both cohorts' records distinct; the local SQLite
registry keys sleeves by ``name`` alone and is a single-cohort layout. Renaming the
challenger sleeves is not an option — the names are part of the frozen contract, and
``bench-spy`` is the benchmark every member definition references — so
:func:`create` reports the collision as a precondition and points at
``SCHWAB_DATABASE_URL``. Creating this cohort in an *empty* local store still works and
is what the offline tests use.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from schwab_trader import (
    challenger_strategies,
    cohort_lifecycle,
    execution_timing,
    market_calendar,
    scheduling,
    strategy_registry,
)
from schwab_trader.challenger_strategies import RebalanceCadence
from schwab_trader.experiments import ExperimentCohort, StrategyDefinition
from schwab_trader.sleeves import SleeveConfig, SleeveStore
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore
from schwab_trader.strategies import contract

__all__ = [
    "COHORT_ID_PREFIX",
    "SCHEMA_VERSION",
    "ChallengerCohortManifest",
    "ChallengerConflictError",
    "ChallengerPlan",
    "ChallengerSleeveSpec",
    "CreateResult",
    "build_plan",
    "create",
    "default_cohort_id",
    "inspect_cohort",
    "preview",
]

#: Schema version of :class:`ChallengerCohortManifest`.
SCHEMA_VERSION = 1

#: Cohort ids default to ``challenger-v1-<start-session>``. Deliberately unlike
#: ``paper-first-*`` so the two experiments can never be confused in a log line, a
#: dashboard row, or a scheduler argument.
COHORT_ID_PREFIX = contract.CONTRACT_ID

_COHORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: The frozen execution methodology reviewed in #79: decide on session T's close,
#: execute and mark at the T+1 opening print. Named rather than re-derived, so the
#: cohort records the exact methodology hash it ran under.
METHODOLOGY = execution_timing.SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1

_VALUATION_SCHEDULE = contract.VALUATION_BASIS
_DECISION_SCHEDULE = contract.SIGNAL_BASIS

#: Which registered strategy implements each frozen sleeve, and how often it may trade.
#: ``control-cash`` and ``bench-spy`` reuse the long-registered ``hold`` and ``buy-hold``
#: strategies exactly as the contract specifies, so their cadence is a property of the
#: implementation (``hold`` proposes nothing, ever; ``buy-hold`` proposes nothing once it
#: is invested) rather than a parameter. Neither accepts parameters at all, which is why
#: their cadence is recorded in the manifest instead of in their definitions.
_IMPLEMENTATION: dict[str, tuple[str, RebalanceCadence]] = {
    "control-cash": ("hold", RebalanceCadence.NEVER),
    "bench-spy": ("buy-hold", RebalanceCadence.BUY_ONCE),
    "dual-momentum-v1": ("dual-momentum-v1", RebalanceCadence.MONTHLY),
    "quality-profitability-v1": ("quality-profitability-v1", RebalanceCadence.MONTHLY),
    "short-term-mean-reversion-v1": (
        "short-term-mean-reversion-v1",
        RebalanceCadence.DAILY,
    ),
}


class ChallengerConflictError(RuntimeError):
    """Existing immutable cohort state differs from the requested definition.

    Always raised *before* anything is written, or after a rollback. It never leaves a
    partially created cohort behind.
    """


# --- desired state -----------------------------------------------------------


@dataclass(frozen=True)
class ChallengerSleeveSpec:
    """One exact sleeve record to persist, derived wholly from the frozen contract."""

    name: str
    role: str
    strategy: str
    cadence: RebalanceCadence
    universe_label: str
    universe: tuple[str, ...]
    max_positions: int
    max_position_fraction: Decimal
    definition: StrategyDefinition

    @property
    def configuration_hash(self) -> str:
        return self.definition.configuration_hash

    def payload(self) -> dict[str, Any]:
        """The sanitized preview/inspection view of this sleeve."""
        return {
            "name": self.name,
            "role": self.role,
            "strategy": self.strategy,
            "rebalance_cadence": self.cadence.value,
            "universe_label": self.universe_label,
            "universe": list(self.universe),
            "universe_size": len(self.universe),
            "starting_cash": str(contract.STARTING_CASH_PER_SLEEVE),
            "settlement_model": contract.SETTLEMENT_MODEL,
            "settlement_t1": True,
            "leverage": str(contract.LEVERAGE),
            "gross_exposure_cap": str(contract.GROSS_EXPOSURE_CAP),
            "max_positions": self.max_positions,
            "max_position_fraction": str(self.max_position_fraction),
            "execution_methodology": METHODOLOGY.key,
            "decision_frequency": self.definition.decision_frequency,
            "decision_time": self.definition.decision_time.isoformat(),
            "data_requirements": list(self.definition.data_requirements),
            "configuration_hash": self.configuration_hash,
            "definition": self.definition.model_dump(mode="json"),
        }


class ChallengerCohortManifest(BaseModel):
    """Durable cohort metadata the sleeve registry's own columns cannot express.

    Frozen and hash-compared on every :func:`create`, so a second create against an
    existing cohort must describe byte-identically the same experiment or be rejected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    cohort: ExperimentCohort

    #: Identity of the frozen experiment specification these sleeves were cut from.
    contract_id: str
    contract_version: str
    contract_hash: str
    """SHA-256 over the *whole* frozen contract. Recording it means a later edit to any
    frozen value is detectable from the persisted cohort alone, without re-deriving the
    sleeves."""

    #: Identity of the reviewed T/T+1 timing model from #79.
    execution_methodology: str
    methodology_hash: str

    decision_schedule: str
    valuation_schedule: str
    execution_schedule: str
    #: sleeve name -> rebalance cadence. Recorded for all five, including the two whose
    #: cadence is implemented by the strategy rather than carried as a parameter.
    cadences: dict[str, str]
    #: sleeve name -> configuration hash of its persisted definition.
    configuration_hashes: dict[str, str]

    benchmark_policy: str
    cost_model_id: str
    cost_bps_per_side: str
    cost_bps_round_trip: str
    gross_exposure_cap: str
    long_only: bool
    leverage_allowed: bool
    whole_shares_only: bool
    max_price_staleness_sessions: int
    conflicting_evidence_fails_sleeve: bool
    interpretation: str
    known_limitations: tuple[str, ...]


@dataclass(frozen=True)
class ChallengerPlan:
    """The complete desired state. Pure data — building one writes nothing."""

    manifest: ChallengerCohortManifest
    specs: tuple[ChallengerSleeveSpec, ...]

    @property
    def cohort_id(self) -> str:
        return self.manifest.cohort.cohort_id

    @property
    def start_session(self) -> date:
        return self.manifest.cohort.start_session

    def payload(self, *, mode: str) -> dict[str, Any]:
        """The full sanitized preview. Identical for ``preview`` and ``create``."""
        return {
            "mode": mode,
            "persistent_writes": "none" if mode == "preview" else "create-if-absent",
            "safety": {
                "paper_only": True,
                "brokerage_account_access": False,
                "order_path_access": False,
                "synthetic_history_or_backfill": False,
                "capital_explanation": (
                    f"${contract.STARTING_CASH_PER_SLEEVE} is assigned independently to "
                    f"each of the {contract.SLEEVE_COUNT} paper sleeves. That is "
                    f"{contract.SLEEVE_COUNT} separate simulations, not "
                    f"${contract.STARTING_CASH_PER_SLEEVE * contract.SLEEVE_COUNT} of "
                    "pooled capital, and none of it is reserved in, divided from, or "
                    "linked to the real brokerage account."
                ),
                "isolation": (
                    "Every sleeve has its own paper account. Cash, positions, fills, and "
                    "observations are never shared between sleeves or with any other cohort."
                ),
            },
            "cohort": self.manifest.cohort.model_dump(mode="json"),
            "contract": {
                "contract_id": self.manifest.contract_id,
                "contract_version": self.manifest.contract_version,
                "contract_hash": self.manifest.contract_hash,
            },
            "timing": {
                "execution_methodology": self.manifest.execution_methodology,
                "methodology_hash": self.manifest.methodology_hash,
                "decision_schedule": self.manifest.decision_schedule,
                "execution_schedule": self.manifest.execution_schedule,
                "valuation_schedule": self.manifest.valuation_schedule,
            },
            "costs": {
                "cost_model_id": self.manifest.cost_model_id,
                "bps_per_side": self.manifest.cost_bps_per_side,
                "bps_round_trip": self.manifest.cost_bps_round_trip,
            },
            "limits": {
                "gross_exposure_cap": self.manifest.gross_exposure_cap,
                "long_only": self.manifest.long_only,
                "leverage_allowed": self.manifest.leverage_allowed,
                "whole_shares_only": self.manifest.whole_shares_only,
                "max_price_staleness_sessions": self.manifest.max_price_staleness_sessions,
                "conflicting_evidence_fails_sleeve": (
                    self.manifest.conflicting_evidence_fails_sleeve
                ),
            },
            "benchmark_policy": self.manifest.benchmark_policy,
            "cadences": dict(self.manifest.cadences),
            "interpretation": self.manifest.interpretation,
            "known_limitations": list(self.manifest.known_limitations),
            "members": [spec.payload() for spec in self.specs],
        }


# --- plan construction -------------------------------------------------------


def default_cohort_id(start_session: date) -> str:
    """The conventional id for a challenger cohort starting on ``start_session``."""
    return f"{COHORT_ID_PREFIX}-{start_session.isoformat()}"


def _validate_cohort_id(cohort_id: str) -> str:
    identity = cohort_id.strip()
    if not _COHORT_ID_RE.fullmatch(identity):
        raise ValueError("cohort_id must contain only letters, digits, '-' or '_' (max 64).")
    if identity in execution_timing.PROTECTED_LEGACY_COHORTS:
        raise ValueError(
            f"{identity!r} is an existing official experiment recorded under a different "
            "execution methodology. It must not be redefined, reused, or superseded by "
            "this bootstrap; choose a new cohort id."
        )
    if cohort_lifecycle.is_historical(identity):
        raise ValueError(
            f"{identity!r} names a superseded cohort whose records are incident evidence. "
            "Choose a new cohort id; a withdrawn experiment is never restarted in place."
        )
    return identity


def _validate_start_session(start_session: date, *, now_et: datetime) -> None:
    """Require an explicit, eligible, not-yet-closed XNYS session. Never backfills.

    Identical in intent to the rule ``scripts/bootstrap_paper_cohort.py`` applies to the
    July cohorts: a cohort may only start on a session whose evidence has not yet been
    produced, so its first observation is genuinely collected rather than reconstructed.
    """
    session = scheduling.session_for_date(start_session)
    if not session.is_trading_day:
        holiday = market_calendar.holiday_name(start_session)
        suffix = f" ({holiday})" if holiday else ""
        raise ValueError(f"{start_session.isoformat()} is not an XNYS trading session{suffix}.")
    if start_session < now_et.date():
        raise ValueError(
            f"{start_session.isoformat()} is historical. Choose a current or future XNYS "
            "session; the challenger bootstrap never backfills performance."
        )
    if (
        start_session == now_et.date()
        and session.close_et is not None
        and now_et >= session.close_et
    ):
        raise ValueError(
            f"{start_session.isoformat()} has already closed. Choose the next XNYS session "
            "rather than creating an ambiguous missed first observation."
        )


def _universe_definition(sleeve: contract.ChallengerSleeve, symbols: tuple[str, ...]) -> dict[
    str, Any
]:
    """The stored, self-describing universe identity for one sleeve.

    Carries the label *and* the exact symbol list snapshotted at freeze time. A later
    edit to a mutable preset therefore cannot retroactively redefine a running sleeve,
    and the reader never has to resolve a preset to know what was traded. The defensive
    asset is named separately because it is not a ranking candidate.
    """
    payload: dict[str, Any] = {
        "preset": sleeve.universe_label,
        "symbols": list(symbols),
    }
    if sleeve.defensive_universe:
        payload["risk_symbols"] = list(sleeve.universe)
        payload["defensive_symbols"] = list(sleeve.defensive_universe)
    return payload


def _spec_for(sleeve: contract.ChallengerSleeve) -> ChallengerSleeveSpec:
    """Build one sleeve's exact record from its frozen contract entry."""
    strategy, cadence = _IMPLEMENTATION[sleeve.name]

    # The traded universe includes the defensive asset: the sleeve may have to buy it,
    # so the cohort snapshot must fetch its quotes, history, and T+1 opening bar. The
    # ranking universe stays what the contract says it is — the strategy reads that from
    # the contract, not from this list.
    symbols = tuple(sleeve.universe) + tuple(sleeve.defensive_universe)

    if strategy_registry.entry(strategy).parameters:
        parameters = challenger_strategies.frozen_parameters(sleeve, cadence=cadence)
    else:
        # `hold` and `buy-hold` accept no parameters at all. Their cadence is recorded in
        # the manifest instead of being invented as a parameter the strategy would reject.
        parameters = {}

    definition = strategy_registry.make_definition(
        strategy,
        universe_definition=_universe_definition(sleeve, symbols),
        parameters=parameters,
        strategy_version=sleeve.version,
        benchmark_symbol_or_sleeve=contract.BENCHMARK_SLEEVE,
        decision_frequency=contract.DECISION_FREQUENCY,
        decision_time=contract.SIGNAL_SESSION_TIME,
        long_only=contract.LONG_ONLY,
        leverage_allowed=contract.LEVERAGE_ALLOWED,
    )
    return ChallengerSleeveSpec(
        name=sleeve.name,
        role=sleeve.role,
        strategy=strategy,
        cadence=cadence,
        universe_label=sleeve.universe_label,
        universe=symbols,
        max_positions=sleeve.max_positions,
        max_position_fraction=sleeve.max_position_fraction,
        definition=definition,
    )


def build_plan(
    *,
    start_session: date,
    cohort_id: str | None = None,
    now: datetime | None = None,
    now_et: datetime | None = None,
) -> ChallengerPlan:
    """Build the exact challenger-v1 desired state. Pure: writes nothing, reads nothing.

    ``now`` stamps the cohort's ``created_at``; ``now_et`` is the Eastern wall clock the
    start session is judged against. Both are injectable so the plan is deterministic
    under test and so this function never reads a clock a caller cannot control.
    """
    captured_at = (now or datetime.now(UTC)).astimezone(UTC)
    eastern_now = now_et or market_calendar.eastern_now()
    _validate_start_session(start_session, now_et=eastern_now)
    identity = _validate_cohort_id(cohort_id or default_cohort_id(start_session))

    specs = tuple(_spec_for(sleeve) for sleeve in contract.SLEEVES)
    # The contract says five; a plan that produced any other number would be a different
    # experiment, so this fails closed rather than creating whatever it happened to build.
    if len(specs) != contract.SLEEVE_COUNT:
        raise ValueError(
            f"challenger-v1 specifies {contract.SLEEVE_COUNT} sleeves but the plan built "
            f"{len(specs)}."
        )

    cohort = ExperimentCohort(
        cohort_id=identity,
        name=f"Challenger v1 - {start_session.isoformat()}",
        created_at=captured_at,
        start_session=start_session,
        starting_cash_per_sleeve=contract.STARTING_CASH_PER_SLEEVE,
        settlement_model=contract.SETTLEMENT_MODEL,
        leverage=contract.LEVERAGE,
        benchmark_sleeve=contract.BENCHMARK_SLEEVE,
        decision_schedule=_DECISION_SCHEDULE,
        cost_model_id=contract.COST_MODEL_ID,
        member_sleeves=tuple(spec.name for spec in specs),
        status="active",
    )
    manifest = ChallengerCohortManifest(
        schema_version=SCHEMA_VERSION,
        cohort=cohort,
        contract_id=contract.CONTRACT_ID,
        contract_version=contract.CONTRACT_VERSION,
        contract_hash=contract.contract_hash(),
        execution_methodology=METHODOLOGY.key,
        methodology_hash=METHODOLOGY.methodology_hash,
        decision_schedule=_DECISION_SCHEDULE,
        valuation_schedule=_VALUATION_SCHEDULE,
        execution_schedule=contract.EXECUTION_BASIS,
        cadences={spec.name: spec.cadence.value for spec in specs},
        configuration_hashes={spec.name: spec.configuration_hash for spec in specs},
        benchmark_policy=(
            f"{contract.BENCHMARK_SLEEVE} is the common sleeve benchmark; every member "
            "definition references it, and it is itself a funded member charged the same "
            "modeled costs as every challenger."
        ),
        cost_model_id=contract.COST_MODEL_ID,
        cost_bps_per_side=str(contract.COST_BPS_PER_SIDE),
        cost_bps_round_trip=str(contract.COST_BPS_ROUND_TRIP),
        gross_exposure_cap=str(contract.GROSS_EXPOSURE_CAP),
        long_only=contract.LONG_ONLY,
        leverage_allowed=contract.LEVERAGE_ALLOWED,
        whole_shares_only=contract.WHOLE_SHARES_ONLY,
        max_price_staleness_sessions=contract.MAX_PRICE_STALENESS_SESSIONS,
        conflicting_evidence_fails_sleeve=contract.CONFLICTING_EVIDENCE_FAILS_SLEEVE,
        interpretation=contract.INTERPRETATION,
        known_limitations=contract.KNOWN_LIMITATIONS,
    )
    return ChallengerPlan(manifest=manifest, specs=specs)


def preview(plan: ChallengerPlan) -> dict[str, Any]:
    """Render the complete desired state. Read-only: performs zero persistent writes."""
    return plan.payload(mode="preview")


# --- persistence -------------------------------------------------------------


@dataclass(frozen=True)
class CreateResult:
    """What :func:`create` actually did."""

    cohort_id: str
    created: tuple[str, ...]
    existing: tuple[str, ...]
    manifest_location: str

    @property
    def already_complete(self) -> bool:
        """True when every member was already present and nothing was written."""
        return not self.created

    def payload(self) -> dict[str, Any]:
        return {
            "result": "created" if self.created else "already-exists",
            "cohort_id": self.cohort_id,
            "created": list(self.created),
            "existing": list(self.existing),
            "manifest_location": self.manifest_location,
            "safety": (
                "Paper records only. No brokerage account, order path, or live-trading "
                "function was accessed."
            ),
        }


def manifest_path(sleeves_dir: Path, cohort_id: str) -> Path:
    """Where a local (file-backed) store keeps a cohort manifest."""
    return sleeves_dir / "cohorts" / f"{cohort_id}.json"


def _read_manifest(store: SleeveStore, cohort_id: str) -> ChallengerCohortManifest | None:
    """The stored manifest for ``cohort_id``, or ``None``. Never creates anything."""
    if isinstance(store, SqlAlchemySleeveStore):
        payload = store.cohort_manifest(cohort_id)
        return None if payload is None else ChallengerCohortManifest.model_validate(payload)
    path = manifest_path(store.dir, cohort_id)
    if not path.is_file():
        return None
    return ChallengerCohortManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _write_manifest(store: SleeveStore, manifest: ChallengerCohortManifest) -> str:
    """Persist the manifest and return its operator-facing location."""
    cohort_id = manifest.cohort.cohort_id
    if isinstance(store, SqlAlchemySleeveStore):
        store.upsert_cohort_manifest(manifest.model_dump(mode="json"))
        return f"shared-storage/cohorts/{cohort_id}"
    path = manifest_path(store.dir, cohort_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same atomic strategy the token store and the July bootstrap use: write a sibling
    # temporary file, flush it to disk, then rename over the target. A crash leaves
    # either the old manifest or the new one, never a truncated file.
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
    return str(path)


def _identity(manifest: ChallengerCohortManifest) -> dict[str, Any]:
    """The immutable part of a manifest, for conflict comparison.

    ``created_at`` is excluded because it records when somebody typed the command, not
    what the experiment is. Everything else — including every configuration hash, the
    contract hash, and the methodology hash — must match exactly.
    """
    payload = manifest.model_dump(mode="json")
    cohort = dict(payload["cohort"])
    cohort.pop("created_at", None)
    payload["cohort"] = cohort
    return payload


def _stored_differences(config: SleeveConfig, spec: ChallengerSleeveSpec, cohort_id: str) -> list[
    str
]:
    """Field names on which an existing sleeve disagrees with the requested one."""
    expected: dict[str, object] = {
        "name": spec.name,
        "strategy": spec.strategy,
        "universe": list(spec.universe),
        "starting_cash": contract.STARTING_CASH_PER_SLEEVE,
        "max_positions": spec.max_positions,
        "max_position_fraction": spec.max_position_fraction,
        "settlement_t1": True,
        "leverage": contract.LEVERAGE,
        "definition": spec.definition,
        "cohort_id": cohort_id,
        "configuration_hash": spec.configuration_hash,
        "decision_frequency": spec.definition.decision_frequency,
        "decision_time": spec.definition.decision_time.isoformat(),
        "execution_methodology": METHODOLOGY.key,
    }
    return [key for key, value in expected.items() if getattr(config, key) != value]


def create(plan: ChallengerPlan, *, store: SleeveStore) -> CreateResult:
    """Create the previewed cohort. Atomic, idempotent, and create-if-absent.

    The order is what makes it safe:

    1. Read the stored manifest, if any, and reject a conflicting one before touching
       any sleeve record.
    2. Reject any member of this cohort that is not part of the plan.
    3. Compare *every* planned member against what is stored and collect all conflicts,
       so a disagreement is reported before the first write rather than after four
       sleeves already exist.
    4. Only then create the missing sleeves, rolling back everything this call created
       if any single creation fails.

    Repeated calls with the same plan are a no-op that reports ``already-exists``. There
    is no flag that repairs, replaces, or partially creates a cohort.
    """
    cohort_id = plan.cohort_id
    stored = _read_manifest(store, cohort_id)
    if stored is not None and _identity(stored) != _identity(plan.manifest):
        raise ChallengerConflictError(
            f"Cohort {cohort_id!r} already exists with conflicting immutable settings. "
            "Choose a new cohort id; the existing cohort was not changed."
        )

    configs = store.list()
    members = [config for config in configs if config.cohort_id == cohort_id]
    expected_names = {spec.name for spec in plan.specs}

    if stored is None and members:
        raise ChallengerConflictError(
            f"Cohort {cohort_id!r} has sleeve records but no manifest, so it cannot be "
            "adopted safely. Choose a new cohort id; nothing was changed."
        )

    unexpected = sorted(config.name for config in members if config.name not in expected_names)
    if unexpected:
        raise ChallengerConflictError(
            f"Cohort {cohort_id!r} contains members that are not part of challenger-v1 "
            f"({', '.join(unexpected)}). No running cohort is mutated; choose a new id."
        )

    _require_scoped_storage(store, configs, cohort_id=cohort_id, expected=expected_names)

    # Every conflict is collected before anything is written. Reporting them one at a
    # time would mean the operator fixes one, re-runs, and discovers the next — with
    # sleeves created in between.
    conflicts: dict[str, list[str]] = {}
    existing: list[str] = []
    missing: list[ChallengerSleeveSpec] = []
    for spec in plan.specs:
        config = store.resolve(spec.name, cohort_id=cohort_id)
        if config is None:
            missing.append(spec)
            continue
        differences = _stored_differences(config, spec, cohort_id)
        if differences:
            conflicts[spec.name] = differences
        else:
            existing.append(spec.name)
    if conflicts:
        detail = "; ".join(
            f"{name}: {', '.join(fields)}" for name, fields in sorted(conflicts.items())
        )
        raise ChallengerConflictError(
            f"Existing sleeve configuration conflicts with the requested immutable cohort "
            f"({detail}). Choose a new cohort id; nothing was changed."
        )
    if stored is not None and missing:
        raise ChallengerConflictError(
            f"Cohort {cohort_id!r} is missing members "
            f"{sorted(spec.name for spec in missing)}. A potentially running cohort is "
            "never repaired in place; investigate, then create a new cohort if needed."
        )

    created: list[str] = []
    rollback: list[str] = []
    try:
        for spec in missing:
            config = store.create(
                spec.name,
                strategy=spec.strategy,
                universe=list(spec.universe),
                starting_cash=contract.STARTING_CASH_PER_SLEEVE,
                max_positions=spec.max_positions,
                max_position_fraction=spec.max_position_fraction,
                settlement_t1=True,
                leverage=contract.LEVERAGE,
                factor="",
                definition=spec.definition,
                cohort_id=cohort_id,
                execution_methodology=METHODOLOGY.key,
            )
            created.append(spec.name)
            rollback.append(config.identity if config.sleeve_id else spec.name)
        location = (
            _write_manifest(store, plan.manifest)
            if stored is None
            else _manifest_location(store, cohort_id)
        )
    except Exception:
        # All-or-nothing: undo exactly what this call created, newest first, and let the
        # original error surface. A half-built cohort is never left behind.
        for reference in reversed(rollback):
            store.remove(reference)
        raise

    return CreateResult(
        cohort_id=cohort_id,
        created=tuple(created),
        existing=tuple(existing),
        manifest_location=location,
    )


def _require_scoped_storage(
    store: SleeveStore,
    configs: list[SleeveConfig],
    *,
    cohort_id: str,
    expected: set[str],
) -> None:
    """Refuse a name collision the local single-cohort registry cannot represent.

    ``challenger-v1`` freezes two sleeve names — ``control-cash`` and ``bench-spy`` —
    that ``paper-first-2026-07-28`` also uses. The shared registry scopes a sleeve by
    ``(namespace, cohort, name)``, so both cohorts hold their own distinct record and
    ``resolve(name, cohort_id=...)`` tells them apart. The local SQLite registry keys
    sleeves by ``name`` alone: it is a single-cohort layout, and there is no scoping to
    add here without a schema change this issue is not permitted to make.

    So the collision is reported as a precondition, before anything is written, rather
    than surfacing as a bare ``SleeveExists`` from four sleeves into the create loop.
    Renaming the challenger sleeves is not an alternative: the names are part of the
    frozen contract, and ``bench-spy`` is the benchmark every member's definition
    references.
    """
    if isinstance(store, SqlAlchemySleeveStore):
        return
    clashes = sorted(
        config.name
        for config in configs
        if config.name in expected and config.cohort_id != cohort_id
    )
    if not clashes:
        return
    other = sorted(
        {config.cohort_id or "(unassigned)" for config in configs if config.name in clashes}
    )
    raise ChallengerConflictError(
        f"The local sleeve registry keys sleeves by name alone, and {', '.join(clashes)} "
        f"already belong to {', '.join(other)}. challenger-v1 freezes those names, so the "
        f"two cohorts cannot coexist in local storage. Configure shared storage "
        f"(SCHWAB_DATABASE_URL), which scopes every sleeve by cohort, and create "
        f"{cohort_id!r} there. Nothing was changed."
    )


def _manifest_location(store: SleeveStore, cohort_id: str) -> str:
    if isinstance(store, SqlAlchemySleeveStore):
        return f"shared-storage/cohorts/{cohort_id}"
    return str(manifest_path(store.dir, cohort_id))


def inspect_cohort(*, store: SleeveStore, cohort_id: str) -> dict[str, Any]:
    """Return the exact stored manifest and sleeve records. Read-only."""
    manifest = _read_manifest(store, cohort_id)
    if manifest is None:
        raise FileNotFoundError(f"No challenger manifest exists for cohort {cohort_id!r}.")
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
                "universe_size": len(config.universe),
                "starting_cash": str(config.starting_cash),
                "settlement_t1": config.settlement_t1,
                "leverage": str(config.leverage),
                "max_positions": config.max_positions,
                "max_position_fraction": str(config.max_position_fraction),
                "execution_methodology": config.execution_methodology,
                "configuration_hash": config.configuration_hash,
                "reproducible": config.reproducible,
                "definition": (
                    config.definition.model_dump(mode="json")
                    if config.definition is not None
                    else None
                ),
            }
        )
    return {
        "manifest_location": _manifest_location(store, cohort_id),
        "manifest": manifest.model_dump(mode="json"),
        "stored_sleeves": records,
    }


def render(payload: dict[str, Any]) -> str:
    """Stable JSON rendering used by the CLI, so output is diffable between runs."""
    return json.dumps(payload, indent=2, sort_keys=True)
