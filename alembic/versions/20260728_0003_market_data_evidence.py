"""Create durable intraday-derived daily evidence tables.

Revision ID: 20260728_0003
Revises: 20260728_0002
Create Date: 2026-07-28

The revision is deliberately declarative. It does not defer to current model metadata
and never changes the historical baseline migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from schwab_trader.storage.types import AwareTimestamp, ExactNumeric

revision = "20260728_0003"
down_revision = "20260728_0002"
branch_labels = None
depends_on = None

_EVIDENCE = "market_data_daily_evidence"
_CONSTITUENTS = "market_data_evidence_constituents"


def upgrade() -> None:
    names = set(sa.inspect(op.get_bind()).get_table_names())
    if _EVIDENCE not in names:
        op.create_table(
            _EVIDENCE,
            sa.Column("dataset_id", sa.String(128), primary_key=True),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("session_date", sa.Date(), nullable=False),
            sa.Column("retrieved_at", AwareTimestamp(), nullable=False),
            sa.Column("source", sa.String(64), nullable=False),
            sa.Column("expected_interval_count", sa.Integer(), nullable=False),
            sa.Column("observed_interval_count", sa.Integer(), nullable=False),
            sa.Column("first_interval_at", AwareTimestamp(), nullable=False),
            sa.Column("final_interval_at", AwareTimestamp(), nullable=False),
            sa.Column("constituent_digest", sa.String(64), nullable=False),
            sa.Column("open", ExactNumeric(), nullable=False),
            sa.Column("high", ExactNumeric(), nullable=False),
            sa.Column("low", ExactNumeric(), nullable=False),
            sa.Column("close", ExactNumeric(), nullable=False),
            sa.Column("volume", sa.BigInteger(), nullable=False),
            sa.UniqueConstraint(
                "symbol",
                "session_date",
                "constituent_digest",
                name="uq_market_data_daily_evidence_session_digest",
            ),
            sa.CheckConstraint(
                "expected_interval_count > 0",
                name="ck_market_data_daily_evidence_expected_positive",
            ),
            sa.CheckConstraint(
                "observed_interval_count = expected_interval_count",
                name="ck_market_data_daily_evidence_complete",
            ),
            sa.CheckConstraint(
                "volume >= 0",
                name="ck_market_data_daily_evidence_volume_nonnegative",
            ),
        )

    names = set(sa.inspect(op.get_bind()).get_table_names())
    if _CONSTITUENTS not in names:
        op.create_table(
            _CONSTITUENTS,
            sa.Column(
                "dataset_id",
                sa.String(128),
                sa.ForeignKey(
                    "market_data_daily_evidence.dataset_id",
                    ondelete="CASCADE",
                    # Must match the ORM name exactly. Left unnamed, PostgreSQL assigns
                    # `..._fkey` while `create_all` produces a truncated hash suffix, so
                    # the two ways of building this database would not be identical.
                    name="fk_market_data_evidence_constituents_dataset",
                ),
                nullable=False,
            ),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("interval_at", AwareTimestamp(), nullable=False),
            sa.Column("open", ExactNumeric(), nullable=False),
            sa.Column("high", ExactNumeric(), nullable=False),
            sa.Column("low", ExactNumeric(), nullable=False),
            sa.Column("close", ExactNumeric(), nullable=False),
            sa.Column("volume", sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint(
                "dataset_id",
                "ordinal",
                name="pk_market_data_evidence_constituents",
            ),
            sa.UniqueConstraint(
                "dataset_id",
                "interval_at",
                name="uq_market_data_evidence_constituents_interval",
            ),
            sa.CheckConstraint(
                "ordinal >= 0",
                name="ck_market_data_evidence_constituents_ordinal_nonnegative",
            ),
            sa.CheckConstraint(
                "volume >= 0",
                name="ck_market_data_evidence_constituents_volume_nonnegative",
            ),
        )


def downgrade() -> None:
    names = set(sa.inspect(op.get_bind()).get_table_names())
    if _CONSTITUENTS in names:
        op.drop_table(_CONSTITUENTS)
    names = set(sa.inspect(op.get_bind()).get_table_names())
    if _EVIDENCE in names:
        op.drop_table(_EVIDENCE)
