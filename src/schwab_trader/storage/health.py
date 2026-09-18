"""Read-only shared-storage health assessment with a stable sanitized contract.

Nothing in this module writes. It opens the configured database, asks it only
introspection questions, and returns one :class:`HealthReport`. The human table and
the ``--json`` document are two renderings of that single object, so they cannot
disagree.

Two rules shape every line here:

1. **Nothing sensitive is ever rendered.** The database URL, host, user, password,
   token, account identifier, and every raw financial row stay out of the report. The
   only text derived from an error is its exception *class name*: SQLAlchemy and
   psycopg both put the connection URL into some exception messages, so the message
   itself is never carried forward.
2. **Fail closed.** A capability that cannot be determined is ``unknown``, not
   assumed-good, and it downgrades the overall verdict to ``indeterminate``.

There is deliberately no repair mode. Every unhealthy verdict ends in exactly one
recommended operator action, and the operator runs it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url

from schwab_trader.config import Settings
from schwab_trader.storage import factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import Base

#: Bump only for a breaking change to the JSON document. Consumers pin this.
HEALTH_CONTRACT_VERSION = 1


class HealthStatus(StrEnum):
    """Overall verdict. Each value maps to exactly one process exit code."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DISCONNECTED = "disconnected"
    INDETERMINATE = "indeterminate"


#: Stable exit codes. ``storage health`` is meant to be usable from a scheduled task,
#: so these are part of the contract and must not be reordered.
EXIT_CODES: dict[HealthStatus, int] = {
    HealthStatus.HEALTHY: 0,
    HealthStatus.UNHEALTHY: 1,
    HealthStatus.DISCONNECTED: 2,
    HealthStatus.INDETERMINATE: 3,
}


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not-applicable"


class Backend(StrEnum):
    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"
    #: No ``SCHWAB_DATABASE_URL`` is configured, so the application is using the
    #: per-sleeve local SQLite layout. There is no single shared schema to assess.
    LOCAL_SQLITE_FILES = "local-sqlite-files"
    UNKNOWN = "unknown"


class TlsPolicy(StrEnum):
    """Sanitized transport posture. Never the host, user, or password."""

    NOT_APPLICABLE = "not-applicable"
    LOOPBACK_EXEMPT = "loopback-exempt"
    REQUIRE = "require"
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"
    UNKNOWN = "unknown"


class RoleCapability(StrEnum):
    READ_ONLY = "read-only"
    RUNTIME_WRITER = "runtime-writer"
    UNKNOWN = "unknown"


#: Tables whose write privileges decide whether this connection could run an official
#: cohort session. Chosen because they are exactly what one session must insert into.
RUNTIME_WRITE_TABLES: tuple[str, ...] = (
    "cohort_runs",
    "cohort_run_members",
    "official_daily_observations",
    "paper_fills",
)

#: Unique constraints that make duplicate runs, members, observations, and fills
#: impossible. A missing one is silent until two writers race, so it is checked
#: explicitly rather than trusted to the migration chain having been applied.
CRITICAL_CONSTRAINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cohort_runs", ("run_key",)),
    ("cohort_runs", ("cohort_id", "scheduled_for")),
    ("cohort_run_members", ("run_id", "ordinal")),
    ("official_daily_observations", ("observation_key",)),
    ("official_daily_observations", ("cohort_id", "sleeve_id", "session_date")),
    ("paper_fills", ("paper_order_id", "fill_sequence")),
)


def _failure_detail(prefix: str, exc: BaseException) -> str:
    """Describe a failure by exception *class* only.

    SQLAlchemy's ``OperationalError`` and several psycopg errors embed the connection
    URL — user, host, and sometimes password — in ``str(exc)``. Carrying the message
    into a report an operator may paste into an issue is how a credential leaks, so
    only the class name survives.
    """
    return f"{prefix} ({type(exc).__name__})"


