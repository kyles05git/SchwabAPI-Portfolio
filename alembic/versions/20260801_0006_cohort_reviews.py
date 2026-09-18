"""Create the durable cohort accounting-review and operator-decision tables.

Revision ID: 20260801_0006
Revises: 20260730_0005
Create Date: 2026-08-01

Declarative on purpose: it never defers to current model metadata and never changes an
existing revision. Every constraint and index is named explicitly and identically to the
ORM declaration, because ``compare_metadata`` does not diff constraint names and a
divergence there produces two databases that are genuinely not the same.

These tables are additive and independent. They carry no foreign key to ``cohorts``,
``sleeves``, or ``official_daily_observations`` — in the local layout those live in a
separate file-backed registry rather than in this database, so the constraint could only
exist on one of the two supported backends. Referential validity is enforced for both by
:class:`schwab_trader.cohort_review.CohortReviewService` before any row is written.

The upgrade writes no row and touches no existing table, so applying it to a database
holding live cohort records changes nothing about those records. The downgrade drops only
the three tables this revision creates; because every review record is append-only and
lives entirely inside them, a downgrade discards recorded review evidence and nothing
else. Export the review (``cohort review show --json``) before downgrading a database
whose review has begun.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from schwab_trader.storage.types import AwareTimestamp

revision = "20260801_0006"
down_revision = "20260730_0005"
branch_labels = None
depends_on = None

_CHECKS = "cohort_accounting_checks"
_NOTES = "cohort_review_notes"
_DECISIONS = "cohort_operator_decisions"

_AREAS = "('cash', 'positions', 'valuation')"
_FINDINGS = "('matched', 'difference')"
_ACTIONS = "('keep', 'modify', 'pause', 'retire')"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    present = _tables()

    if _CHECKS not in present:
        op.create_table(
            _CHECKS,
            sa.Column("entry_id", sa.String(64), nullable=False),
            sa.Column("cohort_id", sa.String(64), nullable=False),
            sa.Column("sleeve_id", sa.String(64), nullable=False),
            sa.Column("observation_key", sa.String(256), nullable=False),
            sa.Column("session_date", sa.Date(), nullable=False),
            sa.Column("area", sa.String(16), nullable=False),
            sa.Column("finding", sa.String(16), nullable=False),
            sa.Column("summary", sa.Text()),
            sa.Column("explanation", sa.Text()),
            sa.Column("recorded_at", AwareTimestamp(), nullable=False),
            sa.Column("recorded_by", sa.String(64), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("supersedes", sa.String(64)),
            sa.PrimaryKeyConstraint("entry_id", name="pk_cohort_accounting_checks"),
            # The append-only guarantee in one constraint: two writers appending the
            # same revision for one identity cannot both succeed, so a correction can
            # never be silently lost to a concurrent one.
            sa.UniqueConstraint(
                "cohort_id",
                "observation_key",
                "area",
                "revision",
                name="uq_cohort_accounting_checks_entry",
            ),
            sa.ForeignKeyConstraint(
                ["supersedes"],
                [f"{_CHECKS}.entry_id"],
                ondelete="RESTRICT",
                name="fk_cohort_accounting_checks_supersedes",
            ),
            sa.CheckConstraint(
                f"area IN {_AREAS}",
                name="ck_cohort_accounting_checks_area",
            ),
            sa.CheckConstraint(
                f"finding IN {_FINDINGS}",
                name="ck_cohort_accounting_checks_finding",
            ),
            sa.CheckConstraint(
                "revision >= 0",
                name="ck_cohort_accounting_checks_revision_nonnegative",
            ),
            sa.CheckConstraint(
                "finding <> 'difference' OR (summary IS NOT NULL AND summary <> '')",
                name="ck_cohort_accounting_checks_difference_has_summary",
            ),
        )
        op.create_index("ix_cohort_accounting_checks_cohort_id", _CHECKS, ["cohort_id"])

    if _NOTES not in present:
        op.create_table(
            _NOTES,
            sa.Column("note_id", sa.String(64), nullable=False),
            sa.Column("cohort_id", sa.String(64), nullable=False),
            sa.Column("sleeve_id", sa.String(64)),
            sa.Column("observation_key", sa.String(256)),
            sa.Column("note", sa.Text(), nullable=False),
            sa.Column("recorded_at", AwareTimestamp(), nullable=False),
            sa.Column("recorded_by", sa.String(64), nullable=False),
            sa.PrimaryKeyConstraint("note_id", name="pk_cohort_review_notes"),
        )
        op.create_index("ix_cohort_review_notes_cohort_id", _NOTES, ["cohort_id"])

    if _DECISIONS not in present:
        op.create_table(
            _DECISIONS,
            sa.Column("decision_id", sa.String(64), nullable=False),
            sa.Column("cohort_id", sa.String(64), nullable=False),
            sa.Column("sleeve_id", sa.String(64), nullable=False),
            sa.Column("action", sa.String(16), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("recorded_at", AwareTimestamp(), nullable=False),
            sa.Column("recorded_by", sa.String(64), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("supersedes", sa.String(64)),
            sa.PrimaryKeyConstraint("decision_id", name="pk_cohort_operator_decisions"),
            sa.UniqueConstraint(
                "cohort_id",
                "sleeve_id",
                "revision",
                name="uq_cohort_operator_decisions_entry",
            ),
            sa.ForeignKeyConstraint(
                ["supersedes"],
                [f"{_DECISIONS}.decision_id"],
                ondelete="RESTRICT",
                name="fk_cohort_operator_decisions_supersedes",
            ),
            sa.CheckConstraint(
                f"action IN {_ACTIONS}",
                name="ck_cohort_operator_decisions_action",
            ),
            sa.CheckConstraint(
                "revision >= 0",
                name="ck_cohort_operator_decisions_revision_nonnegative",
            ),
            # A decision with no reason is the thing the 30-session review exists to
            # prevent, so the database refuses one too.
            sa.CheckConstraint(
                "rationale <> ''",
                name="ck_cohort_operator_decisions_rationale_present",
            ),
        )
        op.create_index("ix_cohort_operator_decisions_cohort_id", _DECISIONS, ["cohort_id"])


def downgrade() -> None:
    present = _tables()
    for table, index in (
        (_DECISIONS, "ix_cohort_operator_decisions_cohort_id"),
        (_NOTES, "ix_cohort_review_notes_cohort_id"),
        (_CHECKS, "ix_cohort_accounting_checks_cohort_id"),
    ):
        if table in present:
            op.drop_index(index, table_name=table)
            op.drop_table(table)
