"""Record execution-timing configuration and observation lineage.

Revision ID: 20260730_0005
Revises: 20260730_0004
Create Date: 2026-07-30

Declarative on purpose: it never defers to current model metadata and never changes an
existing revision.

Every column added here is nullable or server-defaulted, and the upgrade writes no row.
That is the whole point. Observations recorded before issue #79 ran the close-marked
model, where the signal session and the execution session are the same session and
``session_date``/``decision_time``/``valuation_time`` already say so. Leaving the new
session/timestamp lineage NULL and the method identity empty for those rows is the
truthful record; back-filling them would restate history in terms of a distinction the
experiment did not make.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from schwab_trader.storage.types import AwareTimestamp

revision = "20260730_0005"
down_revision = "20260730_0004"
branch_labels = None
depends_on = None

_OBSERVATIONS = "official_daily_observations"
_SLEEVES = "sleeves"

_OBSERVATION_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "execution_methodology",
        sa.Column("execution_methodology", sa.String(128), nullable=False, server_default=""),
    ),
    ("signal_session_date", sa.Column("signal_session_date", sa.Date())),
    ("execution_session_date", sa.Column("execution_session_date", sa.Date())),
    ("signal_time", sa.Column("signal_time", AwareTimestamp())),
    ("execution_time", sa.Column("execution_time", AwareTimestamp())),
)

_SLEEVE_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "execution_methodology",
        sa.Column("execution_methodology", sa.String(128), nullable=False, server_default=""),
    ),
)


def _existing(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_missing(table: str, columns: tuple[tuple[str, sa.Column[object]], ...]) -> None:
    present = _existing(table)
    if not present:
        return
    for name, column in columns:
        if name not in present:
            op.add_column(table, column)


def upgrade() -> None:
    _add_missing(_SLEEVES, _SLEEVE_COLUMNS)
    _add_missing(_OBSERVATIONS, _OBSERVATION_COLUMNS)


def downgrade() -> None:
    for table, columns in (
        (_OBSERVATIONS, _OBSERVATION_COLUMNS),
        (_SLEEVES, _SLEEVE_COLUMNS),
    ):
        present = _existing(table)
        for name, _ in reversed(columns):
            if name in present:
                op.drop_column(table, name)