class HealthCheck(BaseModel):
    """One named, sanitized finding. ``detail`` never contains untrusted text."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: CheckStatus
    detail: str


class LeaseSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    current: int = 0
    stale: int = 0
    #: ``cohort_id/scheduled_for`` for each stale lease. Cohort ids and session dates
    #: are operator-facing identifiers, not secrets; owner ids and token hashes are
    #: deliberately excluded.
    stale_sessions: tuple[str, ...] = ()


class RunSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    cohort_id: str
    scheduled_for: str
    status: str
    completed_members: int
    expected_members: int


class HealthReport(BaseModel):
    """The single source both renderings read from."""

    model_config = ConfigDict(frozen=True)

    contract_version: int = HEALTH_CONTRACT_VERSION
    generated_at: datetime
    status: HealthStatus
    exit_code: int
    backend: Backend
    tls_policy: TlsPolicy
    connected: bool
    server_version: str | None = None
    role: RoleCapability = RoleCapability.UNKNOWN
    alembic_current: tuple[str, ...] = ()
    alembic_expected_head: str | None = None
    missing_tables: tuple[str, ...] = ()
    unexpected_tables: tuple[str, ...] = ()
    missing_constraints: tuple[str, ...] = ()
    cohorts: int | None = None
    official_runs: int | None = None
    official_observations: int | None = None
    table_counts: dict[str, int] = Field(default_factory=dict)
    leases: LeaseSummary = LeaseSummary()
    latest_run: RunSummary | None = None
    checks: tuple[HealthCheck, ...] = ()
    next_action: str

    def sanitized_payload(self) -> dict[str, Any]:
        """The JSON document. Every field is already sanitized by construction."""
        return self.model_dump(mode="json")


class ServerFacts(BaseModel):
    """Raw, sanitized facts gathered from one connection.

    Split out from :func:`assess_health` so the backend-specific SQL has exactly one
    home, and so an offline test can supply PostgreSQL facts without a PostgreSQL
    server. Every field is ``None`` when the probe could not answer, which the
    assembly step turns into ``unknown`` rather than a guess.
    """

    model_config = ConfigDict(frozen=True)

    connected: bool = False
    server_version: str | None = None
    timezone: str | None = None
    server_now_is_aware: bool | None = None
    read_only_session: bool | None = None
    in_recovery: bool | None = None
    #: Runtime tables this role can INSERT into, as far as the backend will say.
    writable_tables: tuple[str, ...] = ()
    #: Runtime tables this role definitely cannot INSERT into.
    unwritable_tables: tuple[str, ...] = ()
    connect_error: str | None = None


def tls_policy(raw_url: str) -> TlsPolicy:
    """Classify transport security without echoing any part of the URL."""
    if not raw_url.strip():
        return TlsPolicy.NOT_APPLICABLE
    parsed = urlsplit(raw_url)
    if parsed.scheme.startswith("sqlite"):
        return TlsPolicy.NOT_APPLICABLE
    host = (parsed.hostname or "").casefold()
    sslmode = parse_qs(parsed.query).get("sslmode", [""])[-1].casefold()
    if sslmode == "verify-full":
        return TlsPolicy.VERIFY_FULL
    if sslmode == "verify-ca":
        return TlsPolicy.VERIFY_CA
    if sslmode == "require":
        return TlsPolicy.REQUIRE
    if host in {"localhost", "127.0.0.1", "::1"}:
        # ``Settings`` permits a loopback URL without ``sslmode``; say so plainly
        # rather than reporting it as unencrypted-and-fine or as an error.
        return TlsPolicy.LOOPBACK_EXEMPT
    return TlsPolicy.UNKNOWN


def backend_of(raw_url: str) -> Backend:
    if not raw_url.strip():
        return Backend.LOCAL_SQLITE_FILES
    scheme = urlsplit(raw_url).scheme
    if scheme.startswith("sqlite"):
        return Backend.SQLITE
    if scheme.startswith("postgresql"):
        return Backend.POSTGRESQL
    return Backend.UNKNOWN


def sqlite_path(raw_url: str) -> Path | None:
    """The on-disk file a ``sqlite://`` URL names, or ``None`` for in-memory."""
    try:
        database = make_url(raw_url).database
    except Exception:
        return None
    if not database or database == ":memory:":
        return None
    return Path(database)


def gather_server_facts(database: Database) -> ServerFacts:
    """Ask the live connection only introspection questions.

    Read-only by construction: every statement is a ``SELECT``, a ``SHOW``, or a
    ``PRAGMA`` read. No transaction here inserts, updates, or deletes anything.
    """
    try:
        with database.engine.connect() as connection:
            if database.dialect == "postgresql":
                return _postgres_facts(connection)
            return _sqlite_facts(connection, database)
    except Exception as exc:
        return ServerFacts(
            connected=False,
            connect_error=_failure_detail("could not open a connection", exc),
        )


