"""Create provider-neutral shared storage.

Revision ID: 20260723_0001
Revises:
Create Date: 2026-07-23

This revision originally called ``Base.metadata.create_all`` with no table list, which
made it a *moving target*: it built whatever the models happened to declare at the
moment it ran. Adding a model therefore silently changed what this revision would build
on a fresh database, while every database already stamped at it was left behind and
``alembic upgrade head`` had nothing to apply. That is how ``cohort_alerts`` reached
production missing (see revision ``20260728_0002``).

The table list below pins this revision to the twenty-four tables that existed when it
was authored, so it now means the same thing forever. The end state is unchanged for
every database: already-stamped ones never re-run it, and a fresh one gets these
twenty-four here plus each later table from its own explicit revision.

**Do not add a table to this list.** A new model gets a new revision.
"""

from __future__ import annotations

from alembic import op

from schwab_trader.storage.schema import Base

revision = "20260723_0001"
down_revision = None
branch_labels = None
depends_on = None

#: The schema as of 2026-07-23. Frozen: this is history, not a view of the models.
BASELINE_TABLES = (
    "cohort_members",
    "cohort_run_members",
    "cohort_runs",
    "cohorts",
    "daily_price_bars",
    "evaluation_cycles",
    "evaluation_decisions",
    "intraday_price_bars",
    "migration_runs",
    "migration_sources",
    "migration_table_results",
    "official_daily_observations",
    "official_session_leases",
    "paper_accounts",
    "paper_fills",
    "paper_orders",
    "paper_positions",
    "paper_unsettled_cash",
    "promotion_verdicts",
    "research_strategy_specs",
    "sec_facts",
    "sleeves",
    "storage_namespaces",
    "usage_events",
)


def _tables() -> list:
    return [Base.metadata.tables[name] for name in BASELINE_TABLES]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_tables(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_tables(), checkfirst=True)
