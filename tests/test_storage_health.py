"""Offline tests for the read-only shared-storage health assessment.

Every database here is a throwaway SQLite file under ``tmp_path``. No ``.env`` is
read, no shared or remote database is reachable, and nothing in this module can
contact Neon, Schwab, SMTP, or any broker endpoint — see the autouse isolation
fixture in ``conftest.py``.

The PostgreSQL-shaped cases work by substituting :func:`gather_server_facts`, which
is the single seam holding backend-specific SQL. That keeps the assertions about
*report assembly* honest without a PostgreSQL server, and the genuinely
server-specific SQL stays covered by the opt-in contract suite.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from typer.testing import CliRunner

from schwab_trader.cli import app
from schwab_trader.config import Settings
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage import health
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import (
    Base,
    Cohort,
    CohortRun,
    OfficialSessionLease,
    StorageNamespace,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 30, 15, 0, tzinfo=UTC)

#: Sentinels that must never reach any output, log, error, or JSON document. They are
#: deliberately distinctive so a substring search cannot produce a false negative.
SENTINEL_PASSWORD = "s3ntinel-pa55word-must-not-appear"
SENTINEL_HOST = "sentinel-host.invalid"
SENTINEL_USER = "sentinel-user"
SENTINEL_DATABASE = "sentinel-database"
SENTINEL_URL = (
    f"postgresql+psycopg://{SENTINEL_USER}:{SENTINEL_PASSWORD}"
    f"@{SENTINEL_HOST}/{SENTINEL_DATABASE}?sslmode=verify-full"
)


def _alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _expected_head() -> str:
    head = ScriptDirectory.from_config(_alembic_config("sqlite://")).get_current_head()
    assert head is not None
    return head


def _migrated_sqlite(tmp_path: Path, *, revision: str = "head") -> tuple[Settings, str]:
    """A throwaway SQLite database built by the real Alembic chain."""
    url = f"sqlite:///{(tmp_path / 'shared.sqlite3').as_posix()}"
    command.upgrade(_alembic_config(url), revision)
    return Settings(_env_file=None, database_url=url), url  # type: ignore[call-arg]


def _settings_for(url: str) -> Settings:
    return Settings(_env_file=None, database_url=url)  # type: ignore[call-arg]


#: The genuine implementation, captured before any test can patch the module.
_REAL_ASSESS = health.assess_health


def _assess(settings: Settings, **kwargs: object) -> health.HealthReport:
    return _REAL_ASSESS(settings, repo_root=REPO_ROOT, now=NOW, **kwargs)  # type: ignore[arg-type]


def _seed_cohort(database: Database, cohort_id: str) -> None:
    with database.session() as session:
        session.add(
            StorageNamespace(
                namespace_id="ns-1",
                name="test-namespace",
                kind="cohort",
                source_identity=None,
                created_at=NOW,
                immutable_metadata={},
            )
        )
        session.flush()
        session.add(
            Cohort(
                cohort_id=cohort_id,
                namespace_id="ns-1",
                name=cohort_id,
                created_at=NOW,
                start_session=date(2026, 7, 27),
                status="active",
                manifest_json={},
                manifest_hash="0" * 64,
            )
        )


def _pg_facts(**overrides: object) -> health.ServerFacts:
    """A healthy PostgreSQL probe result, overridable per test."""
    defaults: dict[str, object] = {
        "connected": True,
        "server_version": "PostgreSQL 16.3",
        "timezone": "UTC",
        "server_now_is_aware": True,
        "read_only_session": False,
        "in_recovery": False,
        "writable_tables": health.RUNTIME_WRITE_TABLES,
        "unwritable_tables": (),
    }
    defaults.update(overrides)
    return health.ServerFacts(**defaults)  # type: ignore[arg-type]


def _as_postgres(
    monkeypatch: pytest.MonkeyPatch, database: Database, facts: health.ServerFacts
) -> Settings:
    """Present a real SQLite schema as if it were reached over a PostgreSQL URL.

    The URL is never dialled: ``factory.database`` is replaced with the already-open
    SQLite handle, so this stays entirely offline while still exercising every
    PostgreSQL branch of the report assembly.
    """
    monkeypatch.setattr(storage_factory, "database", lambda _settings: database)
    monkeypatch.setattr(health, "gather_server_facts", lambda _database: facts)
    return _settings_for(SENTINEL_URL)


# --------------------------------------------------------------------------------
# Healthy results
# --------------------------------------------------------------------------------


def test_healthy_sqlite_reports_zero_exit_and_no_findings(tmp_path):
    settings, _url = _migrated_sqlite(tmp_path)
    report = _assess(settings)

    assert report.status is health.HealthStatus.HEALTHY
    assert report.exit_code == 0
    assert report.backend is health.Backend.SQLITE
    assert report.tls_policy is health.TlsPolicy.NOT_APPLICABLE
    assert report.connected is True
    assert report.missing_tables == ()
    assert report.missing_constraints == ()
    assert report.alembic_current == (report.alembic_expected_head,)
    assert report.role is health.RoleCapability.RUNTIME_WRITER
    assert report.cohorts == 0
    assert {check.status for check in report.checks} <= {
        health.CheckStatus.OK,
        health.CheckStatus.NOT_APPLICABLE,
    }
    assert report.next_action.startswith("No action required")


def test_healthy_postgresql_style_reports_tls_version_and_utc(tmp_path, monkeypatch):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    settings = _as_postgres(monkeypatch, database, _pg_facts())

    report = _assess(settings)

    assert report.status is health.HealthStatus.HEALTHY
    assert report.exit_code == 0
    assert report.backend is health.Backend.POSTGRESQL
    assert report.tls_policy is health.TlsPolicy.VERIFY_FULL
    assert report.server_version == "PostgreSQL 16.3"
    assert report.role is health.RoleCapability.RUNTIME_WRITER
    timezone = next(check for check in report.checks if check.name == "timezone")
    assert timezone.status is health.CheckStatus.OK
    assert "UTC" in timezone.detail


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", health.TlsPolicy.NOT_APPLICABLE),
        ("sqlite:///./data/shared.sqlite3", health.TlsPolicy.NOT_APPLICABLE),
        ("postgresql+psycopg://u:p@localhost/app", health.TlsPolicy.LOOPBACK_EXEMPT),
        ("postgresql+psycopg://u:p@h.example/app?sslmode=require", health.TlsPolicy.REQUIRE),
        ("postgresql+psycopg://u:p@h.example/app?sslmode=verify-ca", health.TlsPolicy.VERIFY_CA),
        (SENTINEL_URL, health.TlsPolicy.VERIFY_FULL),
    ],
)
def test_tls_policy_classifies_without_echoing_the_url(raw, expected):
    policy = health.tls_policy(raw)
    assert policy is expected
    assert SENTINEL_PASSWORD not in policy.value
    assert SENTINEL_HOST not in policy.value


# --------------------------------------------------------------------------------
# Disconnected
# --------------------------------------------------------------------------------


def test_absent_sqlite_file_is_disconnected_and_is_never_created(tmp_path):
    missing = tmp_path / "does-not-exist.sqlite3"
    settings = _settings_for(f"sqlite:///{missing.as_posix()}")

    report = _assess(settings)

    assert report.status is health.HealthStatus.DISCONNECTED
    assert report.exit_code == 2
    # Opening the engine would have created an empty file and then reported it as
    # schema-drifted, which is a materially different and misleading finding.
    assert not missing.exists()
    assert "reachable" in report.next_action


def test_refused_connection_is_disconnected_and_reports_only_the_exception_class(
    tmp_path, monkeypatch
):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    facts = health.ServerFacts(
        connected=False,
        connect_error="could not open a connection (OperationalError)",
    )
    settings = _as_postgres(monkeypatch, database, facts)

    report = _assess(settings)

    assert report.status is health.HealthStatus.DISCONNECTED
    assert report.exit_code == 2
    connectivity = next(check for check in report.checks if check.name == "connectivity")
    assert connectivity.detail == "could not open a connection (OperationalError)"
    assert SENTINEL_HOST not in json.dumps(report.sanitized_payload())


def test_failure_detail_never_carries_an_exception_message():
    """SQLAlchemy embeds the connection URL in some messages; only the class survives."""
    exc = RuntimeError(f"connection to {SENTINEL_HOST} with password {SENTINEL_PASSWORD}")
    detail = health._failure_detail("the database could not be opened", exc)

    assert detail == "the database could not be opened (RuntimeError)"
    assert SENTINEL_PASSWORD not in detail
    assert SENTINEL_HOST not in detail


# --------------------------------------------------------------------------------
# Schema drift
# --------------------------------------------------------------------------------


def test_revision_behind_head_is_unhealthy_with_an_upgrade_action(tmp_path):
    settings, _url = _migrated_sqlite(tmp_path, revision="20260730_0004")
    report = _assess(settings)

    assert report.status is health.HealthStatus.UNHEALTHY
    assert report.exit_code == 1
    revision = next(check for check in report.checks if check.name == "schema_revision")
    assert revision.status is health.CheckStatus.FAIL
    assert report.alembic_current != (report.alembic_expected_head,)
    assert "alembic upgrade head" in report.next_action


def test_no_recorded_revision_is_a_finding_even_when_the_models_built_the_schema(tmp_path):
    """The #68 failure mode: a database built from the models reports no revision.

    ``Database(create_schema=True)`` produces every table, so a naive table-presence
    check passes. Only the revision comparison notices nothing was ever migrated.
    """
    path = tmp_path / "modelled.sqlite3"
    Database(f"sqlite:///{path.as_posix()}", create_schema=True).dispose()
    settings = _settings_for(f"sqlite:///{path.as_posix()}")

    report = _assess(settings)

    assert report.missing_tables == ()
    assert report.status is health.HealthStatus.UNHEALTHY
    revision = next(check for check in report.checks if check.name == "schema_revision")
    assert revision.status is health.CheckStatus.FAIL
    assert "no revision is recorded" in revision.detail


def test_missing_table_is_reported_by_name(tmp_path):
    path = tmp_path / "partial.sqlite3"
    database = Database(f"sqlite:///{path.as_posix()}")
    # Build every table except one, rather than dropping anything.
    keep = [table for name, table in Base.metadata.tables.items() if name != "cohort_alerts"]
    Base.metadata.create_all(database.engine, tables=keep)
    settings = _settings_for(f"sqlite:///{path.as_posix()}")

    report = _assess(settings)

    assert report.missing_tables == ("cohort_alerts",)
    assert report.status is health.HealthStatus.UNHEALTHY
    assert report.exit_code == 1
    tables = next(check for check in report.checks if check.name == "required_tables")
    assert tables.status is health.CheckStatus.FAIL
    assert "cohort_alerts" in tables.detail


def test_missing_duplicate_prevention_constraint_is_reported(tmp_path):
    """A database that reports the right revision can still have lost a constraint.

    This is why the constraint check exists separately from the revision check: an
    operator who rebuilt one table by hand leaves the revision untouched, and only a
    direct look at the enforced uniqueness notices that duplicate fills are now
    possible.
    """
    path = tmp_path / "unconstrained.sqlite3"
    database = Database(f"sqlite:///{path.as_posix()}")
    keep = [table for name, table in Base.metadata.tables.items() if name != "paper_fills"]
    Base.metadata.create_all(database.engine, tables=keep)
    # Recreate paper_fills without its (paper_order_id, fill_sequence) uniqueness.
    # This is the drift that lets a replay write a duplicate fill.
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE paper_fills ("
                "paper_fill_id INTEGER PRIMARY KEY, "
                "paper_order_id INTEGER NOT NULL, "
                "fill_sequence INTEGER NOT NULL, "
                "quantity BIGINT NOT NULL, "
                "price NUMERIC NOT NULL)"
            )
        )
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
            {"head": _expected_head()},
        )
    settings = _settings_for(f"sqlite:///{path.as_posix()}")

    report = _assess(settings)

    assert "paper_fills(paper_order_id, fill_sequence)" in report.missing_constraints
    assert report.status is health.HealthStatus.UNHEALTHY
    constraints = next(
        check for check in report.checks if check.name == "critical_constraints"
    )
    assert constraints.status is health.CheckStatus.FAIL
    assert "duplicate-prevention constraint is missing" in report.next_action


def test_unexpected_table_is_reported_without_failing_the_schema(tmp_path):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE leftover_scratch (id INTEGER PRIMARY KEY)"))

    report = _assess(settings)

    assert report.unexpected_tables == ("leftover_scratch",)
    assert report.missing_tables == ()
    unexpected = next(check for check in report.checks if check.name == "unexpected_tables")
    assert unexpected.status is health.CheckStatus.WARN


# --------------------------------------------------------------------------------
# Role capability
# --------------------------------------------------------------------------------


def test_read_only_role_passes_by_default_and_fails_under_require_writer(
    tmp_path, monkeypatch
):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    facts = _pg_facts(
        read_only_session=True, writable_tables=(), unwritable_tables=health.RUNTIME_WRITE_TABLES
    )
    settings = _as_postgres(monkeypatch, database, facts)

    # A read-only role is the correct configuration for a dashboard machine.
    permissive = _assess(settings)
    assert permissive.role is health.RoleCapability.READ_ONLY
    assert permissive.status is health.HealthStatus.HEALTHY

    strict = _assess(settings, require_writer=True)
    assert strict.status is health.HealthStatus.UNHEALTHY
    assert strict.exit_code == 1
    role = next(check for check in strict.checks if check.name == "role_capability")
    assert role.status is health.CheckStatus.FAIL
    assert "expected capability" in strict.next_action


def test_replica_connection_is_classified_read_only(tmp_path, monkeypatch):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    settings = _as_postgres(monkeypatch, database, _pg_facts(in_recovery=True))

    report = _assess(settings, require_writer=True)

    assert report.role is health.RoleCapability.READ_ONLY
    role = next(check for check in report.checks if check.name == "role_capability")
    assert "read replica" in role.detail


def test_partial_write_grant_fails_closed_as_indeterminate(tmp_path, monkeypatch):
    """A role that can write some runtime tables would fail mid-session.

    Reporting it as ``runtime-writer`` would be worse than useless, so a mixed grant
    is ``unknown`` and the whole verdict becomes indeterminate.
    """
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    facts = _pg_facts(
        writable_tables=("cohort_runs", "cohort_run_members"),
        unwritable_tables=("official_daily_observations", "paper_fills"),
    )
    settings = _as_postgres(monkeypatch, database, facts)

    report = _assess(settings)

    assert report.role is health.RoleCapability.UNKNOWN
    assert report.status is health.HealthStatus.INDETERMINATE
    assert report.exit_code == 3
    role = next(check for check in report.checks if check.name == "role_capability")
    assert role.status is health.CheckStatus.UNKNOWN
    assert "official_daily_observations" in role.detail


def test_non_utc_server_timezone_is_a_finding(tmp_path, monkeypatch):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    settings = _as_postgres(monkeypatch, database, _pg_facts(timezone="America/New_York"))

    report = _assess(settings)

    timezone = next(check for check in report.checks if check.name == "timezone")
    assert timezone.status is health.CheckStatus.WARN
    assert report.status is health.HealthStatus.UNHEALTHY


# --------------------------------------------------------------------------------
# Operational findings
# --------------------------------------------------------------------------------


def test_stale_lease_is_reported_with_its_session_and_a_recover_action(tmp_path):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    _seed_cohort(database, "cohort-alpha")
    with database.session() as session:
        session.add(
            OfficialSessionLease(
                cohort_id="cohort-alpha",
                scheduled_for=date(2026, 7, 29),
                owner_id="machine-abc:process-1",
                lease_token_hash=b"\x00" * 32,
                acquired_at=NOW - timedelta(hours=12),
                expires_at=NOW - timedelta(hours=6),
                released_at=None,
            )
        )

    report = _assess(settings)

    assert report.leases.stale == 1
    assert report.leases.current == 0
    assert report.leases.stale_sessions == ("cohort-alpha/2026-07-29",)
    assert report.status is health.HealthStatus.UNHEALTHY
    assert "storage recover" in report.next_action
    # The owner id and token hash prove ownership; neither belongs in a report.
    payload = json.dumps(report.sanitized_payload())
    assert "machine-abc" not in payload
    assert "lease_token" not in payload


def test_live_lease_is_current_not_stale(tmp_path):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    _seed_cohort(database, "cohort-alpha")
    with database.session() as session:
        session.add(
            OfficialSessionLease(
                cohort_id="cohort-alpha",
                scheduled_for=date(2026, 7, 30),
                owner_id="machine-abc:process-1",
                lease_token_hash=b"\x00" * 32,
                acquired_at=NOW - timedelta(minutes=5),
                expires_at=NOW + timedelta(hours=5),
                released_at=None,
            )
        )

    report = _assess(settings)

    assert (report.leases.current, report.leases.stale) == (1, 0)
    leases = next(check for check in report.checks if check.name == "official_session_leases")
    assert leases.status is health.CheckStatus.OK


def test_incomplete_latest_run_is_reported(tmp_path):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    _seed_cohort(database, "cohort-alpha")
    with database.session() as session:
        session.add(
            CohortRun(
                run_id="run-1",
                run_key="cohort-alpha:2026-07-29",
                cohort_id="cohort-alpha",
                session_id="XNYS:2026-07-29",
                scheduled_for=date(2026, 7, 29),
                expected_members=["one", "two", "three"],
                completed_members=["one"],
                data_snapshot_ids={},
                started_at=NOW - timedelta(days=1),
                status="partial",
                errors=[],
            )
        )

    report = _assess(settings)

    assert report.official_runs == 1
    assert report.latest_run is not None
    assert report.latest_run.status == "partial"
    assert (report.latest_run.completed_members, report.latest_run.expected_members) == (1, 3)
    assert report.status is health.HealthStatus.UNHEALTHY
    assert "storage recover" in report.next_action


def test_completed_latest_run_is_not_a_finding(tmp_path):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    _seed_cohort(database, "cohort-alpha")
    with database.session() as session:
        session.add(
            CohortRun(
                run_id="run-1",
                run_key="cohort-alpha:2026-07-29",
                cohort_id="cohort-alpha",
                session_id="XNYS:2026-07-29",
                scheduled_for=date(2026, 7, 29),
                expected_members=["one"],
                completed_members=["one"],
                data_snapshot_ids={},
                started_at=NOW - timedelta(days=1),
                completed_at=NOW - timedelta(days=1),
                status="completed",
                errors=[],
            )
        )

    report = _assess(settings)

    assert report.status is health.HealthStatus.HEALTHY
    assert report.exit_code == 0


# --------------------------------------------------------------------------------
# Unconfigured shared storage
# --------------------------------------------------------------------------------


def test_no_shared_database_is_indeterminate_not_healthy():
    """Reporting "healthy" here would claim a shared database that does not exist."""
    report = _assess(_settings_for(""))

    assert report.backend is health.Backend.LOCAL_SQLITE_FILES
    assert report.status is health.HealthStatus.INDETERMINATE
    assert report.exit_code == 3
    assert report.connected is False
    assert "SCHWAB_DATABASE_URL" in report.next_action


# --------------------------------------------------------------------------------
# Human and JSON renderings must agree
# --------------------------------------------------------------------------------


def _run_cli(monkeypatch, settings: Settings, *arguments: str):
    """Drive the real CLI, pinning only the settings, clock, and checkout root.

    The wrapper always delegates to :data:`_REAL_ASSESS`, captured at import. The CLI
    reaches ``assess_health`` through the same module object this patches, so reading
    the name again would let a second call in one test wrap the first wrapper and
    pass ``repo_root`` twice.
    """
    monkeypatch.setattr("schwab_trader.storage.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        health,
        "assess_health",
        lambda config, **kwargs: _REAL_ASSESS(config, repo_root=REPO_ROOT, now=NOW, **kwargs),
    )
    return CliRunner().invoke(app, ["storage", *arguments])


@pytest.mark.parametrize("revision", ["head", "20260730_0004"])
def test_human_and_json_output_agree(tmp_path, monkeypatch, revision):
    settings, _url = _migrated_sqlite(tmp_path, revision=revision)
    expected = _assess(settings)

    human = _run_cli(monkeypatch, settings, "health")
    document = _run_cli(monkeypatch, settings, "health", "--json")

    assert human.exit_code == document.exit_code == expected.exit_code
    payload = json.loads(document.stdout)
    assert payload["status"] == expected.status.value
    assert payload["exit_code"] == expected.exit_code
    assert payload["contract_version"] == health.HEALTH_CONTRACT_VERSION

    # Every check the document reports is also named, with the same verdict, in the
    # human table — and there is no check in one rendering that the other omits.
    plain = " ".join(human.stdout.split())
    assert len(payload["checks"]) == len(expected.checks)
    for check in payload["checks"]:
        assert check["name"] in plain
        assert check["status"] in plain
    assert payload["backend"] in plain
    assert payload["tls_policy"] in plain
    assert payload["role"] in plain


def test_doctor_is_an_alias_for_health(tmp_path, monkeypatch):
    settings, _url = _migrated_sqlite(tmp_path)

    health_result = _run_cli(monkeypatch, settings, "health", "--json")
    doctor_result = _run_cli(monkeypatch, settings, "doctor", "--json")

    assert health_result.exit_code == doctor_result.exit_code == 0
    left = json.loads(health_result.stdout)
    right = json.loads(doctor_result.stdout)
    for key in ("status", "backend", "checks", "next_action"):
        assert left[key] == right[key]


# --------------------------------------------------------------------------------
# Secret redaction
# --------------------------------------------------------------------------------


def _forbidden(text_value: str) -> None:
    for sentinel in (SENTINEL_PASSWORD, SENTINEL_HOST, SENTINEL_USER, SENTINEL_DATABASE):
        assert sentinel not in text_value, f"{sentinel!r} leaked into output"


def test_sentinel_credentials_never_reach_the_report_or_either_rendering(
    tmp_path, monkeypatch
):
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    settings = _as_postgres(monkeypatch, database, _pg_facts())
    assert SENTINEL_PASSWORD in settings.database_url.get_secret_value()

    report = _assess(settings)
    _forbidden(json.dumps(report.sanitized_payload()))
    _forbidden(report.next_action)
    for check in report.checks:
        _forbidden(check.detail)

    human = _run_cli(monkeypatch, settings, "health")
    document = _run_cli(monkeypatch, settings, "health", "--json")
    _forbidden(human.stdout)
    _forbidden(document.stdout)


def test_health_never_writes_to_the_database(tmp_path):
    """The whole command is read-only; the file must be byte-identical afterwards."""
    settings, url = _migrated_sqlite(tmp_path)
    database = Database(url)
    _seed_cohort(database, "cohort-alpha")
    database.dispose()

    path = tmp_path / "shared.sqlite3"
    before = path.read_bytes()
    _assess(settings)
    _assess(settings, require_writer=True)

    assert path.read_bytes() == before
