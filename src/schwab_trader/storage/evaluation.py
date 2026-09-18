"""SQLAlchemy evaluation repository implementing the existing store contract."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select

from schwab_trader import metrics
from schwab_trader.agent import CycleReport
from schwab_trader.evaluation import (
    CycleRecord,
    EvalSummary,
    ObservationStatus,
    OfficialDailyObservation,
)
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import (
    EvaluationCycle,
    EvaluationDecision,
)
from schwab_trader.storage.schema import (
    OfficialDailyObservation as ObservationRow,
)


class SqlAlchemyEvaluationStore:
    """Shared evaluation history scoped by stable sleeve identity."""

    def __init__(self, database: Database, sleeve_id: str) -> None:
        self.database = database
        self.sleeve_id = sleeve_id

    def record_cycle(self, report: CycleReport) -> int:
        valuation = report.valuation
        with self.database.session() as session:
            row = EvaluationCycle(
                sleeve_id=self.sleeve_id,
                source_path=None,
                source_cycle_id=None,
                observed_at=report.now,
                source_ts=None,
                strategy=report.strategy,
                num_proposals=len(report.outcomes),
                num_filled=report.num_filled,
                num_rejected=report.num_rejected,
                cash=valuation.cash,
                positions_value=valuation.positions_value,
                total_value=valuation.total_value,
                realized_pnl=valuation.realized_pnl,
                unrealized_pnl=valuation.unrealized_pnl,
                starting_cash=valuation.starting_cash,
                return_pct=valuation.total_return_pct,
            )
            session.add(row)
            session.flush()
            for outcome in report.outcomes:
                request = outcome.proposal.request
                session.add(
                    EvaluationDecision(
                        cycle_id=row.cycle_id,
                        source_path=None,
                        source_decision_id=None,
                        side=request.side.value,
                        symbol=request.symbol,
                        quantity=request.quantity,
                        limit_price=request.limit_price,
                        status=outcome.status,
                        fill_price=outcome.fill_price,
                        rationale=outcome.proposal.rationale,
                    )
                )
            return row.cycle_id

    def recent_cycles(self, limit: int = 20) -> list[CycleRecord]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(EvaluationCycle)
                    .where(EvaluationCycle.sleeve_id == self.sleeve_id)
                    .order_by(EvaluationCycle.cycle_id.desc())
                    .limit(limit)
                )
            )
        return [self._domain_cycle(row) for row in rows]

    @staticmethod
    def _domain_cycle(row: EvaluationCycle) -> CycleRecord:
        stamp = row.observed_at
        if stamp is None and row.source_ts:
            stamp = datetime.fromisoformat(row.source_ts)
        return CycleRecord(
            id=row.cycle_id,
            ts=stamp or datetime.now(UTC),
            strategy=row.strategy,
            num_proposals=row.num_proposals,
            num_filled=row.num_filled,
            num_rejected=row.num_rejected,
            cash=Decimal(row.cash),
            positions_value=Decimal(row.positions_value),
            total_value=Decimal(row.total_value),
            realized_pnl=Decimal(row.realized_pnl),
            unrealized_pnl=Decimal(row.unrealized_pnl),
            starting_cash=Decimal(row.starting_cash),
            return_pct=Decimal(row.return_pct),
        )

    def equity_curve(self, limit: int = 200) -> list[tuple[datetime, Decimal]]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(EvaluationCycle)
                    .where(EvaluationCycle.sleeve_id == self.sleeve_id)
                    .order_by(EvaluationCycle.cycle_id.desc())
                    .limit(limit)
                )
            )
        points = [
            (self._domain_cycle(row).ts, Decimal(row.total_value))
            for row in rows
        ]
        points.reverse()
        return points

    def summary(self) -> EvalSummary:
        with self.database.session() as session:
            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(EvaluationCycle)
                    .where(EvaluationCycle.sleeve_id == self.sleeve_id)
                )
                or 0
            )
            if count == 0:
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
            rows = list(
                session.scalars(
                    select(EvaluationCycle)
                    .where(EvaluationCycle.sleeve_id == self.sleeve_id)
                    .order_by(EvaluationCycle.cycle_id)
                )
            )
        records = [self._domain_cycle(row) for row in rows]
        latest = records[-1]
        values = [latest.starting_cash, *(record.total_value for record in records)]
        simple_returns = metrics.simple_returns([record.total_value for record in records])
        sharpe_value = metrics.sharpe(simple_returns)
        return EvalSummary(
            cycles=len(records),
            trades_filled=sum(record.num_filled for record in records),
            starting_cash=latest.starting_cash,
            latest_value=latest.total_value,
            total_return_pct=latest.return_pct,
            realized_pnl=latest.realized_pnl,
            max_drawdown_pct=metrics.max_drawdown_pct(values),
            sharpe=(
                Decimal(str(round(sharpe_value, 2)))
                if sharpe_value is not None
                else None
            ),
            first_ts=records[0].ts,
            last_ts=latest.ts,
        )

    def record_official_observation(self, obs: OfficialDailyObservation) -> int:
        if obs.sleeve_id != self.sleeve_id:
            raise ValueError(
                "official observation sleeve identity does not match the repository"
            )
        with self.database.session() as session:
            existing = session.scalar(
                select(ObservationRow).where(
                    ObservationRow.observation_key == obs.observation_key
                )
            )
            if existing is not None:
                if self._domain_observation(existing) != obs:
                    raise RuntimeError(
                        "official observation identity conflicts with existing content"
                    )
                return existing.observation_id
            row = ObservationRow(
                source_observation_id=None,
                observation_key=obs.observation_key,
                source_observation_key=None,
                cohort_id=obs.cohort_id,
                run_id=obs.run_id,
                sleeve_id=self.sleeve_id,
                strategy=obs.strategy,
                strategy_hash=obs.strategy_hash,
                session_date=obs.session_date,
                decision_time=obs.decision_time,
                valuation_time=obs.valuation_time,
                execution_methodology=obs.execution_methodology,
                signal_session_date=obs.signal_session_date,
                execution_session_date=obs.execution_session_date,
                signal_time=obs.signal_time,
                execution_time=obs.execution_time,
                status=obs.status.value,
                total_value=obs.total_value,
                return_pct=obs.return_pct,
                benchmark_value=obs.benchmark_value,
                exposure=obs.exposure,
                num_positions=obs.num_positions,
                turnover=obs.turnover,
                modeled_cost=obs.modeled_cost,
                num_filled=obs.num_filled,
                num_rejected=obs.num_rejected,
                quote_coverage=obs.quote_coverage,
                snapshot_ids=dict(obs.snapshot_ids),
                readiness_ready=obs.readiness_ready,
                readiness_reasons=list(obs.readiness_reasons),
                recorded_at=datetime.now(UTC),
                source_recorded_at=None,
                source_path=None,
            )
            session.add(row)
            session.flush()
            return row.observation_id

    def official_observations(self, limit: int = 200) -> list[OfficialDailyObservation]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ObservationRow)
                    .where(ObservationRow.sleeve_id == self.sleeve_id)
                    .order_by(ObservationRow.session_date, ObservationRow.observation_id)
                    .limit(limit)
                )
            )
        return [self._domain_observation(row) for row in rows]

    def official_equity_curve(self, limit: int = 10_000) -> list[tuple[date, Decimal]]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ObservationRow)
                    .where(
                        ObservationRow.sleeve_id == self.sleeve_id,
                        ObservationRow.total_value.is_not(None),
                    )
                    .order_by(ObservationRow.session_date, ObservationRow.observation_id)
                    .limit(limit)
                )
            )
        return [
            (row.session_date, Decimal(row.total_value))
            for row in rows
            if row.total_value is not None
        ]

    @staticmethod
    def _domain_observation(row: ObservationRow) -> OfficialDailyObservation:
        return OfficialDailyObservation(
            cohort_id=row.cohort_id,
            run_id=row.run_id,
            sleeve_id=row.sleeve_id,
            strategy=row.strategy,
            strategy_hash=row.strategy_hash,
            session_date=row.session_date,
            decision_time=row.decision_time,
            valuation_time=row.valuation_time,
            execution_methodology=row.execution_methodology or "",
            signal_session_date=row.signal_session_date,
            execution_session_date=row.execution_session_date,
            signal_time=row.signal_time,
            execution_time=row.execution_time,
            status=ObservationStatus(row.status),
            total_value=(
                Decimal(row.total_value) if row.total_value is not None else None
            ),
            return_pct=Decimal(row.return_pct) if row.return_pct is not None else None,
            benchmark_value=(
                Decimal(row.benchmark_value)
                if row.benchmark_value is not None
                else None
            ),
            exposure=Decimal(row.exposure) if row.exposure is not None else None,
            num_positions=row.num_positions,
            turnover=Decimal(row.turnover) if row.turnover is not None else None,
            modeled_cost=(
                Decimal(row.modeled_cost) if row.modeled_cost is not None else None
            ),
            num_filled=row.num_filled,
            num_rejected=row.num_rejected,
            quote_coverage=(
                Decimal(row.quote_coverage)
                if row.quote_coverage is not None
                else None
            ),
            snapshot_ids=dict(row.snapshot_ids),
            readiness_ready=row.readiness_ready,
            readiness_reasons=tuple(row.readiness_reasons),
        )
