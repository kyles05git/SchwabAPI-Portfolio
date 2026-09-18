"""Shared research, promotion, and optional usage history repositories."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from schwab_trader.promotion import (
    PromotionVerdict as PromotionVerdictModel,
)
from schwab_trader.research import StrategySpec
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import (
    PromotionVerdict,
    ResearchStrategySpec,
    UsageEvent,
)
from schwab_trader.usage import Usage, UsageSummary, estimate_cost


def _canonical_json(payload: object) -> tuple[dict[str, object], str]:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise ValueError("stored immutable record must be a JSON object")
    return parsed, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SqlAlchemyResearchStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, spec: StrategySpec) -> int:
        payload, payload_hash = _canonical_json(spec.model_dump(mode="json"))
        with self.database.session() as session:
            row = ResearchStrategySpec(
                source_path=None,
                source_spec_id=None,
                created_at=spec.created_at,
                source_created_at=None,
                model=spec.model,
                specification=payload,
                specification_hash=payload_hash,
            )
            session.add(row)
            session.flush()
            return row.spec_id

    def latest(self) -> StrategySpec | None:
        with self.database.session() as session:
            row = session.scalar(
                select(ResearchStrategySpec)
                .order_by(ResearchStrategySpec.spec_id.desc())
                .limit(1)
            )
        return None if row is None else StrategySpec.model_validate(row.specification)

    def recent(self, limit: int = 10) -> list[StrategySpec]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ResearchStrategySpec)
                    .order_by(ResearchStrategySpec.spec_id.desc())
                    .limit(limit)
                )
            )
        return [StrategySpec.model_validate(row.specification) for row in rows]


class SqlAlchemyPromotionStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, verdict: PromotionVerdictModel) -> int:
        payload, payload_hash = _canonical_json(verdict.model_dump(mode="json"))
        with self.database.session() as session:
            row = PromotionVerdict(
                source_path=None,
                source_verdict_id=None,
                strategy=verdict.strategy,
                universe=verdict.universe,
                created_at=verdict.created_at,
                source_created_at=None,
                verdict=payload,
                verdict_hash=payload_hash,
            )
            session.add(row)
            session.flush()
            return row.verdict_id

    def latest(
        self, strategy: str, universe: str
    ) -> PromotionVerdictModel | None:
        with self.database.session() as session:
            row = session.scalar(
                select(PromotionVerdict)
                .where(
                    PromotionVerdict.strategy == strategy,
                    PromotionVerdict.universe == universe,
                )
                .order_by(PromotionVerdict.verdict_id.desc())
                .limit(1)
            )
        return (
            None
            if row is None
            else PromotionVerdictModel.model_validate(row.verdict)
        )

    def all_latest(self) -> list[PromotionVerdictModel]:
        with self.database.session() as session:
            latest_ids = (
                select(func.max(PromotionVerdict.verdict_id))
                .group_by(PromotionVerdict.strategy, PromotionVerdict.universe)
            )
            rows = list(
                session.scalars(
                    select(PromotionVerdict)
                    .where(PromotionVerdict.verdict_id.in_(latest_ids))
                    .order_by(PromotionVerdict.strategy, PromotionVerdict.universe)
                )
            )
        return [PromotionVerdictModel.model_validate(row.verdict) for row in rows]


class SqlAlchemyUsageStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, kind: str, usage: Usage) -> Decimal:
        cost = estimate_cost(usage)
        with self.database.session() as session:
            session.add(
                UsageEvent(
                    source_path=None,
                    source_event_id=None,
                    occurred_at=datetime.now(UTC),
                    source_ts=None,
                    kind=kind,
                    model=usage.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    web_searches=usage.web_searches,
                    cost=cost,
                )
            )
        return cost

    def summary(self) -> UsageSummary:
        with self.database.session() as session:
            rows = list(
                session.scalars(select(UsageEvent).order_by(UsageEvent.event_id))
            )
        execution = sum(
            (Decimal(row.cost) for row in rows if row.kind == "execution"),
            Decimal(0),
        )
        research = sum(
            (Decimal(row.cost) for row in rows if row.kind == "research"),
            Decimal(0),
        )
        stamps = [
            row.occurred_at
            or (
                datetime.fromisoformat(row.source_ts)
                if row.source_ts is not None
                else None
            )
            for row in rows
        ]
        actual_stamps = [stamp for stamp in stamps if stamp is not None]
        return UsageSummary(
            calls=len(rows),
            total_cost=execution + research,
            execution_cost=execution,
            research_cost=research,
            input_tokens=sum(row.input_tokens for row in rows),
            output_tokens=sum(row.output_tokens for row in rows),
            web_searches=sum(row.web_searches for row in rows),
            first_ts=min(actual_stamps) if actual_stamps else None,
            last_ts=max(actual_stamps) if actual_stamps else None,
        )