def _postgres_facts(connection: Any) -> ServerFacts:
    version = connection.execute(text("SHOW server_version")).scalar_one()
    timezone = connection.execute(text("SHOW TimeZone")).scalar_one()
    server_now = connection.execute(text("SELECT now()")).scalar_one()
    read_only = connection.execute(
        text("SELECT current_setting('transaction_read_only')")
    ).scalar_one()
    in_recovery = bool(connection.execute(text("SELECT pg_is_in_recovery()")).scalar_one())

    writable: list[str] = []
    unwritable: list[str] = []
    for table in RUNTIME_WRITE_TABLES:
        try:
            allowed = connection.execute(
                text("SELECT has_table_privilege(:relation, 'INSERT')"),
                {"relation": table},
            ).scalar_one()
        except Exception:
            # The table is absent, or the role may not inspect it. Either way this is
            # not evidence of write capability, and the missing-table check reports
            # the absence separately.
            continue
        (writable if allowed else unwritable).append(table)

    return ServerFacts(
        connected=True,
        server_version=f"PostgreSQL {version}",
        timezone=str(timezone),
        server_now_is_aware=getattr(server_now, "tzinfo", None) is not None,
        read_only_session=str(read_only).casefold() == "on",
        in_recovery=in_recovery,
        writable_tables=tuple(writable),
        unwritable_tables=tuple(unwritable),
    )


def _sqlite_facts(connection: Any, database: Database) -> ServerFacts:
    version = connection.exec_driver_sql("SELECT sqlite_version()").scalar_one()
    query_only = bool(connection.exec_driver_sql("PRAGMA query_only").scalar_one())

    # SQLite has no roles. Write capability is a filesystem fact, so it is derived
    # from the file itself and reported as such rather than inferred from a probe
    # write, which this command must never perform.
    raw_path = database.engine.url.database
    path = Path(raw_path) if raw_path and raw_path != ":memory:" else None
    if query_only:
        writable: tuple[str, ...] = ()
        unwritable: tuple[str, ...] = RUNTIME_WRITE_TABLES
    elif path is None:
        writable, unwritable = (), ()
    elif os.access(path, os.W_OK):
        writable, unwritable = RUNTIME_WRITE_TABLES, ()
    else:
        writable, unwritable = (), RUNTIME_WRITE_TABLES

    return ServerFacts(
        connected=True,
        server_version=f"SQLite {version}",
        timezone=None,
        server_now_is_aware=None,
        read_only_session=query_only,
        in_recovery=False,
        writable_tables=writable,
        unwritable_tables=unwritable,
    )


def alembic_revisions(
    database: Database, repo_root: Path
) -> tuple[tuple[str, ...] | None, str | None, str | None]:
    """``(current heads, expected head, error)`` — any element may be ``None``."""
    config_path = repo_root / "alembic.ini"
    script_path = repo_root / "alembic"
    if not config_path.is_file() or not script_path.is_dir():
        return None, None, "the migration chain is not available in this checkout"

    try:
        from alembic.config import Config
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory

        config = Config(str(config_path))
        config.set_main_option("script_location", str(script_path))
        expected = ScriptDirectory.from_config(config).get_current_head()
    except Exception as exc:
        return None, None, _failure_detail("could not read the migration chain", exc)

    try:
        with database.engine.connect() as connection:
            current = tuple(MigrationContext.configure(connection).get_current_heads())
    except Exception as exc:
        return None, expected, _failure_detail("could not read the applied revision", exc)
    return current, expected, None


def present_tables(database: Database) -> frozenset[str]:
    return frozenset(inspect(database.engine).get_table_names())


def _missing_constraints(database: Database, present: frozenset[str]) -> tuple[str, ...]:
    """Which duplicate-prevention constraints the live schema does not enforce."""
    inspector = inspect(database.engine)
    missing: list[str] = []
    for table, columns in CRITICAL_CONSTRAINTS:
        label = f"{table}({', '.join(columns)})"
        if table not in present:
            missing.append(label)
            continue
        try:
            enforced = {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table)
            }
            enforced.add(tuple(inspector.get_pk_constraint(table).get("constrained_columns") or ()))
            enforced |= {
                # Expression indexes report ``None`` for the computed column; drop
                # those so the tuple compares cleanly against a plain column list.
                tuple(name for name in index["column_names"] if name is not None)
                for index in inspector.get_indexes(table)
                if index.get("unique")
            }
        except Exception:
            # Fail closed: an unreadable constraint set is reported as missing rather
            # than assumed present.
            missing.append(label)
            continue
        if columns not in enforced:
            missing.append(label)
    return tuple(missing)


