"""Create the cohort_alerts durable notification-transition table.

Revision ID: 20260728_0002
Revises: 20260723_0001
Create Date: 2026-07-28

The ``CohortAlert`` model landed in `3996970` (task #62) without a migration. Because
the baseline revision `20260723_0001` calls ``Base.metadata.create_all`` rather than
declaring DDL, the omission was invisible: every fresh database picked the table up from
the models and passed, while databases already stamped at the baseline never received it
and ``alembic upgrade head`` had nothing to apply. The shared PostgreSQL database was
left without ``cohort_alerts``, so alert de-duplication could not read its own record.

This revision is written **declaratively** on purpose. A migration must mean the same
thing forever, whatever the models later become; a migration that defers to
``Base.metadata`` silently changes meaning every time a model is added, which is the
defect this revision exists to correct. Keep future revisions explicit too - and see
``tests/test_migration_model_parity.py``, which fails when the chain and the models
diverge.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from schwab_trader.storage.types import AwareTimestamp

revision = "20260728_0002"
down_revision = "20260723_0001"
branch_labels = None
depends_on = None

_TABLE = "cohort_alerts"


def upgrade() -> None:
    # ``checkfirst`` equivalent: a database created from the models after the baseline
    # already has this table, so creating it again must not fail the upgrade.
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("alert_key", sa.String(64), primary_key=True),
        sa.Column(
            "cohort_id",
            sa.String(64),
            sa.ForeignKey("cohorts.cohort_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(80), nullable=False),
        sa.Column("scheduled_for", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("delivery", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", AwareTimestamp(), nullable=False),
        sa.Column("updated_at", AwareTimestamp(), nullable=False),
        sa.Column("delivered_at", AwareTimestamp(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        # The whole point of the table: "has this transition already been notified?" is
        # a database question, so repeated scheduler invocations cannot resend.
        # The name is spelled out because `op.create_table` does not apply the models'
        # naming convention, and a constraint named differently from the model is
        # exactly the silent drift `test_migrated_schema_has_no_drift_from_the_models`
        # exists to catch.
        sa.UniqueConstraint(
            "cohort_id",
            "session_id",
            "kind",
            name="uq_cohort_alerts_cohort_id_session_id_kind",
        ),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
