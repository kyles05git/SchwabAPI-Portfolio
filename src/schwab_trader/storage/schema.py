"""Shared SQLAlchemy schema for paper, research, market, and migration data."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from schwab_trader.storage.types import AwareTimestamp, ExactNumeric

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class StorageNamespace(Base):
    __tablename__ = "storage_namespaces"

    namespace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_identity: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    immutable_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)


class Cohort(Base):
    __tablename__ = "cohorts"

    cohort_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(
        ForeignKey("storage_namespaces.namespace_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    start_session: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    starting_cash_per_sleeve: Mapped[Any | None] = mapped_column(ExactNumeric())
    settlement_model: Mapped[str | None] = mapped_column(String(32))
    leverage: Mapped[Any | None] = mapped_column(ExactNumeric())
    benchmark_sleeve_name: Mapped[str | None] = mapped_column(String(80))
    decision_schedule: Mapped[str | None] = mapped_column(Text)
    cost_model_id: Mapped[str | None] = mapped_column(String(128))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)


class Sleeve(Base):
    __tablename__ = "sleeves"
    __table_args__ = (
        UniqueConstraint("scope_key", "name"),
        UniqueConstraint("source_identity"),
        UniqueConstraint("source_path", "source_sleeve_id"),
        CheckConstraint(
            "(cohort_id IS NULL AND scope_key LIKE 'namespace:%') OR "
            "(cohort_id IS NOT NULL AND scope_key LIKE 'cohort:%')",
            name="scope_matches_cohort",
        ),
    )

    sleeve_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(
        ForeignKey("storage_namespaces.namespace_id"), nullable=False
    )
    cohort_id: Mapped[str | None] = mapped_column(ForeignKey("cohorts.cohort_id"))
    scope_key: Mapped[str] = mapped_column(String(140), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    original_name: Mapped[str] = mapped_column(String(80), nullable=False)
    source_identity: Mapped[str] = mapped_column(Text, nullable=False)
    source_sleeve_id: Mapped[str | None] = mapped_column(String(80))
    source_path: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(String(128), nullable=False)
    universe: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    starting_cash: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    max_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    max_position_fraction: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    settlement_t1: Mapped[bool] = mapped_column(Boolean, nullable=False)
    leverage: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    factor: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_definition: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    configuration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_frequency: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_time: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_methodology: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    created_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_created_at: Mapped[str | None] = mapped_column(Text)


class CohortMember(Base):
    __tablename__ = "cohort_members"
    __table_args__ = (UniqueConstraint("cohort_id", "ordinal"),)

    cohort_id: Mapped[str] = mapped_column(
        ForeignKey("cohorts.cohort_id", ondelete="CASCADE"), primary_key=True
    )
    sleeve_id: Mapped[str] = mapped_column(
        ForeignKey("sleeves.sleeve_id", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str | None] = mapped_column(String(64))
    configuration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)


class PaperAccount(Base):
    __tablename__ = "paper_accounts"

    sleeve_id: Mapped[str] = mapped_column(
        ForeignKey("sleeves.sleeve_id", ondelete="RESTRICT"), primary_key=True
    )
    starting_cash: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    cash: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    realized_pnl: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_created_at: Mapped[str | None] = mapped_column(Text)
    last_accrual: Mapped[date | None] = mapped_column(Date)
    source_path: Mapped[str | None] = mapped_column(Text)


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    sleeve_id: Mapped[str] = mapped_column(
        ForeignKey("paper_accounts.sleeve_id", ondelete="CASCADE"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    avg_cost: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    __table_args__ = (UniqueConstraint("sleeve_id", "source_path", "source_order_id"),)

    paper_order_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sleeve_id: Mapped[str] = mapped_column(
        ForeignKey("paper_accounts.sleeve_id", ondelete="CASCADE"), nullable=False
    )
    source_path: Mapped[str | None] = mapped_column(Text)
    source_order_id: Mapped[int | None] = mapped_column(BigInteger)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    limit_price: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    fill_price: Mapped[Any | None] = mapped_column(ExactNumeric())
    created_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_created_at: Mapped[str | None] = mapped_column(Text)
    filled_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_filled_at: Mapped[str | None] = mapped_column(Text)


class PaperFill(Base):
    __tablename__ = "paper_fills"
    __table_args__ = (UniqueConstraint("paper_order_id", "fill_sequence"),)

    paper_fill_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_order_id: Mapped[int] = mapped_column(
        ForeignKey("paper_orders.paper_order_id", ondelete="CASCADE"), nullable=False
    )
    fill_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    filled_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_filled_at: Mapped[str | None] = mapped_column(Text)


class PaperUnsettledCash(Base):
    __tablename__ = "paper_unsettled_cash"
    __table_args__ = (UniqueConstraint("sleeve_id", "source_path", "source_unsettled_id"),)

    unsettled_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sleeve_id: Mapped[str] = mapped_column(
        ForeignKey("paper_accounts.sleeve_id", ondelete="CASCADE"), nullable=False
    )
    source_path: Mapped[str | None] = mapped_column(Text)
    source_unsettled_id: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    settle_date: Mapped[date] = mapped_column(Date, nullable=False)


class EvaluationCycle(Base):
    __tablename__ = "evaluation_cycles"
    __table_args__ = (UniqueConstraint("sleeve_id", "source_path", "source_cycle_id"),)

    cycle_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sleeve_id: Mapped[str] = mapped_column(
        ForeignKey("sleeves.sleeve_id", ondelete="CASCADE"), nullable=False
    )
    source_path: Mapped[str | None] = mapped_column(Text)
    source_cycle_id: Mapped[int | None] = mapped_column(BigInteger)
    observed_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_ts: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(String(128), nullable=False)
    num_proposals: Mapped[int] = mapped_column(Integer, nullable=False)
    num_filled: Mapped[int] = mapped_column(Integer, nullable=False)
    num_rejected: Mapped[int] = mapped_column(Integer, nullable=False)
    cash: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    positions_value: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    total_value: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    realized_pnl: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    unrealized_pnl: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    starting_cash: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    return_pct: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)


class EvaluationDecision(Base):
    __tablename__ = "evaluation_decisions"
    __table_args__ = (UniqueConstraint("source_path", "source_decision_id"),)

    decision_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(
        ForeignKey("evaluation_cycles.cycle_id", ondelete="CASCADE"), nullable=False
    )
    source_path: Mapped[str | None] = mapped_column(Text)
    source_decision_id: Mapped[int | None] = mapped_column(BigInteger)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    limit_price: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    fill_price: Mapped[Any | None] = mapped_column(ExactNumeric())
    rationale: Mapped[str | None] = mapped_column(Text)


class CohortRun(Base):
    __tablename__ = "cohort_runs"
    __table_args__ = (
        UniqueConstraint("run_key"),
        UniqueConstraint("cohort_id", "scheduled_for"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_key: Mapped[str] = mapped_column(String(180), nullable=False)
    cohort_id: Mapped[str] = mapped_column(
        ForeignKey("cohorts.cohort_id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_for: Mapped[date] = mapped_column(Date, nullable=False)
    expected_members: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    completed_members: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(String(256))
    quote_snapshot_id: Mapped[str | None] = mapped_column(String(256))
    data_snapshot_ids: Mapped[dict[str, str]] = mapped_column(JSON_VALUE, nullable=False)
    started_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON_VALUE, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)


class CohortRunMember(Base):
    __tablename__ = "cohort_run_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["cohort_id", "sleeve_id"],
            ["cohort_members.cohort_id", "cohort_members.sleeve_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "ordinal"),
        UniqueConstraint("run_id", "source_sleeve_id"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("cohort_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    sleeve_id: Mapped[str] = mapped_column(
        ForeignKey("sleeves.sleeve_id", ondelete="RESTRICT"), primary_key=True
    )
    source_sleeve_id: Mapped[str | None] = mapped_column(String(80))
    cohort_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    completed_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    source_path: Mapped[str | None] = mapped_column(Text)


class OfficialDailyObservation(Base):
    __tablename__ = "official_daily_observations"
    __table_args__ = (
        UniqueConstraint("observation_key"),
        UniqueConstraint("cohort_id", "sleeve_id", "session_date"),
        UniqueConstraint("source_path", "source_observation_id"),
    )

    observation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_observation_id: Mapped[int | None] = mapped_column(BigInteger)
    observation_key: Mapped[str] = mapped_column(String(256), nullable=False)
    source_observation_key: Mapped[str | None] = mapped_column(Text)
    cohort_id: Mapped[str] = mapped_column(
        ForeignKey("cohorts.cohort_id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("cohort_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    sleeve_id: Mapped[str] = mapped_column(
        ForeignKey("sleeves.sleeve_id", ondelete="RESTRICT"), nullable=False
    )
    strategy: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    decision_time: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    valuation_time: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    # Execution-timing lineage (#79). Nullable and defaulted so every row written under
    # the close-marked model — where the signal and execution sessions coincide and
    # session_date/decision_time already say so — stays exactly as it is.
    execution_methodology: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    signal_session_date: Mapped[date | None] = mapped_column(Date)
    execution_session_date: Mapped[date | None] = mapped_column(Date)
    signal_time: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    execution_time: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    total_value: Mapped[Any | None] = mapped_column(ExactNumeric())
    return_pct: Mapped[Any | None] = mapped_column(ExactNumeric())
    benchmark_value: Mapped[Any | None] = mapped_column(ExactNumeric())
    exposure: Mapped[Any | None] = mapped_column(ExactNumeric())
    num_positions: Mapped[int | None] = mapped_column(Integer)
    turnover: Mapped[Any | None] = mapped_column(ExactNumeric())
    modeled_cost: Mapped[Any | None] = mapped_column(ExactNumeric())
    num_filled: Mapped[int] = mapped_column(Integer, nullable=False)
    num_rejected: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_coverage: Mapped[Any | None] = mapped_column(ExactNumeric())
    snapshot_ids: Mapped[dict[str, str]] = mapped_column(JSON_VALUE, nullable=False)
    readiness_ready: Mapped[bool | None] = mapped_column(Boolean)
    readiness_reasons: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    recorded_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_recorded_at: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(Text)


class OfficialSessionLease(Base):
    __tablename__ = "official_session_leases"

    cohort_id: Mapped[str] = mapped_column(
        ForeignKey("cohorts.cohort_id", ondelete="CASCADE"), primary_key=True
    )
    scheduled_for: Mapped[date] = mapped_column(Date, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())


class CohortAlert(Base):
    """One durable cohort lifecycle notification transition.

    The ``(cohort_id, session_id, kind)`` uniqueness is the whole point: it makes
    "has this transition already been notified?" a database question rather than
    process state, so repeated scheduler invocations and restarts cannot resend.
    """

    __tablename__ = "cohort_alerts"
    __table_args__ = (UniqueConstraint("cohort_id", "session_id", "kind"),)

    alert_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    cohort_id: Mapped[str] = mapped_column(
        ForeignKey("cohorts.cohort_id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_for: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    detail: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)


#: Accounting domains a review entry may cover. Mirrors
#: :class:`schwab_trader.operational_gate.AccountingArea`; spelled out here so the
#: database refuses an unknown area even if some future caller bypasses the service.
_ACCOUNTING_AREAS = "('cash', 'positions', 'valuation')"
_REVIEW_FINDINGS = "('matched', 'difference')"
_OPERATOR_ACTIONS = "('keep', 'modify', 'pause', 'retire')"


class CohortAccountingCheck(Base):
    """One immutable accounting-review entry for one observation and accounting area.

    Append-only. A correction is a new row with the next ``revision`` for the same
    ``(cohort_id, observation_key, area)``; the row it corrects is never updated or
    deleted, and "current" is the highest revision. That is why there is no
    ``superseded_at`` column to maintain — the absence of a later revision *is* the
    statement that this row is current, and no writer can get it out of step.

    Deliberately carries no foreign key to ``cohorts``, ``sleeves``, or
    ``official_daily_observations``. In the local layout those live in a separate
    file-backed registry rather than in this database, so the constraint could only be
    expressed on one of the two supported backends. Referential validity is enforced for
    both by :class:`schwab_trader.cohort_review.CohortReviewService`, which checks every
    identity against the authoritative records before any row is written.
    """

    __tablename__ = "cohort_accounting_checks"
    __table_args__ = (
        UniqueConstraint(
            "cohort_id",
            "observation_key",
            "area",
            "revision",
            name="uq_cohort_accounting_checks_entry",
        ),
        CheckConstraint(f"area IN {_ACCOUNTING_AREAS}", name="area"),
        CheckConstraint(f"finding IN {_REVIEW_FINDINGS}", name="finding"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        # A difference with no summary says only "something was wrong", which is the
        # state this whole record exists to replace.
        CheckConstraint(
            "finding <> 'difference' OR (summary IS NOT NULL AND summary <> '')",
            name="difference_has_summary",
        ),
        Index("ix_cohort_accounting_checks_cohort_id", "cohort_id"),
    )

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sleeve_id: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_key: Mapped[str] = mapped_column(String(256), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    area: Mapped[str] = mapped_column(String(16), nullable=False)
    finding: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes: Mapped[str | None] = mapped_column(
        ForeignKey(
            "cohort_accounting_checks.entry_id",
            name="fk_cohort_accounting_checks_supersedes",
            ondelete="RESTRICT",
        )
    )


class CohortReviewNote(Base):
    """One durable operator note about a cohort, a member sleeve, or an observation.

    Notes are not versioned: the identity is a digest of the target and the text, so
    repeating an identical note is a no-op rather than a second copy, and any edit is a
    different note that keeps the original.
    """

    __tablename__ = "cohort_review_notes"
    __table_args__ = (Index("ix_cohort_review_notes_cohort_id", "cohort_id"),)

    note_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sleeve_id: Mapped[str | None] = mapped_column(String(64))
    observation_key: Mapped[str | None] = mapped_column(String(256))
    note: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(64), nullable=False)


class CohortOperatorDecision(Base):
    """One immutable keep/modify/pause/retire disposition for one cohort sleeve.

    Append-only with the same revision rule as :class:`CohortAccountingCheck`. A recorded
    decision is a *research* disposition about a paper experiment: it never promotes,
    pauses, retires, or reconfigures anything, and it never authorizes live trading.
    """

    __tablename__ = "cohort_operator_decisions"
    __table_args__ = (
        UniqueConstraint(
            "cohort_id",
            "sleeve_id",
            "revision",
            name="uq_cohort_operator_decisions_entry",
        ),
        CheckConstraint(f"action IN {_OPERATOR_ACTIONS}", name="action"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        CheckConstraint("rationale <> ''", name="rationale_present"),
        Index("ix_cohort_operator_decisions_cohort_id", "cohort_id"),
    )

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sleeve_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes: Mapped[str | None] = mapped_column(
        ForeignKey(
            "cohort_operator_decisions.decision_id",
            name="fk_cohort_operator_decisions_supersedes",
            ondelete="RESTRICT",
        )
    )


class ResearchStrategySpec(Base):
    __tablename__ = "research_strategy_specs"
    __table_args__ = (UniqueConstraint("source_path", "source_spec_id"),)

    spec_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str | None] = mapped_column(Text)
    source_spec_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_created_at: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    specification: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    specification_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class PromotionVerdict(Base):
    __tablename__ = "promotion_verdicts"
    __table_args__ = (UniqueConstraint("source_path", "source_verdict_id"),)

    verdict_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str | None] = mapped_column(Text)
    source_verdict_id: Mapped[int | None] = mapped_column(BigInteger)
    strategy: Mapped[str] = mapped_column(String(128), nullable=False)
    universe: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_created_at: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    verdict_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class DailyPriceBar(Base):
    __tablename__ = "daily_price_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Any | None] = mapped_column(ExactNumeric())
    high: Mapped[Any | None] = mapped_column(ExactNumeric())
    low: Mapped[Any | None] = mapped_column(ExactNumeric())
    close: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)


class IntradayPriceBar(Base):
    __tablename__ = "intraday_price_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    minutes: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    observed_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_ts: Mapped[str] = mapped_column(Text, nullable=False)
    open: Mapped[Any | None] = mapped_column(ExactNumeric())
    high: Mapped[Any | None] = mapped_column(ExactNumeric())
    low: Mapped[Any | None] = mapped_column(ExactNumeric())
    close: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)


class MarketDataDailyEvidence(Base):
    """One complete, content-addressed intraday-derived daily candle."""

    __tablename__ = "market_data_daily_evidence"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "session_date",
            "constituent_digest",
            name="uq_market_data_daily_evidence_session_digest",
        ),
        CheckConstraint(
            "expected_interval_count > 0",
            name="expected_positive",
        ),
        CheckConstraint(
            "observed_interval_count = expected_interval_count",
            name="complete",
        ),
        CheckConstraint("volume >= 0", name="volume_nonnegative"),
    )

    dataset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_interval_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_interval_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_interval_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    final_interval_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    constituent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    open: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    high: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    low: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    close: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)


class MarketDataEvidenceConstituent(Base):
    """One exact five-minute constituent in a derived daily evidence set."""

    __tablename__ = "market_data_evidence_constituents"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "interval_at",
            name="uq_market_data_evidence_constituents_interval",
        ),
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint("volume >= 0", name="volume_nonnegative"),
    )

    dataset_id: Mapped[str] = mapped_column(
        ForeignKey(
            "market_data_daily_evidence.dataset_id",
            ondelete="CASCADE",
            # Explicit and short. The convention would generate 74 characters, which
            # PostgreSQL truncates to a hash suffix while the migration's unnamed FK gets
            # a `..._fkey` server default — so a create_all database and a migrated one
            # genuinely differ, and `compare_metadata` does not diff constraint names.
            name="fk_market_data_evidence_constituents_dataset",
        ),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    interval_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    open: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    high: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    low: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    close: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)


# ---------------------------------------------------------------------------
# Historical replay: RESEARCH evidence only.
#
# These four tables are a deliberately separate boundary from the official
# `market_data_daily_evidence` pair above. Nothing here references a cohort, sleeve,
# run, paper account, or official observation, and nothing there references these.
# A replay record therefore cannot be joined into official cohort evidence, cannot
# satisfy forward readiness, and cannot produce a fill, position, or cash movement.
# ---------------------------------------------------------------------------


class HistoricalReplayUniverse(Base):
    """A cohort-independent symbol set a replay download was requested for."""

    __tablename__ = "historical_replay_universes"

    universe_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    symbols: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)


class HistoricalReplaySession(Base):
    """One content-addressed research observation of one (symbol, session).

    ``replay_id`` is the content address, so an identical repeat ingestion converges on
    this row instead of writing a second one. Only ``last_seen_at`` is mutable.
    """

    __tablename__ = "historical_replay_sessions"
    __table_args__ = (
        Index(
            "ix_historical_replay_sessions_symbol_session",
            "symbol",
            "session_date",
        ),
        CheckConstraint("expected_bar_count >= 0", name="expected_nonnegative"),
        CheckConstraint("unique_bar_count >= 0", name="unique_nonnegative"),
        CheckConstraint(
            "unique_bar_count <= expected_bar_count",
            name="unique_within_expected",
        ),
        CheckConstraint(
            "status in ('complete', 'incomplete', 'unavailable')",
            name="status_known",
        ),
    )

    replay_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    universe_id: Mapped[str] = mapped_column(
        ForeignKey(
            "historical_replay_universes.universe_id",
            # Explicit and short: the convention would generate 71 characters, which
            # PostgreSQL truncates to a hash suffix while `create_all` and the migration
            # would then disagree on the name.
            name="fk_historical_replay_sessions_universe",
        ),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    raw_payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_bar_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    returned_bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    in_session_bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unique_bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    request_params: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class HistoricalReplayBar(Base):
    """One normalized in-session five-minute bar belonging to a replay observation."""

    __tablename__ = "historical_replay_bars"
    __table_args__ = (
        UniqueConstraint(
            "replay_id",
            "interval_at",
            name="uq_historical_replay_bars_interval",
        ),
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint("volume >= 0", name="volume_nonnegative"),
    )

    replay_id: Mapped[str] = mapped_column(
        ForeignKey(
            "historical_replay_sessions.replay_id",
            ondelete="CASCADE",
            name="fk_historical_replay_bars_session",
        ),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    interval_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    open: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    high: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    low: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    close: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)


class HistoricalReplayObservation(Base):
    """Append-only ingestion log making correction history explicit.

    Revision 1 is the first content ever recorded for a (symbol, session). Every later
    revision is a provider correction that names the exact observation it replaced, so a
    changed payload is visible rather than silently overwriting research evidence.
    """

    __tablename__ = "historical_replay_observations"
    # Deliberately *no* uniqueness on (symbol, session_date, replay_id): a provider that
    # corrects a session and later reverts to its earlier content has produced two real
    # correction events, and hiding the second would misreport the provider's behavior.
    # Idempotency is enforced where it belongs — identical content can only ever be one
    # `historical_replay_sessions` row, because `replay_id` is that table's primary key.
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "(revision = 1) = (previous_replay_id is null)",
            name="revision_one_is_first",
        ),
        CheckConstraint(
            "previous_replay_id is null or previous_replay_id <> replay_id",
            name="correction_changes",
        ),
    )

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    replay_id: Mapped[str] = mapped_column(
        ForeignKey(
            "historical_replay_sessions.replay_id",
            ondelete="CASCADE",
            name="fk_historical_replay_observations_session",
        ),
        nullable=False,
    )
    previous_replay_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "historical_replay_sessions.replay_id",
            ondelete="CASCADE",
            name="fk_historical_replay_observations_previous",
        )
    )
    observed_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)


class SecFact(Base):
    __tablename__ = "sec_facts"
    __table_args__ = (
        UniqueConstraint("ticker", "concept", "unit", "period_end", "filed", "accession"),
    )

    sec_fact_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    cik: Mapped[int] = mapped_column(BigInteger, nullable=False)
    concept: Mapped[str] = mapped_column(String(256), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(32))
    form: Mapped[str | None] = mapped_column(String(32))
    filed: Mapped[date] = mapped_column(Date, nullable=False)
    accession: Mapped[str] = mapped_column(String(64), nullable=False)
    frame: Mapped[str | None] = mapped_column(String(64))
    source_path: Mapped[str | None] = mapped_column(Text)


Index(
    "ix_sec_facts_point_in_time",
    SecFact.ticker,
    SecFact.concept,
    SecFact.filed,
)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (UniqueConstraint("source_path", "source_event_id"),)

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str | None] = mapped_column(Text)
    source_event_id: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    source_ts: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    web_searches: Mapped[int] = mapped_column(Integer, nullable=False)
    cost: Mapped[Any] = mapped_column(ExactNumeric(), nullable=False)


class MigrationRun(Base):
    __tablename__ = "migration_runs"

    migration_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_set_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    code_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    backup_path_hash: Mapped[str | None] = mapped_column(String(64))
    verification_status: Mapped[str | None] = mapped_column(String(32))


class MigrationSource(Base):
    __tablename__ = "migration_sources"

    migration_id: Mapped[str] = mapped_column(
        ForeignKey("migration_runs.migration_id", ondelete="CASCADE"), primary_key=True
    )
    source_path: Mapped[str] = mapped_column(Text, primary_key=True)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int | None] = mapped_column(Integer)
    table_counts: Mapped[dict[str, int]] = mapped_column(JSON_VALUE, nullable=False)
    table_checksums: Mapped[dict[str, str]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    completed_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    error_code: Mapped[str | None] = mapped_column(String(64))


class MigrationTableResult(Base):
    __tablename__ = "migration_table_results"

    migration_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_path: Mapped[str] = mapped_column(Text, primary_key=True)
    source_table: Mapped[str] = mapped_column(String(128), primary_key=True)
    destination_table: Mapped[str] = mapped_column(String(128), nullable=False)
    source_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    destination_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["migration_id", "source_path"],
            ["migration_sources.migration_id", "migration_sources.source_path"],
            ondelete="CASCADE",
        ),
    )
