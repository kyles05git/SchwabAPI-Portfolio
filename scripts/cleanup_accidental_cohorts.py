#!/usr/bin/env python
"""Safe, allow-listed cleanup for cohorts accidentally created in the shared DB.

Context: a ``pytest`` run against a real ``.env`` once created bootstrap test cohorts
in the production Neon database (see issue #54). This tool removes *only* a hard-coded
allow-list of those accidental cohorts, and only after an explicit execution gate.

Safety design:
- Dry-run is the default and performs zero writes.
- Only the hard-coded ``ACCIDENTAL_COHORTS`` allow-list can ever be targeted.
- The real ``paper-first-2026-07-27`` cohort is a protected constant and is refused.
- Execution refuses if any targeted cohort has official observations, completed runs,
  fills, non-empty positions, or paper orders (anything beyond empty bootstrap records).
- Deletion runs inside one transaction and re-verifies the protected cohort is
  unchanged; any mismatch rolls the whole thing back.

This never reads secrets, never prints the database URL, and never touches a live-order
path. Configure ``SCHWAB_DATABASE_URL`` locally; do not pass it on the command line.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from schwab_trader.config import get_settings
from schwab_trader.storage import factory

# The only cohorts this tool may ever delete. Extending this list is a code change.
ACCIDENTAL_COHORTS: tuple[str, ...] = (
    "immutable-cohort",
    "omitted-cohort",
    "ready-cohort",
    "paper-first-2026-07-24",
)

# Never deletable. If any appears in the target set, the tool aborts.
PROTECTED_COHORTS: frozenset[str] = frozenset({"paper-first-2026-07-27"})

CONFIRM_PHRASE = "DELETE ACCIDENTAL TEST COHORTS"

_COHORT_KEYED = (
    "cohort_members",
    "cohort_runs",
    "cohort_run_members",
    "official_daily_observations",
    "official_session_leases",
)
_SLEEVE_KEYED = (
    "paper_accounts",
    "paper_orders",
    "paper_positions",
    "paper_unsettled_cash",
    "evaluation_cycles",
)
# Any of these being non-zero blocks execution: the cohort is not an empty artifact.
_BLOCKING = ("official_daily_observations", "paper_orders", "paper_positions", "cohort_runs")


def _table_exists(session: Session, table: str) -> bool:
    return session.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar() is not None


def _has_column(session: Session, table: str, column: str) -> bool:
    columns = {c["name"] for c in inspect(session.get_bind()).get_columns(table)}
    return column in columns


def _count(session: Session, table: str, column: str, values: Sequence[str]) -> int:
    if not values or not _table_exists(session, table):
        return 0
    return int(
        session.execute(
            text(f"SELECT count(*) FROM {table} WHERE {column} = ANY(:v)"),
            {"v": list(values)},
        ).scalar_one()
    )


def _inventory(session: Session) -> dict[str, dict[str, object]]:
    report: dict[str, dict[str, object]] = {}
    for cohort in ACCIDENTAL_COHORTS:
        present = _count(session, "cohorts", "cohort_id", [cohort]) > 0
        sleeve_ids = [
            row[0]
            for row in session.execute(
                text("SELECT sleeve_id FROM sleeves WHERE cohort_id = :c"), {"c": cohort}
            )
        ]
        counts: dict[str, int] = {"cohort_row": int(present), "sleeves": len(sleeve_ids)}
        for table in _COHORT_KEYED:
            counts[table] = _count(session, table, "cohort_id", [cohort])
        for table in _SLEEVE_KEYED:
            counts[table] = _count(session, table, "sleeve_id", sleeve_ids)
        report[cohort] = {
            "present": present,
            "counts": counts,
            "blocking": {k: counts[k] for k in _BLOCKING if counts.get(k, 0)},
        }
    return report


def _protected_snapshot(session: Session) -> dict[str, int]:
    cohort = next(iter(PROTECTED_COHORTS))
    tables = [
        "cohorts",
        "cohort_members",
        "cohort_runs",
        "cohort_run_members",
        "official_daily_observations",
        "sleeves",
    ]
    return {t: _count(session, t, "cohort_id", [cohort]) for t in tables}


def _delete_present(session: Session, present: Sequence[str]) -> None:
    sleeve_ids = [
        row[0]
        for row in session.execute(
            text("SELECT sleeve_id FROM sleeves WHERE cohort_id = ANY(:c)"),
            {"c": list(present)},
        )
    ]
    for table in _SLEEVE_KEYED:
        if _table_exists(session, table) and _has_column(session, table, "sleeve_id"):
            session.execute(
                text(f"DELETE FROM {table} WHERE sleeve_id = ANY(:v)"), {"v": sleeve_ids}
            )
    for table in _COHORT_KEYED:
        if _table_exists(session, table):
            session.execute(
                text(f"DELETE FROM {table} WHERE cohort_id = ANY(:c)"), {"c": list(present)}
            )
    session.execute(text("DELETE FROM sleeves WHERE cohort_id = ANY(:c)"), {"c": list(present)})
    session.execute(text("DELETE FROM cohorts WHERE cohort_id = ANY(:c)"), {"c": list(present)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Allow-listed accidental-cohort cleanup.")
    parser.add_argument(
        "--execute", action="store_true", help="Perform deletion (default: read-only dry run)."
    )
    parser.add_argument(
        "--confirm", default="", help=f'Required with --execute: exact "{CONFIRM_PHRASE}".'
    )
    args = parser.parse_args(argv)

    if PROTECTED_COHORTS & set(ACCIDENTAL_COHORTS):
        print("ABORT: allow-list overlaps a protected cohort.", file=sys.stderr)
        return 2

    database = factory.database(get_settings())
    if database is None:
        print("No shared database is configured (SCHWAB_DATABASE_URL unset).")
        return 0
    if database.dialect != "postgresql":
        print(f"Refusing to run against dialect '{database.dialect}'.", file=sys.stderr)
        return 2

    with database.session() as session:
        report = _inventory(session)
        protected_before = _protected_snapshot(session)

    print("=== Sanitized accidental-cohort cleanup inventory ===")
    present_cohorts = [c for c, info in report.items() if info["present"]]
    blocked = False
    for cohort, info in report.items():
        state = "PRESENT" if info["present"] else "absent"
        print(f"\n[{cohort}] {state}")
        if info["present"]:
            counts: dict[str, int] = info["counts"]  # type: ignore[assignment]
            for key, value in counts.items():
                print(f"    {key:32} {value}")
            if info["blocking"]:
                blocked = True
                print(f"    !! BLOCKING non-empty data: {info['blocking']}")
    print(f"\nProtected cohort snapshot (must stay unchanged): {protected_before}")

    if not present_cohorts:
        print("\nNothing to clean: no allow-listed accidental cohort is present.")
        return 0
    if not args.execute:
        print(
            "\nDRY RUN: no writes performed. Re-run with --execute and "
            f'--confirm "{CONFIRM_PHRASE}" to delete the PRESENT cohorts above.'
        )
        return 0
    if blocked:
        print(
            "\nABORT: a targeted cohort contains non-empty trading data; refusing to "
            "delete. Investigate before proceeding.",
            file=sys.stderr,
        )
        return 3
    if args.confirm != CONFIRM_PHRASE:
        print(f'\nABORT: --execute requires --confirm "{CONFIRM_PHRASE}".', file=sys.stderr)
        return 3

    with database.session() as session:  # one transaction (session.begin())
        _delete_present(session, present_cohorts)
        protected_after = _protected_snapshot(session)
        if protected_after != protected_before:
            raise RuntimeError(
                "Protected cohort changed during cleanup; rolling back. "
                f"before={protected_before} after={protected_after}"
            )

    print(f"\nDeleted accidental cohorts: {present_cohorts}")
    print(f"Protected cohort verified unchanged: {protected_before}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
