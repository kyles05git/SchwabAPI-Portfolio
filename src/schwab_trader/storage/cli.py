"""Safe administrative commands for shared-storage inventory and migration."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from schwab_trader.config import Settings, get_settings
from schwab_trader.storage import backup as backup_ops
from schwab_trader.storage import factory
from schwab_trader.storage import health as health_ops
from schwab_trader.storage import recovery as recovery_ops
from schwab_trader.storage.database import Database
from schwab_trader.storage.inventory import InventoryReport, build_inventory
from schwab_trader.storage.migration import (
    MigrationConflictError,
    execute_migration,
    preflight_migration,
    verify_migration,
)
from schwab_trader.storage.schema import MigrationRun, MigrationTableResult
from schwab_trader.storage.snapshots import (
    create_snapshot_set,
    load_snapshot_set,
    verify_snapshot_set,
)

storage_app = typer.Typer(
    help="Inventory, migrate, and verify shared application storage.",
    no_args_is_help=True,
)
console = Console()


def _abort(message: str) -> NoReturn:
    console.print(f"[red]Storage operation stopped:[/] {message}")
    raise typer.Exit(code=1)


def _shared_postgres(settings: Settings) -> Database:
    database = factory.database(settings)
    if database is None:
        _abort(
            "set SCHWAB_DATABASE_URL locally; do not pass credentials on the command line"
        )
    if database.dialect != "postgresql":
        _abort("the migration destination must be PostgreSQL")
    return database


def _revision(root: Path) -> str:
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        _abort("a clean Git revision is required for migration execution")
    if dirty:
        _abort("migration execution requires a clean, reviewed checkout")
    if len(revision) != 40:
        _abort("could not identify the reviewed Git revision")
    return revision


def _inventory_table(report: InventoryReport) -> Table:
    table = Table(title="Sanitized SQLite migration inventory")
    table.add_column("Source")
    table.add_column("Kind")
    table.add_column("Tables", justify="right")
    table.add_column("Rows", justify="right")
    table.add_column("Eligible")
    table.add_column("Content hash")
    for source in report.sources:
        table.add_row(
            source.relative_path,
            source.kind,
            str(len(source.tables)),
            str(sum(source.table_counts.values())),
            "yes" if source.eligible else source.eligibility_reason,
            source.content_hash,
        )
    return table


@storage_app.command("inventory")
def inventory(
    source_root: Path = typer.Option(
        Path("."),
        "--source-root",
        help="Repository root containing the allow-listed data directory.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print the full sanitized schema/count/checksum report.",
    ),
) -> None:
    """Discover allow-listed SQLite sources without reading raw records."""
    try:
        report = build_inventory(source_root)
    except Exception as exc:
        _abort(f"inventory failed closed ({type(exc).__name__})")
    if json_output:
        console.print_json(data=report.sanitized_payload())
    else:
        console.print(_inventory_table(report))
        console.print(f"Source-set hash: {report.source_set_hash}")
        console.print(
            f"Eligible sources: {len(report.eligible_sources)} of {len(report.sources)}"
        )


@storage_app.command("migrate")
def migrate(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Inventory and check the destination without writing either side.",
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Snapshot and import after review and merge.",
    ),
    writers_stopped: bool = typer.Option(
        False,
        "--writers-stopped",
        help="Assert all cohort runners and paper writers are stopped.",
    ),
    source_root: Path = typer.Option(
        Path("."),
        "--source-root",
        help="Reviewed checkout containing the authoritative data directory.",
    ),
) -> None:
    """Dry-run or execute the resumable, fail-closed SQLite migration."""
    if dry_run == execute:
        _abort("choose exactly one of --dry-run or --execute")
    settings = get_settings()
    database = _shared_postgres(settings)
    try:
        report = build_inventory(source_root)
        preflight = preflight_migration(database, report)
    except MigrationConflictError as exc:
        _abort(str(exc))
    except Exception as exc:
        _abort(f"preflight failed closed ({type(exc).__name__})")

    console.print(_inventory_table(report))
    console.print(
        f"Preflight passed: {preflight.eligible_sources} sources, "
        f"{preflight.source_tables} tables, {preflight.source_rows} rows; "
        f"{'resumable prior attempt' if preflight.resumable else 'empty source scope'}."
    )
    if dry_run:
        console.print("[green]Dry run complete; no source or destination was changed.[/]")
        return
    if not writers_stopped:
        _abort("--execute requires --writers-stopped after stopping every paper writer")

    revision = _revision(source_root.resolve())
    try:
        snapshot_root, snapshot = create_snapshot_set(
            report,
            settings.migration_backup_dir,
            writers_stopped=True,
        )
        outcome = execute_migration(
            database,
            snapshot_root,
            snapshot,
            code_revision=revision,
        )
    except (MigrationConflictError, ValueError) as exc:
        _abort(str(exc))
    except Exception as exc:
        _abort(f"migration failed closed ({type(exc).__name__})")
    console.print(
        f"[green]Migration {outcome.migration_id} completed and "
        f"{outcome.verification_status} verification.[/]"
    )
    console.print("Source SQLite files were not modified or deleted.")


def _latest_migration_id(database: Database) -> str:
    with database.session() as session:
        migration_id = session.scalar(
            select(MigrationRun.migration_id)
            .order_by(MigrationRun.started_at.desc())
            .limit(1)
        )
    if migration_id is None:
        _abort("the destination has no recorded migration")
    return migration_id


@storage_app.command("verify")
def verify(
    migration_id: str = typer.Option(
        "",
        "--migration-id",
        help="Recorded migration ID; defaults to the most recent.",
    ),
) -> None:
    """Re-run count, checksum, relationship, uniqueness, and financial checks."""
    settings = get_settings()
    database = _shared_postgres(settings)
    selected_id = migration_id.strip() or _latest_migration_id(database)
    try:
        snapshot_root = settings.migration_backup_dir.resolve() / selected_id
        snapshot = load_snapshot_set(snapshot_root)
        if snapshot.migration_id != selected_id:
            raise ValueError("snapshot migration identity mismatch")
        verify_snapshot_set(snapshot_root, snapshot)
        status = verify_migration(database, selected_id)
        with database.session() as session:
            results = list(
                session.scalars(
                    select(MigrationTableResult)
                    .where(MigrationTableResult.migration_id == selected_id)
                    .order_by(
                        MigrationTableResult.source_path,
                        MigrationTableResult.source_table,
                    )
                )
            )
    except KeyError:
        _abort("the requested migration ID does not exist")
    except Exception as exc:
        _abort(f"verification failed closed ({type(exc).__name__})")

    table = Table(title=f"Migration verification: {selected_id}")
    table.add_column("Source")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    table.add_column("Checksum")
    table.add_column("Status")
    for result in results:
        table.add_row(
            result.source_path,
            result.source_table,
            f"{result.source_count}/{result.destination_count}",
            "match"
            if result.source_checksum == result.destination_checksum
            else "mismatch",
            result.status,
        )
    console.print(table)
    if status != "passed":
        _abort("one or more migration checks failed; do not cut over")
    console.print("[green]Verification passed.[/]")


# --------------------------------------------------------------------------------
# Health, backup, and recovery inspection (issue #64)
#
# Every command below is read-only against the database. ``backup --execute`` is the
# only one that writes anything at all, and it writes only to an explicitly named
# destination directory outside the repository.
# --------------------------------------------------------------------------------


_CHECK_COLOUR = {
    health_ops.CheckStatus.OK: "green",
    health_ops.CheckStatus.WARN: "yellow",
    health_ops.CheckStatus.FAIL: "red",
    health_ops.CheckStatus.UNKNOWN: "magenta",
    health_ops.CheckStatus.NOT_APPLICABLE: "dim",
}

_STATUS_COLOUR = {
    health_ops.HealthStatus.HEALTHY: "green",
    health_ops.HealthStatus.UNHEALTHY: "red",
    health_ops.HealthStatus.DISCONNECTED: "red",
    health_ops.HealthStatus.INDETERMINATE: "magenta",
}


def _count(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def _render_health(report: health_ops.HealthReport) -> None:
    """Print the same facts the JSON document carries, in the same order.

    Both renderings read from one :class:`HealthReport`, so they cannot drift. Values
    are escaped because a cohort id or table name containing square brackets would
    otherwise be swallowed by Rich's markup parser.
    """
    summary = Table(title="Shared storage health", show_header=False, box=None)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    summary.add_row("Status", f"[{_STATUS_COLOUR[report.status]}]{report.status.value}[/]")
    summary.add_row("Exit code", str(report.exit_code))
    summary.add_row("Contract version", str(report.contract_version))
    summary.add_row("Backend", escape(report.backend.value))
    summary.add_row("TLS policy", escape(report.tls_policy.value))
    summary.add_row("Connected", "yes" if report.connected else "no")
    summary.add_row("Server", escape(report.server_version or "unreported"))
    summary.add_row("Role", escape(report.role.value))
    summary.add_row(
        "Schema revision",
        escape(
            f"{', '.join(report.alembic_current) or 'none'} "
            f"(expected {report.alembic_expected_head or 'unknown'})"
        ),
    )
    summary.add_row("Cohorts", _count(report.cohorts))
    summary.add_row("Official runs", _count(report.official_runs))
    summary.add_row("Official observations", _count(report.official_observations))
    summary.add_row("Leases", f"{report.leases.current} current, {report.leases.stale} stale")
    if report.latest_run is not None:
        summary.add_row(
            "Latest official run",
            escape(
                f"{report.latest_run.cohort_id}/{report.latest_run.scheduled_for} "
                f"{report.latest_run.status} "
                f"{report.latest_run.completed_members}/{report.latest_run.expected_members}"
            ),
        )
    console.print(summary)

    checks = Table(title="Checks")
    checks.add_column("Check")
    checks.add_column("Status")
    checks.add_column("Detail")
    for check in report.checks:
        colour = _CHECK_COLOUR[check.status]
        checks.add_row(
            escape(check.name),
            f"[{colour}]{check.status.value}[/]",
            escape(check.detail),
        )
    console.print(checks)
    console.print(f"Next action: {escape(report.next_action)}")


@storage_app.command("health")
def health(
    json_output: bool = typer.Option(
        False, "--json", help="Print the stable, versioned JSON health document."
    ),
    require_writer: bool = typer.Option(
        False,
        "--require-writer",
        help=(
            "Fail unless the connected role can write every runtime table. Use this on "
            "the designated official-scheduler machine."
        ),
    ),
) -> None:
    """Report sanitized shared-storage health. Read-only; it never repairs anything.

    Exit codes: 0 healthy, 1 unhealthy or drifted, 2 disconnected, 3 indeterminate.
    """
    settings = get_settings()
    try:
        report = health_ops.assess_health(settings, require_writer=require_writer)
    except Exception as exc:
        # Fail closed and say nothing about the connection: a SQLAlchemy error message
        # can carry the URL, so only the exception class is reported.
        _abort(f"health assessment failed closed ({type(exc).__name__})")
    if json_output:
        console.print_json(data=report.sanitized_payload())
    else:
        _render_health(report)
    raise typer.Exit(code=report.exit_code)


@storage_app.command("doctor")
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Print the JSON document."),
    require_writer: bool = typer.Option(
        False, "--require-writer", help="Fail unless the role can write runtime tables."
    ),
) -> None:
    """Alias for 'storage health', for operators who reach for 'doctor' first."""
    health(json_output=json_output, require_writer=require_writer)


def _version(major: int | None) -> str:
    return "unknown" if major is None else str(major)


def _render_plan(plan: backup_ops.BackupPlan) -> None:
    table = Table(title="Backup preflight", show_header=False, box=None)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Backend", escape(plan.backend.value))
    table.add_row("Destination", escape(plan.destination))
    table.add_row("Would create", escape(plan.artifact_name))
    table.add_row("Manifest", escape(plan.manifest_name))
    # `tool` is None for two unrelated reasons: SQLite needs no external tool, and a
    # PostgreSQL preflight could not find pg_dump. Telling an operator whose PostgreSQL
    # tools are missing that the backup will use the "sqlite online backup API" names
    # the wrong backend entirely. Observed against a real preflight with the tools off
    # PATH while validating issue #108.
    if plan.tool:
        tool_label = plan.tool
    elif plan.backend is health_ops.Backend.SQLITE:
        tool_label = "sqlite online backup API"
    else:
        tool_label = "pg_dump not found"
    table.add_row("Tool", escape(tool_label))
    table.add_row("Schema revision", escape(plan.alembic_revision or "unknown"))
    table.add_row("Tables counted", str(len(plan.table_counts)))
    if plan.backend is not health_ops.Backend.SQLITE:
        table.add_row(
            "Client / server major",
            escape(
                f"pg_dump {_version(plan.pg_dump_major)}, "
                f"pg_restore {_version(plan.pg_restore_major)}, "
                f"server {_version(plan.server_major)}"
            ),
        )
    table.add_row("Ready", "yes" if plan.ready else "no")
    console.print(table)
    for blocker in plan.blockers:
        console.print(f"[red]Blocker:[/] {escape(blocker)}")


@storage_app.command("backup")
def backup(
    destination: Path = typer.Option(
        ...,
        "--destination",
        help="Existing directory to write the artifact into. Must be an absolute path.",
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually produce the artifact. Without this the command only preflights.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the JSON document."),
) -> None:
    """Preflight (default) or produce a verified backup of application-owned storage.

    Dry run is the default on purpose: the preflight reports the exact artifact name
    and destination so an operator can confirm both before anything is written.
    """
    settings = get_settings()
    try:
        plan = backup_ops.plan_backup(settings, destination)
    except backup_ops.BackupError as exc:
        _abort(str(exc))
    except Exception as exc:
        _abort(f"backup preflight failed closed ({type(exc).__name__})")

    if not execute:
        if json_output:
            console.print_json(data=plan.sanitized_payload())
        else:
            _render_plan(plan)
        if not plan.ready:
            raise typer.Exit(code=1)
        console.print("[green]Dry run complete; nothing was written.[/]")
        return

    if not plan.ready:
        _abort("; ".join(plan.blockers))

    try:
        outcome = backup_ops.execute_backup(settings, destination)
    except backup_ops.BackupError as exc:
        _abort(str(exc))
    except Exception as exc:
        _abort(f"backup failed closed ({type(exc).__name__})")

    if json_output:
        console.print_json(data=outcome.sanitized_payload())
    else:
        console.print(
            f"[green]Backup written:[/] {escape(outcome.manifest.artifact_name)} "
            f"({outcome.manifest.artifact_bytes} bytes)"
        )
        console.print(f"SHA-256: {outcome.manifest.sha256}")
        console.print(f"Manifest: {escape(outcome.manifest_name)}")
        console.print(
            f"Checked: {escape(outcome.manifest.verification_level.value)} — "
            f"{escape(outcome.manifest.verification_detail)}"
        )
        # Said plainly and every time. "Verified" reads as "known to restore", and
        # nothing this command does establishes that.
        console.print(
            "[yellow]Not restore-tested.[/] No restore was performed, so this artifact "
            "is not yet known to load into a database. Rehearse it per "
            "docs/operations/storage-backup-restore.md section 6."
        )
        console.print("Old backups were not deleted.")


@storage_app.command("backup-verify")
def backup_verify(
    manifest: Path = typer.Option(
        ...,
        "--manifest",
        help="Path to a '*.manifest.json' written by `storage backup --execute`.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the JSON document."),
) -> None:
    """Re-verify an existing artifact against its manifest. Read-only.

    Run this before trusting an artifact enough to restore it into a scratch database.
    """
    try:
        result = backup_ops.verify_artifact(manifest)
    except Exception as exc:
        _abort(f"artifact verification failed closed ({type(exc).__name__})")
    if json_output:
        console.print_json(data=result.sanitized_payload())
    else:
        console.print(f"Artifact: {escape(result.artifact_name or 'unknown')}")
        console.print(f"Result: {escape(result.result.value)}")
        console.print(
            f"Checked: {escape(result.verification_level.value)} — "
            f"{escape(result.verification_detail)}"
        )
        console.print(
            "[yellow]Not restore-tested.[/] This command reads the artifact; it never "
            "restores one."
        )
        for reason in result.reasons:
            console.print(f"[red]Rejected:[/] {escape(reason)}")
    if result.result is not backup_ops.Verification.PASSED:
        raise typer.Exit(code=1)


def _lease_label(lease: recovery_ops.LeaseFacts) -> str:
    if not lease.present:
        return "none recorded"
    state = "released" if lease.released else ("expired" if lease.expired else "held")
    age = "unknown age" if lease.age_seconds is None else f"{lease.age_seconds}s old"
    return f"{state}, {age}"


def _render_recovery(report: recovery_ops.RecoveryReport) -> None:
    summary = Table(title="Cohort session recovery inspection", show_header=False, box=None)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    summary.add_row("State", escape(report.state.value))
    summary.add_row("Exit code", str(report.exit_code))
    summary.add_row("Contract version", str(report.contract_version))
    summary.add_row("Cohort", escape(report.cohort_id))
    summary.add_row("Scheduled for", escape(report.scheduled_for))
    summary.add_row("Session", escape(report.session_id or "unknown"))
    summary.add_row("Trading day", "yes" if report.is_trading_day else "no")
    summary.add_row("Schedule verdict", escape(report.schedule_verdict))
    summary.add_row("Run id", escape(report.run_id or "none recorded"))
    summary.add_row("Run status", escape(report.run_status or "none recorded"))
    summary.add_row("Snapshot", escape(report.snapshot_id or "not bound"))
    summary.add_row("Quote snapshot", escape(report.quote_snapshot_id or "not bound"))
    summary.add_row("Resumption", escape(report.resume_safety.value))
    summary.add_row("Lease", escape(_lease_label(report.lease)))
    if report.run_error_codes:
        summary.add_row("Run errors", escape(", ".join(report.run_error_codes)))
    console.print(summary)

    members = Table(title="Members")
    members.add_column("Sleeve")
    members.add_column("Status")
    members.add_column("Official observation")
    for member in report.members:
        members.add_row(
            escape(member.sleeve_id),
            escape(member.status),
            escape(
                f"{member.observation_key} ({member.observation_status})"
                if member.observation_key
                else "none"
            ),
        )
    console.print(members)
    console.print(f"Recommended action: {escape(report.recommended_action)}")


@storage_app.command("recover")
def recover(
    cohort: str = typer.Option(..., "--cohort", help="Cohort identifier to inspect."),
    scheduled_for: str = typer.Option(
        ..., "--scheduled-for", help="Scheduled session date, YYYY-MM-DD."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the JSON document."),
) -> None:
    """Inspect one official cohort session and report the one safe next action.

    Strictly non-mutating. It cannot delete a run, clear a lease, reset a member,
    replay anything, create an observation or fill, trigger the scheduler, or submit
    an order.
    """
    settings = get_settings()
    try:
        session_date = datetime.strptime(scheduled_for.strip(), "%Y-%m-%d").date()
    except ValueError:
        _abort("--scheduled-for must be an exact YYYY-MM-DD calendar date")
    database = factory.database(settings)
    if database is None:
        _abort(
            "no shared database is configured; recovery inspection reads the shared "
            "cohort record named by SCHWAB_DATABASE_URL"
        )
    try:
        report = recovery_ops.inspect_recovery(database, cohort, session_date)
    except ValueError as exc:
        _abort(str(exc))
    except Exception as exc:
        _abort(f"recovery inspection failed closed ({type(exc).__name__})")

    if json_output:
        console.print_json(data=report.sanitized_payload())
    else:
        _render_recovery(report)
    raise typer.Exit(code=report.exit_code)
