"""Create historical-replay research evidence tables.

Revision ID: 20260730_0004
Revises: 20260728_0003
Create Date: 2026-07-30

Declarative on purpose: it never defers to current model metadata and never changes an
existing revision. These tables are a research/replay boundary. They carry no foreign
key to a cohort, sleeve, run, paper account, or official observation, and none of those
reference them, so a replay record cannot masquerade as official cohort evidence.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from schwab_trader.storage.schema import JSON_VALUE
from schwab_trader.storage.types import AwareTimestamp, ExactNumeric

revision = "20260730_0004"
down_revision = "20260728_0003"
branch_labels = None
depends_on = None

_UNIVERSES = "historical_replay_universes"
_SESSIONS = "historical_replay_sessions"
_BARS = "historical_replay_bars"
_OBSERVATIONS = "historical_replay_observations"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _UNIVERSES not in _tables():
        op.create_table(
            _UNIVERSES,
            sa.Column("universe_id", sa.String(64), nullable=False),
            sa.Column("label", sa.String(128), nullable=False),
            sa.Column("symbols", JSON_VALUE, nullable=False),
            sa.Column("created_at", AwareTimestamp(), nullable=False),
            sa.PrimaryKeyConstraint("universe_id", name="pk_historical_replay_universes"),
        )

    if _SESSIONS not in _tables():
        op.create_table(
            _SESSIONS,
            sa.Column("replay_id", sa.String(128), nullable=False),
            sa.Column("universe_id", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("session_date", sa.Date(), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("source", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("retrieved_at", AwareTimestamp(), nullable=False),
            sa.Column("first_seen_at", AwareTimestamp(), nullable=False),
            sa.Column("last_seen_at", AwareTimestamp(), nullable=False),
            sa.Column("raw_payload_digest", sa.String(64), nullable=False),
            sa.Column("normalized_bar_digest", sa.String(64), nullable=False),
            sa.Column("expected_bar_count", sa.Integer(), nullable=False),
            sa.Column("returned_bar_count", sa.Integer(), nullable=False),
            sa.Column("in_session_bar_count", sa.Integer(), nullable=False),
            sa.Column("unique_bar_count", sa.Integer(), nullable=False),
            sa.Column("request_params", JSON_VALUE, nullable=False),
            sa.Column("validation", JSON_VALUE, nullable=False),
            sa.Column("error", sa.Text()),
            sa.PrimaryKeyConstraint("replay_id", name="pk_historical_replay_sessions"),
            sa.ForeignKeyConstraint(
                ["universe_id"],
                [f"{_UNIVERSES}.universe_id"],
                # Must match the ORM name exactly: unnamed, the convention emits 71
                # characters that PostgreSQL truncates to a hash suffix while this
                # migration would get a server-assigned `..._fkey`.
                name="fk_historical_replay_sessions_universe",
            ),
            sa.CheckConstraint(
                "expected_bar_count >= 0",
                name="ck_historical_replay_sessions_expected_nonnegative",
            ),
            sa.CheckConstraint(
                "unique_bar_count >= 0",
                name="ck_historical_replay_sessions_unique_nonnegative",
            ),
            sa.CheckConstraint(
                "unique_bar_count <= expected_bar_count",
                name="ck_historical_replay_sessions_unique_within_expected",
            ),
            sa.CheckConstraint(
                "status in ('complete', 'incomplete', 'unavailable')",
                name="ck_historical_replay_sessions_status_known",
            ),
        )
        op.create_index(
            "ix_historical_replay_sessions_symbol_session",
            _SESSIONS,
            ["symbol", "session_date"],
        )

    if _BARS not in _tables():
        op.create_table(
            _BARS,
            sa.Column("replay_id", sa.String(128), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("interval_at", AwareTimestamp(), nullable=False),
            sa.Column("open", ExactNumeric(), nullable=False),
            sa.Column("high", ExactNumeric(), nullable=False),
            sa.Column("low", ExactNumeric(), nullable=False),
            sa.Column("close", ExactNumeric(), nullable=False),
            sa.Column("volume", sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint(
                "replay_id",
                "ordinal",
                name="pk_historical_replay_bars",
            ),
            sa.ForeignKeyConstraint(
                ["replay_id"],
                [f"{_SESSIONS}.replay_id"],
                ondelete="CASCADE",
                name="fk_historical_replay_bars_session",
            ),
            sa.UniqueConstraint(
                "replay_id",
                "interval_at",
                name="uq_historical_replay_bars_interval",
            ),
            sa.CheckConstraint(
                "ordinal >= 0",
                name="ck_historical_replay_bars_ordinal_nonnegative",
            ),
            sa.CheckConstraint(
                "volume >= 0",
                name="ck_historical_replay_bars_volume_nonnegative",
            ),
        )

    if _OBSERVATIONS not in _tables():
        op.create_table(
            _OBSERVATIONS,
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("session_date", sa.Date(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("replay_id", sa.String(128), nullable=False),
            sa.Column("previous_replay_id", sa.String(128)),
            sa.Column("observed_at", AwareTimestamp(), nullable=False),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.PrimaryKeyConstraint(
                "symbol",
                "session_date",
                "revision",
                name="pk_historical_replay_observations",
            ),
            sa.ForeignKeyConstraint(
                ["replay_id"],
                [f"{_SESSIONS}.replay_id"],
                ondelete="CASCADE",
                name="fk_historical_replay_observations_session",
            ),
            sa.ForeignKeyConstraint(
                ["previous_replay_id"],
                [f"{_SESSIONS}.replay_id"],
                ondelete="CASCADE",
                name="fk_historical_replay_observations_previous",
            ),
            # No uniqueness on (symbol, session_date, replay_id): a provider correction
            # that later reverts to earlier content is two real events. Idempotency is
            # enforced by `historical_replay_sessions.replay_id` being a primary key.
            sa.CheckConstraint(
                "revision >= 1",
                name="ck_historical_replay_observations_revision_positive",
            ),
            sa.CheckConstraint(
                "(revision = 1) = (previous_replay_id is null)",
                name="ck_historical_replay_observations_revision_one_is_first",
            ),
            sa.CheckConstraint(
                "previous_replay_id is null or previous_replay_id <> replay_id",
                name="ck_historical_replay_observations_correction_changes",
            ),
        )


def downgrade() -> None:
    for name in (_OBSERVATIONS, _BARS, _SESSIONS, _UNIVERSES):
        if name in _tables():
            op.drop_table(name)
