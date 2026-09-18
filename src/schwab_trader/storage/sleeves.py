"""SQLAlchemy sleeve registry with scoped names and stable identities."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select

from schwab_trader.experiments import StrategyDefinition
from schwab_trader.sleeves import SleeveConfig, SleeveExists, SleeveStore
from schwab_trader.storage.database import Database
from schwab_trader.storage.identity import sleeve_scope, stable_id, stable_sleeve_id
from schwab_trader.storage.schema import (
    Cohort,
    CohortMember,
    PaperAccount,
    Sleeve,
    StorageNamespace,
)


class AmbiguousSleeveName(LookupError):
    """A bare human-readable sleeve name exists in more than one scope."""


class SqlAlchemySleeveStore:
    """Shared sleeve registry implementing the existing ``SleeveStore`` API."""

    def __init__(self, database: Database, *, namespace_name: str = "application") -> None:
        self.database = database
        self.namespace_id = stable_id("namespace", namespace_name)
        self._ensure_namespace(self.namespace_id, namespace_name, "application")

    def _ensure_namespace(self, namespace_id: str, name: str, kind: str) -> None:
        with self.database.session() as session:
            existing = session.get(StorageNamespace, namespace_id)
            if existing is None:
                session.add(
                    StorageNamespace(
                        namespace_id=namespace_id,
                        name=name,
                        kind=kind,
                        source_identity=None,
                        created_at=datetime.now(UTC),
                        immutable_metadata={},
                    )
                )
            elif existing.name != name or existing.kind != kind:
                raise RuntimeError("storage namespace identity conflicts with existing metadata")

    @staticmethod
    def _validate_name(name: str) -> None:
        SleeveStore._validate_name(name)

    def paper_path(self, name: str) -> Path:
        del name
        raise RuntimeError("shared paper state is addressed by sleeve_id, not a filesystem path")

    def eval_path(self, name: str) -> Path:
        del name
        raise RuntimeError(
            "shared evaluation state is addressed by sleeve_id, not a filesystem path"
        )

    def _ensure_cohort(self, cohort_id: str) -> tuple[str, str]:
        namespace_id = stable_id("namespace", "official-cohorts")
        self._ensure_namespace(namespace_id, "official-cohorts", "cohort")
        with self.database.session() as session:
            cohort = session.get(Cohort, cohort_id)
            if cohort is None:
                empty_manifest: dict[str, object] = {}
                session.add(
                    Cohort(
                        cohort_id=cohort_id,
                        namespace_id=namespace_id,
                        name=cohort_id,
                        created_at=datetime.now(UTC),
                        start_session=None,
                        status="pending-manifest",
                        starting_cash_per_sleeve=None,
                        settlement_model=None,
                        leverage=None,
                        benchmark_sleeve_name=None,
                        decision_schedule=None,
                        cost_model_id=None,
                        manifest_json=empty_manifest,
                        manifest_hash=hashlib.sha256(b"{}").hexdigest(),
                        source_path=None,
                    )
                )
            elif cohort.namespace_id != namespace_id:
                namespace_id = cohort.namespace_id
        return namespace_id, sleeve_scope(namespace_id=namespace_id, cohort_id=cohort_id)

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
        self._validate_name(name)
        normalized_cohort = cohort_id.strip()
        if normalized_cohort:
            namespace_id, scope_key = self._ensure_cohort(normalized_cohort)
        else:
            namespace_id = self.namespace_id
            scope_key = sleeve_scope(namespace_id=namespace_id, cohort_id=None)
        source_identity = f"application:{scope_key}:{name}"
        sleeve_id = stable_sleeve_id(
            source_identity=source_identity,
            scope_key=scope_key,
            name=name,
        )
        stamp = datetime.now(UTC)
        normalized_universe = [symbol.strip().upper() for symbol in universe if symbol.strip()]
        definition_payload = (
            definition.model_dump(mode="json", round_trip=True) if definition is not None else None
        )
        config_hash = definition.configuration_hash if definition is not None else ""

        with self.database.session() as session:
            duplicate = session.scalar(
                select(Sleeve).where(Sleeve.scope_key == scope_key, Sleeve.name == name)
            )
            if duplicate is not None:
                scope_label = (
                    f"cohort {normalized_cohort}"
                    if normalized_cohort
                    else "the local namespace"
                )
                raise SleeveExists(
                    f"Sleeve '{name}' already exists in {scope_label}."
                )
            session.add(
                Sleeve(
                    sleeve_id=sleeve_id,
                    namespace_id=namespace_id,
                    cohort_id=normalized_cohort or None,
                    scope_key=scope_key,
                    name=name,
                    original_name=name,
                    source_identity=source_identity,
                    source_sleeve_id=None,
                    source_path=None,
                    strategy=strategy,
                    universe=normalized_universe,
                    starting_cash=starting_cash,
                    max_positions=max_positions,
                    max_position_fraction=max_position_fraction,
                    settlement_t1=settlement_t1,
                    leverage=leverage,
                    factor=factor,
                    strategy_definition=definition_payload,
                    configuration_hash=config_hash,
                    decision_frequency=definition.decision_frequency if definition else "",
                    decision_time=definition.decision_time.isoformat() if definition else "",
                    execution_methodology=execution_methodology.strip(),
                    created_at=stamp,
                    source_created_at=None,
                )
            )
            # These models deliberately avoid ORM relationships. Flush the
            # parent explicitly so both SQLite and PostgreSQL satisfy the
            # sleeve foreign key before inserting account/membership children.
            session.flush()
            session.add(
                PaperAccount(
                    sleeve_id=sleeve_id,
                    starting_cash=starting_cash,
                    cash=starting_cash,
                    realized_pnl=Decimal(0),
                    created_at=stamp,
                    source_created_at=None,
                    last_accrual=None,
                    source_path=None,
                )
            )
            if normalized_cohort:
                ordinal = session.scalar(
                    select(func.coalesce(func.max(CohortMember.ordinal), -1)).where(
                        CohortMember.cohort_id == normalized_cohort
                    )
                )
                session.add(
                    CohortMember(
                        cohort_id=normalized_cohort,
                        sleeve_id=sleeve_id,
                        ordinal=int(ordinal) + 1 if ordinal is not None else 0,
                        role=None,
                        configuration_hash=config_hash,
                        source_path=None,
                    )
                )
        result = self.resolve(sleeve_id)
        assert result is not None
        return result

    def get(self, name: str) -> SleeveConfig | None:
        """Resolve a bare name only when it is unique across all scopes."""
        with self.database.session() as session:
            rows = list(session.scalars(select(Sleeve).where(Sleeve.name == name)))
        if not rows:
            return None
        if len(rows) > 1:
            scopes = ", ".join(sorted(row.scope_key for row in rows))
            raise AmbiguousSleeveName(
                f"Sleeve name '{name}' is ambiguous; pass --cohort or a sleeve_id "
                f"(available scopes: {scopes})."
            )
        return self._row_to_config(rows[0])

    def resolve(self, reference: str, *, cohort_id: str | None = None) -> SleeveConfig | None:
        with self.database.session() as session:
            exact = session.get(Sleeve, reference)
            if exact is not None:
                if cohort_id is not None and exact.cohort_id != cohort_id:
                    return None
                return self._row_to_config(exact)
            statement = select(Sleeve).where(Sleeve.name == reference)
            if cohort_id is not None:
                statement = statement.where(Sleeve.cohort_id == cohort_id)
            rows = list(session.scalars(statement))
        if not rows:
            return None
        if len(rows) > 1:
            scopes = ", ".join(sorted(row.scope_key for row in rows))
            raise AmbiguousSleeveName(
                f"Sleeve name '{reference}' is ambiguous; pass --cohort or a sleeve_id "
                f"(available scopes: {scopes})."
            )
        return self._row_to_config(rows[0])

    def list(self) -> list[SleeveConfig]:
        with self.database.session() as session:
            rows = list(session.scalars(select(Sleeve).order_by(Sleeve.created_at, Sleeve.name)))
        return [self._row_to_config(row) for row in rows]

    def remove(self, name: str, *, delete_data: bool = True) -> bool:
        config = self.resolve(name)
        if config is None:
            return False
        with self.database.session() as session:
            row = session.get(Sleeve, config.sleeve_id)
            assert row is not None
            if delete_data:
                session.execute(
                    delete(CohortMember).where(
                        CohortMember.sleeve_id == config.sleeve_id
                    )
                )
                session.execute(
                    delete(PaperAccount).where(PaperAccount.sleeve_id == config.sleeve_id)
                )
            elif session.get(PaperAccount, config.sleeve_id) is not None:
                raise RuntimeError(
                    "shared sleeves with paper state cannot be unregistered without deleting data"
                )
            session.delete(row)
        return True

    @staticmethod
    def _row_to_config(row: Sleeve) -> SleeveConfig:
        definition = (
            StrategyDefinition.model_validate(row.strategy_definition)
            if row.strategy_definition is not None
            else None
        )
        created_at = row.created_at
        if created_at is None and row.source_created_at:
            created_at = datetime.fromisoformat(row.source_created_at)
        return SleeveConfig(
            sleeve_id=row.sleeve_id,
            name=row.name,
            original_name=row.original_name,
            namespace_id=row.namespace_id,
            strategy=row.strategy,
            universe=list(row.universe),
            starting_cash=Decimal(row.starting_cash),
            max_positions=row.max_positions,
            max_position_fraction=Decimal(row.max_position_fraction),
            created_at=created_at or datetime.now(UTC),
            settlement_t1=row.settlement_t1,
            leverage=Decimal(row.leverage),
            factor=row.factor,
            definition=definition,
            cohort_id=row.cohort_id or "",
            configuration_hash=row.configuration_hash,
            decision_frequency=row.decision_frequency,
            decision_time=row.decision_time,
            execution_methodology=row.execution_methodology or "",
        )

    def cohort_start_session(self, cohort_id: str) -> date | None:
        """The cohort's persisted first official session from its immutable record.

        This is the authoritative answer to "when was this cohort first owed a run?",
        and it exists independently of whether that run ever happened — which is
        exactly why a scheduler that missed day one can still be caught.
        """
        with self.database.session() as session:
            cohort = session.get(Cohort, cohort_id)
            return None if cohort is None else cohort.start_session

    def cohort_manifest(self, cohort_id: str) -> dict[str, object] | None:
        """Return immutable shared cohort metadata when it has been finalized."""
        with self.database.session() as session:
            cohort = session.get(Cohort, cohort_id)
            if cohort is None or not cohort.manifest_json:
                return None
            return dict(cohort.manifest_json)

    def upsert_cohort_manifest(
        self,
        manifest: dict[str, object],
        *,
        source_path: str | None = None,
    ) -> None:
        """Persist an immutable cohort manifest or fail on any conflict."""
        cohort_payload = manifest.get("cohort")
        if not isinstance(cohort_payload, dict):
            raise ValueError("cohort manifest is missing its cohort object")
        cohort_id = str(cohort_payload.get("cohort_id", "")).strip()
        if not cohort_id:
            raise ValueError("cohort manifest has no cohort_id")
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        manifest_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        namespace_id, _ = self._ensure_cohort(cohort_id)
        with self.database.session() as session:
            cohort = session.get(Cohort, cohort_id)
            assert cohort is not None
            if cohort.manifest_json and cohort.manifest_hash != hashlib.sha256(b"{}").hexdigest():
                if cohort.manifest_hash != manifest_hash:
                    raise RuntimeError(f"cohort '{cohort_id}' has conflicting immutable metadata")
                return
            created_raw = cohort_payload.get("created_at")
            created = (
                datetime.fromisoformat(str(created_raw)) if created_raw is not None else None
            )
            cohort.namespace_id = namespace_id
            cohort.name = str(cohort_payload.get("name") or cohort_id)
            cohort.created_at = created
            cohort.start_session = (
                datetime.fromisoformat(str(cohort_payload["start_session"])).date()
                if cohort_payload.get("start_session")
                else None
            )
            cohort.status = str(cohort_payload.get("status") or "active")
            cohort.starting_cash_per_sleeve = cohort_payload.get("starting_cash_per_sleeve")
            cohort.settlement_model = (
                str(cohort_payload["settlement_model"])
                if cohort_payload.get("settlement_model") is not None
                else None
            )
            cohort.leverage = cohort_payload.get("leverage")
            cohort.benchmark_sleeve_name = (
                str(cohort_payload["benchmark_sleeve"])
                if cohort_payload.get("benchmark_sleeve") is not None
                else None
            )
            cohort.decision_schedule = (
                str(cohort_payload["decision_schedule"])
                if cohort_payload.get("decision_schedule") is not None
                else None
            )
            cohort.cost_model_id = (
                str(cohort_payload["cost_model_id"])
                if cohort_payload.get("cost_model_id") is not None
                else None
            )
            cohort.manifest_json = manifest
            cohort.manifest_hash = manifest_hash
            cohort.source_path = source_path