def table_counts(database: Database, present: frozenset[str]) -> dict[str, int]:
    """Row counts for the application-owned tables that exist. Counts only."""
    counts: dict[str, int] = {}
    with database.session() as session:
        for name, table in sorted(Base.metadata.tables.items()):
            if name not in present:
                continue
            try:
                counts[name] = int(
                    session.scalar(select(func.count()).select_from(table)) or 0
                )
            except Exception:
                # One unreadable table must not blank the whole report.
                continue
    return counts


def _lease_summary(database: Database, present: frozenset[str], now: datetime) -> LeaseSummary:
    if "official_session_leases" not in present:
        return LeaseSummary()
    from schwab_trader.storage.schema import OfficialSessionLease

    current = 0
    stale: list[str] = []
    with database.session() as session:
        rows = session.execute(
            select(
                OfficialSessionLease.cohort_id,
                OfficialSessionLease.scheduled_for,
                OfficialSessionLease.expires_at,
            ).where(OfficialSessionLease.released_at.is_(None))
        ).all()
    for cohort_id, scheduled_for, expires_at in rows:
        expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if expiry <= now:
            stale.append(f"{cohort_id}/{scheduled_for.isoformat()}")
        else:
            current += 1
    return LeaseSummary(current=current, stale=len(stale), stale_sessions=tuple(sorted(stale)))


