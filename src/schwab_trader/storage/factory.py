"""Central backend selection; application modules do not branch on PostgreSQL."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import cast

from sqlalchemy import Table, inspect

from schwab_trader.cohort_alerts import CohortAlertStore
from schwab_trader.config import Settings
from schwab_trader.evaluation import EvaluationStore
from schwab_trader.intraday_panel import IntradayPanel
from schwab_trader.paper import PaperEngine
from schwab_trader.pricepanel import PricePanel
from schwab_trader.promotion import PromotionStore
from schwab_trader.research import ResearchStore
from schwab_trader.sec_store import SecStore
from schwab_trader.sleeve_runs import SleeveRunStore
from schwab_trader.sleeves import SleeveConfig, SleeveStore
from schwab_trader.storage.alerts import SqlAlchemyCohortAlertStore
from schwab_trader.storage.cohort_reviews import SqlAlchemyCohortReviewStore
from schwab_trader.storage.contracts import MarketDataEvidenceRepository
from schwab_trader.storage.database import Database
from schwab_trader.storage.datasets import (
    SqlAlchemyIntradayPanel,
    SqlAlchemyPricePanel,
    SqlAlchemySecStore,
)
from schwab_trader.storage.evaluation import SqlAlchemyEvaluationStore
from schwab_trader.storage.historical_replay import SqlAlchemyHistoricalReplayStore
from schwab_trader.storage.identity import standalone_paper_sleeve_id
from schwab_trader.storage.market_data import SqlAlchemyMarketDataEvidenceStore
from schwab_trader.storage.paper import SqlAlchemyPaperEngine
from schwab_trader.storage.records import (
    SqlAlchemyPromotionStore,
    SqlAlchemyResearchStore,
    SqlAlchemyUsageStore,
)
from schwab_trader.storage.runs import SqlAlchemySleeveRunStore
from schwab_trader.storage.schema import (
    Base,
    CohortAccountingCheck,
    CohortOperatorDecision,
    CohortReviewNote,
    HistoricalReplayBar,
    HistoricalReplayObservation,
    HistoricalReplaySession,
    HistoricalReplayUniverse,
    MarketDataDailyEvidence,
    MarketDataEvidenceConstituent,
)
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore
from schwab_trader.usage import UsageStore


@lru_cache(maxsize=4)
def _shared_database(raw_url: str) -> Database:
    return Database(raw_url)


def database(settings: Settings) -> Database | None:
    raw = settings.database_url.get_secret_value().strip()
    return _shared_database(raw) if raw else None


def sleeve_store(settings: Settings) -> SleeveStore:
    shared = database(settings)
    if shared is None:
        return SleeveStore(settings.sleeves_dir)
    return cast(SleeveStore, SqlAlchemySleeveStore(shared))


def paper_engine(settings: Settings, config: SleeveConfig) -> PaperEngine:
    shared = database(settings)
    if shared is None:
        return PaperEngine(
            settings.sleeves_dir / config.name / "paper.sqlite3",
            starting_cash=config.starting_cash,
            settle_t1=config.settlement_t1,
            leverage=config.leverage,
        )
    return cast(
        PaperEngine,
        SqlAlchemyPaperEngine(
            shared,
            config.identity,
            starting_cash=config.starting_cash,
            settle_t1=config.settlement_t1,
            leverage=config.leverage,
        ),
    )


def _standalone_config(settings: Settings) -> SleeveConfig:
    shared = database(settings)
    if shared is None:
        raise RuntimeError("standalone shared config requested while SQLite is active")
    config = SqlAlchemySleeveStore(shared).resolve(standalone_paper_sleeve_id())
    if config is None:
        raise RuntimeError(
            "shared standalone paper state has not been migrated; run storage verify"
        )
    return config


def default_paper_engine(settings: Settings) -> PaperEngine:
    shared = database(settings)
    if shared is None:
        return PaperEngine(
            settings.paper_db_path,
            starting_cash=settings.paper_starting_cash,
        )
    return paper_engine(settings, _standalone_config(settings))


def evaluation_store(settings: Settings, config: SleeveConfig) -> EvaluationStore:
    shared = database(settings)
    if shared is None:
        return EvaluationStore(settings.sleeves_dir / config.name / "eval.sqlite3")
    return cast(
        EvaluationStore,
        SqlAlchemyEvaluationStore(shared, config.identity),
    )


def default_evaluation_store(settings: Settings) -> EvaluationStore:
    shared = database(settings)
    if shared is None:
        return EvaluationStore(settings.agent_eval_db_path)
    return cast(
        EvaluationStore,
        SqlAlchemyEvaluationStore(shared, _standalone_config(settings).identity),
    )


def run_store(settings: Settings) -> SleeveRunStore:
    shared = database(settings)
    if shared is None:
        return SleeveRunStore(settings.sleeves_dir / "runs.sqlite3")
    return cast(SleeveRunStore, SqlAlchemySleeveRunStore(shared))


def alert_store(settings: Settings) -> CohortAlertStore:
    """Cohort alert transitions live beside the runs they describe.

    The shared database is the durable dedupe boundary when it is configured; the
    local file is only correct for the single-writer local layout.
    """
    shared = database(settings)
    if shared is None:
        return CohortAlertStore(settings.sleeves_dir / "cohort_alerts.sqlite3")
    return cast(CohortAlertStore, SqlAlchemyCohortAlertStore(shared))


def research_store(settings: Settings) -> ResearchStore:
    shared = database(settings)
    if shared is None:
        return ResearchStore(settings.research_db_path)
    return cast(ResearchStore, SqlAlchemyResearchStore(shared))


def promotion_store(settings: Settings) -> PromotionStore:
    shared = database(settings)
    if shared is None:
        return PromotionStore(settings.promotion_db_path)
    return cast(PromotionStore, SqlAlchemyPromotionStore(shared))


def usage_store(settings: Settings) -> UsageStore:
    shared = database(settings)
    if shared is None:
        return UsageStore(settings.usage_db_path)
    return cast(UsageStore, SqlAlchemyUsageStore(shared))


def price_panel(settings: Settings) -> PricePanel:
    shared = database(settings)
    if shared is None:
        return PricePanel(settings.price_panel_db_path)
    return cast(PricePanel, SqlAlchemyPricePanel(shared))


def intraday_panel(settings: Settings) -> IntradayPanel:
    shared = database(settings)
    if shared is None:
        return IntradayPanel(settings.intraday_panel_db_path)
    return cast(IntradayPanel, SqlAlchemyIntradayPanel(shared))


#: Bounded cache of local evidence databases, keyed by *resolved* path so two spellings
#: of one file do not open two engines. Deliberately not ``lru_cache``: each value owns a
#: live connection pool, and a fifth distinct path would evict one without disposing it,
#: leaking the pool and its open SQLite file handles until garbage collection.
_MAX_LOCAL_MARKET_DATA_DATABASES = 4
_local_market_data_databases: OrderedDict[str, Database] = OrderedDict()


def _local_market_data_database(raw_path: str) -> Database:
    path = Path(raw_path).resolve()
    key = str(path)
    cached = _local_market_data_databases.get(key)
    if cached is not None:
        _local_market_data_databases.move_to_end(key)
        return cached

    path.parent.mkdir(parents=True, exist_ok=True)
    local = Database(f"sqlite:///{path}")
    MarketDataDailyEvidence.metadata.create_all(
        bind=local.engine,
        tables=[
            cast(Table, MarketDataDailyEvidence.__table__),
            cast(Table, MarketDataEvidenceConstituent.__table__),
        ],
        checkfirst=True,
    )
    _local_market_data_databases[key] = local
    while len(_local_market_data_databases) > _MAX_LOCAL_MARKET_DATA_DATABASES:
        _, evicted = _local_market_data_databases.popitem(last=False)
        evicted.dispose()
    return local


#: Tables Alembic revision ``20260728_0003`` adds. The shared PostgreSQL evidence store
#: does not create them itself, so an unapplied revision surfaces only when the first
#: fallback-triggering session fails for every symbol as ``provider_error`` — a
#: misleading cause that also blocks the whole cohort. Named here so readiness can say so.
MARKET_DATA_EVIDENCE_TABLES = (
    "market_data_daily_evidence",
    "market_data_evidence_constituents",
)


def missing_tables(target: Database, expected: Sequence[str]) -> tuple[str, ...]:
    """Which of ``expected`` the database does not have. Read-only; creates nothing."""
    present = set(inspect(target.engine).get_table_names())
    return tuple(name for name in expected if name not in present)


def missing_market_data_evidence_tables(settings: Settings) -> tuple[str, ...]:
    """Evidence tables the configured backend is missing.

    Only the shared database can have a gap: the local SQLite store builds its own
    schema when opened, so it is never behind a migration.
    """
    shared = database(settings)
    if shared is None:
        return ()
    return missing_tables(shared, MARKET_DATA_EVIDENCE_TABLES)


def market_data_evidence_store(settings: Settings) -> MarketDataEvidenceRepository:
    """Use shared PostgreSQL when configured, otherwise a dedicated local SQLite DB."""
    shared = database(settings)
    if shared is None:
        shared = _local_market_data_database(str(settings.market_data_evidence_db_path))
    return SqlAlchemyMarketDataEvidenceStore(shared)


def market_data_evidence_reader(settings: Settings) -> MarketDataEvidenceRepository:
    """Open existing evidence storage without creating a local file or schema."""
    shared = database(settings)
    if shared is None:
        path = settings.market_data_evidence_db_path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"market-data evidence store does not exist: {path}")
        shared = Database(f"sqlite:///{path}")
    return SqlAlchemyMarketDataEvidenceStore(shared)


#: Tables Alembic revision ``20260730_0004`` adds for historical-replay RESEARCH
#: evidence. Deliberately *not* part of ``MARKET_DATA_EVIDENCE_TABLES``: forward cohort
#: readiness must never be satisfied by replay storage being present.
HISTORICAL_REPLAY_TABLES = (
    "historical_replay_universes",
    "historical_replay_sessions",
    "historical_replay_bars",
    "historical_replay_observations",
)

#: Bounded, disposing cache of local replay databases; see the evidence cache above.
_local_historical_replay_databases: OrderedDict[str, Database] = OrderedDict()


def _local_historical_replay_database(raw_path: str) -> Database:
    path = Path(raw_path).resolve()
    key = str(path)
    cached = _local_historical_replay_databases.get(key)
    if cached is not None:
        _local_historical_replay_databases.move_to_end(key)
        return cached

    path.parent.mkdir(parents=True, exist_ok=True)
    local = Database(f"sqlite:///{path}")
    Base.metadata.create_all(
        bind=local.engine,
        tables=[
            cast(Table, HistoricalReplayUniverse.__table__),
            cast(Table, HistoricalReplaySession.__table__),
            cast(Table, HistoricalReplayBar.__table__),
            cast(Table, HistoricalReplayObservation.__table__),
        ],
        checkfirst=True,
    )
    _local_historical_replay_databases[key] = local
    while len(_local_historical_replay_databases) > _MAX_LOCAL_MARKET_DATA_DATABASES:
        _, evicted = _local_historical_replay_databases.popitem(last=False)
        evicted.dispose()
    return local


def historical_replay_store(settings: Settings) -> SqlAlchemyHistoricalReplayStore:
    """Research-only replay storage: shared PostgreSQL when configured, else local SQLite.

    This is intentionally a concrete research adapter rather than one of the application
    repository protocols. No official cohort, readiness, paper, or order code path takes
    this store as a dependency.
    """
    shared = database(settings)
    if shared is None:
        shared = _local_historical_replay_database(str(settings.historical_replay_db_path))
    return SqlAlchemyHistoricalReplayStore(shared)


#: Tables Alembic revision ``20260801_0006`` adds for cohort review evidence. Named here
#: so a shared database that has not been upgraded reports the gap as a readable message
#: instead of failing the first ``cohort review`` command with a driver error.
COHORT_REVIEW_TABLES = (
    "cohort_accounting_checks",
    "cohort_review_notes",
    "cohort_operator_decisions",
)

#: Bounded, disposing cache of local review databases; see the evidence cache above.
_local_cohort_review_databases: OrderedDict[str, Database] = OrderedDict()


def _local_cohort_review_database(raw_path: str) -> Database:
    path = Path(raw_path).resolve()
    key = str(path)
    cached = _local_cohort_review_databases.get(key)
    if cached is not None:
        _local_cohort_review_databases.move_to_end(key)
        return cached

    path.parent.mkdir(parents=True, exist_ok=True)
    local = Database(f"sqlite:///{path}")
    Base.metadata.create_all(
        bind=local.engine,
        tables=[
            cast(Table, CohortAccountingCheck.__table__),
            cast(Table, CohortReviewNote.__table__),
            cast(Table, CohortOperatorDecision.__table__),
        ],
        checkfirst=True,
    )
    _local_cohort_review_databases[key] = local
    while len(_local_cohort_review_databases) > _MAX_LOCAL_MARKET_DATA_DATABASES:
        _, evicted = _local_cohort_review_databases.popitem(last=False)
        evicted.dispose()
    return local


def missing_cohort_review_tables(settings: Settings) -> tuple[str, ...]:
    """Review tables the configured backend is missing.

    Only the shared database can have a gap: the local file builds its own schema when
    it is opened, so it is never behind a migration.
    """
    shared = database(settings)
    if shared is None:
        return ()
    return missing_tables(shared, COHORT_REVIEW_TABLES)


def cohort_review_store(settings: Settings) -> SqlAlchemyCohortReviewStore:
    """Cohort review records: shared PostgreSQL when configured, else local SQLite.

    One adapter serves both, because the review tables reference cohorts, sleeves, and
    observations by stable *identity* rather than by foreign key — see
    :mod:`schwab_trader.storage.cohort_reviews`. That is what lets the local layout,
    whose sleeve registry is not in this database at all, use the same code path.
    """
    shared = database(settings)
    if shared is None:
        shared = _local_cohort_review_database(str(settings.cohort_review_db_path))
    return SqlAlchemyCohortReviewStore(shared)


def sec_store(settings: Settings) -> SecStore:
    shared = database(settings)
    if shared is None:
        return SecStore(settings.sec_db_path)
    return cast(SecStore, SqlAlchemySecStore(shared))
