"""Provider-neutral application ports implemented by SQLite and SQLAlchemy adapters."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

from schwab_trader.agent import CycleReport
from schwab_trader.cohort_alerts import (
    DEFAULT_AMBIGUOUS_AFTER,
    DEFAULT_MAX_ATTEMPTS,
    AlertKind,
    ClaimResult,
    CohortAlert,
)
from schwab_trader.evaluation import CycleRecord, EvalSummary, OfficialDailyObservation
from schwab_trader.market_bar_evidence import DerivedDailyEvidence
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest
from schwab_trader.paper import (
    PaperAccount,
    PaperOrder,
    PaperPosition,
    PaperValuation,
)
from schwab_trader.sleeve_runs import SleeveRun
from schwab_trader.sleeves import SleeveConfig


@runtime_checkable
class SleeveRepository(Protocol):
    """Stable-ID and scoped-name access to complete sleeve definitions."""

    def resolve(
        self,
        reference: str,
        *,
        cohort_id: str | None = None,
    ) -> SleeveConfig | None: ...

    def list(self) -> list[SleeveConfig]: ...


@runtime_checkable
class PaperRepository(Protocol):
    """Transactional paper account, position, order, fill, and cash behavior."""

    def account(self) -> PaperAccount: ...

    def positions(self) -> list[PaperPosition]: ...

    def recent_orders(self, limit: int = 20) -> list[PaperOrder]: ...

    def place_order(
        self,
        request: OrderRequest,
        quote: Quote,
        *,
        now: datetime | None = None,
    ) -> PaperOrder: ...

    def value(self, marks: dict[str, Decimal | None]) -> PaperValuation: ...


@runtime_checkable
class EvaluationRepository(Protocol):
    """Cycle history and immutable official daily observations."""

    def record_cycle(self, report: CycleReport) -> int: ...

    def recent_cycles(self, limit: int = 20) -> list[CycleRecord]: ...

    def summary(self) -> EvalSummary: ...

    def record_official_observation(
        self,
        obs: OfficialDailyObservation,
    ) -> int: ...

    def official_observations(
        self,
        limit: int = 200,
    ) -> list[OfficialDailyObservation]: ...


@runtime_checkable
class OfficialRunRepository(Protocol):
    """Durable run checkpoints plus exclusive official-session ownership."""

    def get(self, run_id: str) -> SleeveRun | None: ...

    def list(
        self,
        *,
        cohort_id: str | None = None,
        limit: int = 200,
    ) -> Sequence[SleeveRun]: ...

    def official_session(
        self,
        cohort_id: str,
        scheduled_for: date,
        *,
        owner_id: str | None = None,
    ) -> AbstractContextManager[bool]: ...


@runtime_checkable
class MarketDataEvidenceRepository(Protocol):
    """Content-addressed derived daily candles and their exact constituents."""

    def save(self, evidence: DerivedDailyEvidence) -> DerivedDailyEvidence: ...

    def get(self, dataset_id: str) -> DerivedDailyEvidence | None: ...

    def for_session(
        self,
        symbol: str,
        session_date: date,
    ) -> tuple[DerivedDailyEvidence, ...]: ...

    def reproduce(self, dataset_id: str) -> DerivedDailyEvidence | None: ...


@runtime_checkable
class CohortAlertRepository(Protocol):
    """At-most-once claim/settle for cohort lifecycle notification transitions."""

    def claim(
        self,
        *,
        cohort_id: str,
        session_id: str,
        scheduled_for: date,
        kind: AlertKind,
        detail: str | None = None,
        now: datetime | None = None,
        ambiguous_after: timedelta = DEFAULT_AMBIGUOUS_AFTER,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> ClaimResult: ...

    def mark_sent(self, key: str, *, now: datetime | None = None) -> CohortAlert | None: ...

    def mark_failed(
        self,
        key: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> CohortAlert | None: ...

    def get(self, key: str) -> CohortAlert | None: ...

    def list(
        self,
        *,
        cohort_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[CohortAlert]: ...