def _latest_run(database: Database, present: frozenset[str]) -> RunSummary | None:
    if "cohort_runs" not in present:
        return None
    from schwab_trader.storage.schema import CohortRun

    with database.session() as session:
        row = session.execute(
            select(
                CohortRun.cohort_id,
                CohortRun.scheduled_for,
                CohortRun.status,
                CohortRun.completed_members,
                CohortRun.expected_members,
            )
            .order_by(CohortRun.scheduled_for.desc(), CohortRun.started_at.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    cohort_id, scheduled_for, status, completed, expected = row
    return RunSummary(
        cohort_id=cohort_id,
        scheduled_for=scheduled_for.isoformat(),
        status=status,
        completed_members=len(completed or []),
        expected_members=len(expected or []),
    )


#: Run statuses that mean the latest official session did not produce a complete
#: result. ``awaiting-data`` is excluded: it is a legitimate, retryable wait.
_INCOMPLETE_RUN_STATUSES = frozenset({"partial", "failed", "missed", "running", "pending"})


def _overall(checks: Sequence[HealthCheck], *, connected: bool) -> HealthStatus:
    if not connected:
        return HealthStatus.DISCONNECTED
    statuses = {check.status for check in checks}
    if CheckStatus.FAIL in statuses or CheckStatus.WARN in statuses:
        return HealthStatus.UNHEALTHY
    if CheckStatus.UNKNOWN in statuses:
        return HealthStatus.INDETERMINATE
    return HealthStatus.HEALTHY


def _next_action(report_checks: Sequence[HealthCheck], backend: Backend) -> str:
    """Exactly one deterministic action, chosen by a fixed priority.

    Priority order is intentional: a connection problem hides every other finding, a
    schema gap makes counts meaningless, and an operational finding is only worth
    acting on once the schema is trusted.
    """
    by_name = {check.name: check for check in report_checks}

    def bad(name: str) -> bool:
        check = by_name.get(name)
        return check is not None and check.status in {CheckStatus.FAIL, CheckStatus.WARN}

    def unknown(name: str) -> bool:
        check = by_name.get(name)
        return check is not None and check.status is CheckStatus.UNKNOWN

    if backend is Backend.LOCAL_SQLITE_FILES:
        return (
            "No shared database is configured. Set SCHWAB_DATABASE_URL in the local "
            ".env, or run this command on the machine that holds shared storage."
        )
    if bad("connectivity"):
        return (
            "Confirm the database service is reachable and the local SCHWAB_DATABASE_URL "
            "is still valid, then re-run `storage health`."
        )
    if unknown("schema_revision"):
        return (
            "The applied schema revision could not be read. Do not run migrations or the "
            "official scheduler; re-run `storage health` from the reviewed checkout."
        )
    if bad("schema_revision") or bad("required_tables"):
        return (
            "Run `alembic upgrade head` with the migration-owner role, confirm a "
            "`Running upgrade` line per applied revision, then re-run `storage health`."
        )
    if bad("critical_constraints"):
        return (
            "A duplicate-prevention constraint is missing. Stop the official scheduler "
            "and follow docs/operations/storage-backup-restore.md before writing again."
        )
    if bad("role_capability") or unknown("role_capability"):
        return (
            "The connected role does not match the expected capability. Re-check which "
            "role this machine is configured with before starting any writer."
        )
    if bad("official_session_leases"):
        return (
            "Inspect the stale official-session lease with `storage recover --cohort "
            "<id> --scheduled-for <YYYY-MM-DD>` before starting the scheduler."
        )
    if bad("latest_official_run"):
        return (
            "Inspect the incomplete official run with `storage recover --cohort <id> "
            "--scheduled-for <YYYY-MM-DD>`; it reports the one safe next action."
        )
    if bad("unexpected_tables") or bad("timezone"):
        return (
            "Investigate the reported drift against docs/migrations/postgresql-runbook.md "
            "before the next official session."
        )
    return "No action required. Re-run `storage health` before the next official session."


def assess_health(
    settings: Settings,
    *,
    now: datetime | None = None,
    require_writer: bool = False,
    repo_root: Path | None = None,
) -> HealthReport:
    """Assess the configured shared storage. Opens nothing it does not read."""
    stamp = now or datetime.now(UTC)
    root = repo_root or Path(__file__).resolve().parents[3]
    raw_url = settings.database_url.get_secret_value().strip()
    backend = backend_of(raw_url)
    tls = tls_policy(raw_url)
    checks: list[HealthCheck] = []

    if backend is Backend.LOCAL_SQLITE_FILES:
        # Honest and fail-closed: this command assesses *shared* storage, and there is
        # none. Reporting "healthy" would tell an operator their shared database is
        # fine when they never configured one.
        checks.append(
            HealthCheck(
                name="backend_selection",
                status=CheckStatus.UNKNOWN,
                detail=(
                    "SCHWAB_DATABASE_URL is not configured; the application is using the "
                    "per-sleeve local SQLite layout and there is no shared schema to check."
                ),
            )
        )
        return HealthReport(
            generated_at=stamp,
            status=HealthStatus.INDETERMINATE,
            exit_code=EXIT_CODES[HealthStatus.INDETERMINATE],
            backend=backend,
            tls_policy=tls,
            connected=False,
            checks=tuple(checks),
            next_action=_next_action(checks, backend),
        )

    checks.append(
        HealthCheck(
            name="backend_selection",
            status=CheckStatus.OK if backend is not Backend.UNKNOWN else CheckStatus.FAIL,
            detail=f"selected backend: {backend.value}; TLS policy: {tls.value}",
        )
    )

    # A missing SQLite file must be reported, never created. Opening the engine would
    # create an empty database and then report it as schema-drifted rather than absent.
    sqlite_file = sqlite_path(raw_url) if backend is Backend.SQLITE else None
    if sqlite_file is not None and not sqlite_file.is_file():
        checks.append(
            HealthCheck(
                name="connectivity",
                status=CheckStatus.FAIL,
                detail="the configured SQLite database file does not exist",
            )
        )
        return HealthReport(
            generated_at=stamp,
            status=HealthStatus.DISCONNECTED,
            exit_code=EXIT_CODES[HealthStatus.DISCONNECTED],
            backend=backend,
            tls_policy=tls,
            connected=False,
            checks=tuple(checks),
            next_action=_next_action(checks, backend),
        )

    try:
        database = factory.database(settings)
    except Exception as exc:
        checks.append(
            HealthCheck(
                name="connectivity",
                status=CheckStatus.FAIL,
                detail=_failure_detail("the database could not be opened", exc),
            )
        )
        return HealthReport(
            generated_at=stamp,
            status=HealthStatus.DISCONNECTED,
            exit_code=EXIT_CODES[HealthStatus.DISCONNECTED],
            backend=backend,
            tls_policy=tls,
            connected=False,
            checks=tuple(checks),
            next_action=_next_action(checks, backend),
        )
    assert database is not None

    facts = gather_server_facts(database)
    if not facts.connected:
        checks.append(
            HealthCheck(
                name="connectivity",
                status=CheckStatus.FAIL,
                detail=facts.connect_error or "the database did not accept a connection",
            )
        )
        return HealthReport(
            generated_at=stamp,
            status=HealthStatus.DISCONNECTED,
            exit_code=EXIT_CODES[HealthStatus.DISCONNECTED],
            backend=backend,
            tls_policy=tls,
            connected=False,
            checks=tuple(checks),
            next_action=_next_action(checks, backend),
        )

    checks.append(
        HealthCheck(
            name="connectivity",
            status=CheckStatus.OK,
            detail=f"connected; server: {facts.server_version or 'unreported'}",
        )
    )
    checks.append(_timezone_check(backend, facts))

    current, expected, revision_error = alembic_revisions(database, root)
    checks.append(_revision_check(current, expected, revision_error))

    try:
        present = present_tables(database)
    except Exception as exc:
        present = frozenset()
        checks.append(
            HealthCheck(
                name="required_tables",
                status=CheckStatus.UNKNOWN,
                detail=_failure_detail("the table list could not be read", exc),
            )
        )
        missing_tables: tuple[str, ...] = ()
        unexpected: tuple[str, ...] = ()
        missing_constraints: tuple[str, ...] = ()
    else:
        expected_tables = frozenset(Base.metadata.tables)
        missing_tables = tuple(sorted(expected_tables - present))
        unexpected = tuple(sorted(present - expected_tables - {"alembic_version"}))
        checks.append(
            HealthCheck(
                name="required_tables",
                status=CheckStatus.OK if not missing_tables else CheckStatus.FAIL,
                detail=(
                    f"{len(expected_tables) - len(missing_tables)} of "
                    f"{len(expected_tables)} application tables present"
                    + (f"; missing: {', '.join(missing_tables)}" if missing_tables else "")
                ),
            )
        )
        checks.append(
            HealthCheck(
                name="unexpected_tables",
                status=CheckStatus.OK if not unexpected else CheckStatus.WARN,
                detail=(
                    "no unexpected tables"
                    if not unexpected
                    else f"{len(unexpected)} unexpected: {', '.join(unexpected)}"
                ),
            )
        )
        missing_constraints = _missing_constraints(database, present)
        checks.append(
            HealthCheck(
                name="critical_constraints",
                status=CheckStatus.OK if not missing_constraints else CheckStatus.FAIL,
                detail=(
                    f"{len(CRITICAL_CONSTRAINTS)} duplicate-prevention constraints enforced"
                    if not missing_constraints
                    else f"not enforced: {', '.join(missing_constraints)}"
                ),
            )
        )

    checks.append(_role_check(facts, require_writer=require_writer))

    counts = table_counts(database, present) if present else {}
    leases = _lease_summary(database, present, stamp)
    latest = _latest_run(database, present)

    checks.append(
        HealthCheck(
            name="official_session_leases",
            status=CheckStatus.OK if not leases.stale else CheckStatus.WARN,
            detail=(
                f"{leases.current} current, {leases.stale} stale"
                + (f"; stale: {', '.join(leases.stale_sessions)}" if leases.stale else "")
            ),
        )
    )
    checks.append(_latest_run_check(latest))

    status = _overall(checks, connected=True)
    return HealthReport(
        generated_at=stamp,
        status=status,
        exit_code=EXIT_CODES[status],
        backend=backend,
        tls_policy=tls,
        connected=True,
        server_version=facts.server_version,
        role=_role_capability(facts),
        alembic_current=current or (),
        alembic_expected_head=expected,
        missing_tables=missing_tables,
        unexpected_tables=unexpected,
        missing_constraints=missing_constraints,
        cohorts=counts.get("cohorts"),
        official_runs=counts.get("cohort_runs"),
        official_observations=counts.get("official_daily_observations"),
        table_counts=counts,
        leases=leases,
        latest_run=latest,
        checks=tuple(checks),
        next_action=_next_action(checks, backend),
    )


def _timezone_check(backend: Backend, facts: ServerFacts) -> HealthCheck:
    if backend is not Backend.POSTGRESQL:
        return HealthCheck(
            name="timezone",
            status=CheckStatus.NOT_APPLICABLE,
            detail=(
                "SQLite has no server timezone; the application's AwareTimestamp type "
                "stores every timestamp as UTC ISO-8601 and rejects naive values."
            ),
        )
    if facts.timezone is None:
        return HealthCheck(
            name="timezone",
            status=CheckStatus.UNKNOWN,
            detail="the server timezone could not be read",
        )
    if facts.timezone.casefold() != "utc":
        return HealthCheck(
            name="timezone",
            status=CheckStatus.WARN,
            detail=f"server TimeZone is {facts.timezone}, not UTC",
        )
    if facts.server_now_is_aware is False:
        return HealthCheck(
            name="timezone",
            status=CheckStatus.WARN,
            detail="server TimeZone is UTC but now() returned a naive timestamp",
        )
    return HealthCheck(
        name="timezone",
        status=CheckStatus.OK,
        detail="server TimeZone is UTC and now() is timezone-aware",
    )


def _revision_check(
    current: tuple[str, ...] | None, expected: str | None, error: str | None
) -> HealthCheck:
    if error is not None or current is None or expected is None:
        return HealthCheck(
            name="schema_revision",
            status=CheckStatus.UNKNOWN,
            detail=error or "the schema revision could not be compared",
        )
    if not current:
        return HealthCheck(
            name="schema_revision",
            status=CheckStatus.FAIL,
            detail=f"no revision is recorded; expected head {expected}",
        )
    if tuple(current) != (expected,):
        return HealthCheck(
            name="schema_revision",
            status=CheckStatus.FAIL,
            detail=f"applied {', '.join(current)}; expected head {expected}",
        )
    return HealthCheck(
        name="schema_revision",
        status=CheckStatus.OK,
        detail=f"at expected head {expected}",
    )


def _role_capability(facts: ServerFacts) -> RoleCapability:
    """Classify the connection, refusing to guess on mixed evidence."""
    if facts.read_only_session or facts.in_recovery:
        return RoleCapability.READ_ONLY
    if facts.writable_tables and not facts.unwritable_tables:
        return RoleCapability.RUNTIME_WRITER
    if facts.unwritable_tables and not facts.writable_tables:
        return RoleCapability.READ_ONLY
    # Either nothing was determinable, or the role can write some runtime tables and
    # not others — a partial grant that would fail mid-session. Neither is a verdict.
    return RoleCapability.UNKNOWN


def _role_check(facts: ServerFacts, *, require_writer: bool) -> HealthCheck:
    capability = _role_capability(facts)
    detail = f"connection appears {capability.value}"
    if facts.in_recovery:
        detail += "; the server is a read replica"
    if capability is RoleCapability.UNKNOWN:
        if facts.writable_tables and facts.unwritable_tables:
            detail = (
                "partial write grant: cannot write "
                f"{', '.join(sorted(facts.unwritable_tables))}"
            )
        return HealthCheck(name="role_capability", status=CheckStatus.UNKNOWN, detail=detail)
    if require_writer and capability is not RoleCapability.RUNTIME_WRITER:
        return HealthCheck(
            name="role_capability",
            status=CheckStatus.FAIL,
            detail=detail + "; --require-writer was requested",
        )
    return HealthCheck(name="role_capability", status=CheckStatus.OK, detail=detail)


def _latest_run_check(latest: RunSummary | None) -> HealthCheck:
    if latest is None:
        return HealthCheck(
            name="latest_official_run",
            status=CheckStatus.OK,
            detail="no official cohort run is recorded",
        )
    label = (
        f"{latest.cohort_id}/{latest.scheduled_for} is {latest.status} "
        f"({latest.completed_members}/{latest.expected_members} members complete)"
    )
    if latest.status in _INCOMPLETE_RUN_STATUSES:
        return HealthCheck(name="latest_official_run", status=CheckStatus.WARN, detail=label)
    return HealthCheck(name="latest_official_run", status=CheckStatus.OK, detail=label)


__all__ = [
    "CRITICAL_CONSTRAINTS",
    "EXIT_CODES",
    "HEALTH_CONTRACT_VERSION",
    "RUNTIME_WRITE_TABLES",
    "Backend",
    "CheckStatus",
    "HealthCheck",
    "HealthReport",
    "HealthStatus",
    "LeaseSummary",
    "RoleCapability",
    "RunSummary",
    "ServerFacts",
    "TlsPolicy",
    "alembic_revisions",
    "assess_health",
    "backend_of",
    "gather_server_facts",
    "present_tables",
    "sqlite_path",
    "table_counts",
    "tls_policy",
]
