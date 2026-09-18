"""Offline tests for the dry-run-first backup and export workflow.

Nothing here touches the operator's real SQLite files or Neon. Every source database
is a throwaway SQLite file under ``tmp_path``, every destination is a ``tmp_path``
directory, and the PostgreSQL path is exercised entirely through a fake ``pg_dump``
and ``pg_restore`` recorded by the test — no PostgreSQL server, no network, no Neon
API, and no ``.env``.

The most important assertions in this module are the negative ones: a sentinel
password, host, user, and database name are configured, and no argument vector,
manifest, log line, exception message, or terminal rendering may contain them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from typer.testing import CliRunner

from schwab_trader.cli import app
from schwab_trader.config import Settings
from schwab_trader.storage import backup
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.health import Backend

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 30, 15, 0, tzinfo=UTC)

SENTINEL_PASSWORD = "s3ntinel-pa55word-must-not-appear"
SENTINEL_HOST = "sentinel-host.invalid"
SENTINEL_USER = "sentinel-user"
SENTINEL_DATABASE = "sentinel-database"
SENTINEL_TOKEN = "sentinel-token-0000"
SENTINEL_URL = (
    f"postgresql+psycopg://{SENTINEL_USER}:{SENTINEL_PASSWORD}"
    f"@{SENTINEL_HOST}:5432/{SENTINEL_DATABASE}?sslmode=verify-full"
)
SENTINELS = (SENTINEL_PASSWORD, SENTINEL_HOST, SENTINEL_USER, SENTINEL_DATABASE, SENTINEL_TOKEN)


def _forbidden(value: str, *, where: str) -> None:
    for sentinel in SENTINELS:
        assert sentinel not in value, f"{sentinel!r} leaked into {where}"


def _migrated_sqlite(tmp_path: Path) -> tuple[Settings, Path]:
    path = tmp_path / "source" / "shared.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{path.as_posix()}"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return Settings(_env_file=None, database_url=url), path  # type: ignore[call-arg]


def _destination(tmp_path: Path) -> Path:
    path = tmp_path / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _plan(settings: Settings, destination: Path) -> backup.BackupPlan:
    return backup.plan_backup(settings, destination, now=NOW, repo_root=REPO_ROOT)


def _execute(settings: Settings, destination: Path, **kwargs) -> backup.BackupOutcome:
    return backup.execute_backup(settings, destination, now=NOW, repo_root=REPO_ROOT, **kwargs)


# --------------------------------------------------------------------------------
# Destination validation
# --------------------------------------------------------------------------------


def test_valid_destination_resolves(tmp_path):
    destination = _destination(tmp_path)
    assert backup.resolve_destination(destination, repo_root=REPO_ROOT) == destination.resolve()


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "required"),
        ("   ", "required"),
        ("relative/backups", "absolute"),
        ("$BACKUP_DIR/artifacts", "environment variable"),
        ("%BACKUP_DIR%\\artifacts", "environment variable"),
    ],
)
def test_ambiguous_destinations_are_rejected(raw, reason):
    with pytest.raises(backup.BackupError, match=reason):
        backup.resolve_destination(raw, repo_root=REPO_ROOT)


def test_traversal_is_rejected_before_resolution(tmp_path):
    destination = _destination(tmp_path)
    with pytest.raises(backup.BackupError, match="traversal"):
        backup.resolve_destination(destination / ".." / "backups", repo_root=REPO_ROOT)


def test_filesystem_root_is_rejected():
    root = Path(Path.cwd().anchor)
    with pytest.raises(backup.BackupError, match="filesystem or drive root"):
        backup.resolve_destination(root, repo_root=REPO_ROOT)


def test_home_directory_root_is_rejected():
    with pytest.raises(backup.BackupError, match="home directory root"):
        backup.resolve_destination(Path.home(), repo_root=REPO_ROOT)


def test_repository_root_is_rejected():
    with pytest.raises(backup.BackupError, match="repository working tree"):
        backup.resolve_destination(REPO_ROOT, repo_root=REPO_ROOT)


def test_ancestor_of_the_repository_root_is_rejected():
    with pytest.raises(backup.BackupError, match="ancestor of the repository root"):
        backup.resolve_destination(REPO_ROOT.parent, repo_root=REPO_ROOT)


# --- Repository containment (review finding #1) ---------------------------------
#
# Rejecting only the repo root and its ancestors let every path *inside* the checkout
# through. An artifact is a full copy of the shared database; landing one in the working
# tree puts the entire database into version-control history the moment anyone runs
# `git add -A`. These lock the whole subtree shut.


@pytest.mark.parametrize(
    "relative",
    [
        "docs",
        "data",
        "data/backups",
        "docs/operations",
        "src/schwab_trader",
        "tests",
        ".git",
    ],
)
def test_no_directory_inside_the_repository_is_accepted(relative):
    target = REPO_ROOT.joinpath(*relative.split("/"))
    with pytest.raises(backup.BackupError, match="repository working tree"):
        backup.resolve_destination(target, repo_root=REPO_ROOT)


def test_a_nested_descendant_is_rejected_even_when_it_does_not_exist():
    """Containment is decided before existence, so a typo'd deep path still fails here."""
    target = REPO_ROOT / "data" / "backups" / "nested" / "deeper"
    assert not target.exists()

    with pytest.raises(backup.BackupError, match="repository working tree"):
        backup.resolve_destination(target, repo_root=REPO_ROOT)


def test_a_path_normalized_into_the_repository_is_rejected(tmp_path):
    """`..` is refused outright, so it cannot be used to re-enter the checkout."""
    sneaky = REPO_ROOT / "docs" / ".." / "data"
    with pytest.raises(backup.BackupError, match="traversal"):
        backup.resolve_destination(sneaky, repo_root=REPO_ROOT)

    # And the same target, spelled without `..`, is refused on containment.
    with pytest.raises(backup.BackupError, match="repository working tree"):
        backup.resolve_destination(Path(os.path.normpath(sneaky)), repo_root=REPO_ROOT)


def test_a_link_resolving_into_the_repository_is_rejected(tmp_path):
    """Containment is judged on the canonical target, not on how it was spelled."""
    link = tmp_path / "innocent-looking-backups"
    target = REPO_ROOT / "data"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows symlinks need Developer Mode or elevation; a junction does not.
        if os.name != "nt":
            pytest.skip("this platform does not permit creating a directory symlink")
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0 or not link.exists():
            pytest.skip("neither a symlink nor a junction could be created here")

    assert link.resolve().is_relative_to(REPO_ROOT.resolve())
    with pytest.raises(backup.BackupError, match="repository working tree"):
        backup.resolve_destination(link, repo_root=REPO_ROOT)


@pytest.mark.skipif(os.name != "nt", reason="path case-insensitivity is Windows-specific")
@pytest.mark.parametrize("transform", [str.upper, str.lower])
def test_mixed_case_windows_paths_inside_the_repository_are_rejected(transform):
    target = transform(str(REPO_ROOT / "data"))
    with pytest.raises(backup.BackupError, match="repository working tree"):
        backup.resolve_destination(target, repo_root=REPO_ROOT)


@pytest.mark.skipif(os.name != "nt", reason="separator normalization is Windows-specific")
def test_forward_slash_windows_paths_inside_the_repository_are_rejected():
    target = str(REPO_ROOT / "data").replace("\\", "/")
    with pytest.raises(backup.BackupError, match="repository working tree"):
        backup.resolve_destination(target, repo_root=REPO_ROOT)


def test_a_sibling_with_a_shared_name_prefix_is_still_accepted(tmp_path):
    """Containment compares path components, not string prefixes.

    `<repo>-backups` starts with the repository path as a *string* but is not inside it.
    A `startswith` check would wrongly reject this legitimate destination.
    """
    fake_root = tmp_path / "checkout"
    fake_root.mkdir()
    sibling = tmp_path / "checkout-backups"
    sibling.mkdir()

    assert backup.resolve_destination(sibling, repo_root=fake_root) == sibling.resolve()


def test_an_ordinary_outside_directory_is_still_accepted(tmp_path):
    """The permitted case must keep working: outside the repo, home root, and roots."""
    destination = tmp_path / "schwab-backups"
    destination.mkdir()

    resolved = backup.resolve_destination(destination, repo_root=REPO_ROOT)

    assert resolved == destination.resolve()
    assert not resolved.is_relative_to(REPO_ROOT.resolve())


def test_missing_destination_directory_is_rejected_not_created(tmp_path):
    absent = tmp_path / "not" / "created" / "yet"
    with pytest.raises(backup.BackupError, match="does not exist"):
        backup.resolve_destination(absent, repo_root=REPO_ROOT)
    assert not absent.exists()


def test_file_destination_is_rejected(tmp_path):
    path = tmp_path / "artifact.dump"
    path.write_bytes(b"not a directory")
    with pytest.raises(backup.BackupError, match="must be a directory"):
        backup.resolve_destination(path, repo_root=REPO_ROOT)


# --------------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------------


def test_dry_run_produces_no_artifact(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)

    plan = _plan(settings, destination)

    assert plan.ready is True
    assert plan.blockers == ()
    assert plan.backend is Backend.SQLITE
    assert plan.artifact_name == "schwab-storage-sqlite-20260730T150000Z.sqlite3"
    assert plan.manifest_name == plan.artifact_name + ".manifest.json"
    assert plan.alembic_revision is not None
    assert "cohort_runs" in plan.table_counts
    # The whole point of the default mode: the destination is untouched.
    assert list(destination.iterdir()) == []


def test_dry_run_is_the_cli_default(tmp_path, monkeypatch):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    monkeypatch.setattr("schwab_trader.storage.cli.get_settings", lambda: settings)

    result = CliRunner().invoke(
        app, ["storage", "backup", "--destination", str(destination)]
    )

    assert result.exit_code == 0
    assert "Dry run complete" in result.stdout
    assert list(destination.iterdir()) == []


def test_unconfigured_shared_storage_blocks_before_any_path_work(tmp_path):
    settings = Settings(_env_file=None, database_url="")  # type: ignore[call-arg]
    with pytest.raises(backup.BackupError, match="no shared database is configured"):
        _plan(settings, _destination(tmp_path))


# --------------------------------------------------------------------------------
# SQLite online backup
# --------------------------------------------------------------------------------


def test_sqlite_backup_uses_the_online_api_and_verifies_the_artifact(tmp_path):
    settings, source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    before = source.read_bytes()

    outcome = _execute(settings, destination)

    artifact = destination / outcome.manifest.artifact_name
    manifest_file = destination / outcome.manifest_name
    assert artifact.is_file()
    assert manifest_file.is_file()
    assert outcome.manifest.verification is backup.Verification.PASSED
    assert outcome.manifest.sha256 == backup.file_sha256(artifact)
    assert outcome.manifest.artifact_bytes == artifact.stat().st_size
    assert outcome.manifest.manifest_version == backup.MANIFEST_VERSION
    assert outcome.manifest.backend is Backend.SQLITE
    assert outcome.manifest.alembic_revision is not None
    assert "cohort_runs" in outcome.manifest.table_counts
    # The source is opened read-only; a backup must never mutate what it copies.
    assert source.read_bytes() == before

    # The artifact is a usable database, not an opaque blob.
    with sqlite3.connect(artifact) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert {"cohort_runs", "official_daily_observations", "paper_fills"} <= names


def test_sqlite_backup_copies_rows_written_while_a_writer_holds_the_source(tmp_path):
    """The online API must handle an open source connection, not a quiesced file."""
    settings, source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)

    database = Database(f"sqlite:///{source.as_posix()}")
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO storage_namespaces "
            "(namespace_id, name, kind, created_at, immutable_metadata) "
            "VALUES ('ns-1', 'live', 'cohort', '2026-07-30T15:00:00+00:00', '{}')"
        )
    # Deliberately leave the engine (and its pooled connection) open across the backup.
    outcome = _execute(settings, destination)

    artifact = destination / outcome.manifest.artifact_name
    with sqlite3.connect(artifact) as connection:
        assert connection.execute("SELECT COUNT(*) FROM storage_namespaces").fetchone()[0] == 1
    assert outcome.manifest.table_counts["storage_namespaces"] == 1
    database.dispose()


def test_existing_artifact_name_is_never_overwritten(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    first = _execute(settings, destination)
    artifact = destination / first.manifest.artifact_name
    original = artifact.read_bytes()

    # Same instant, so the timestamped name collides.
    with pytest.raises(backup.BackupError, match="already exists"):
        _execute(settings, destination)

    assert artifact.read_bytes() == original


def test_a_later_backup_creates_a_new_timestamped_artifact(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)

    first = _execute(settings, destination)
    second = backup.execute_backup(
        settings,
        destination,
        now=NOW.replace(minute=5),
        repo_root=REPO_ROOT,
    )

    assert first.manifest.artifact_name != second.manifest.artifact_name
    # Old backups are never removed.
    assert (destination / first.manifest.artifact_name).is_file()
    assert (destination / second.manifest.artifact_name).is_file()


def test_corrupt_artifact_is_rejected_and_gets_no_manifest(tmp_path, monkeypatch):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)

    def truncated(_settings, artifact_path: Path) -> None:
        artifact_path.write_bytes(b"SQLite format 3\x00 truncated garbage")

    monkeypatch.setattr(backup, "_sqlite_online_backup", truncated)

    with pytest.raises(backup.BackupError, match="failed verification"):
        _execute(settings, destination)

    names = {path.name for path in destination.iterdir()}
    assert not any(name.endswith(".manifest.json") for name in names)
    assert any(name.endswith(".rejected") for name in names)


def test_count_mismatch_is_rejected_even_when_the_artifact_is_a_valid_database(
    tmp_path, monkeypatch
):
    """A structurally sound database missing rows is still an incomplete backup."""
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    database = Database(f"sqlite:///{(tmp_path / 'source' / 'shared.sqlite3').as_posix()}")
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO storage_namespaces "
            "(namespace_id, name, kind, created_at, immutable_metadata) "
            "VALUES ('ns-1', 'live', 'cohort', '2026-07-30T15:00:00+00:00', '{}')"
        )
    database.dispose()

    real = backup._sqlite_online_backup

    def backup_then_lose_a_row(config, artifact_path: Path) -> None:
        real(config, artifact_path)
        with sqlite3.connect(artifact_path) as connection:
            connection.execute("DELETE FROM storage_namespaces WHERE namespace_id = 'ns-1'")

    monkeypatch.setattr(backup, "_sqlite_online_backup", backup_then_lose_a_row)

    with pytest.raises(backup.BackupError, match="failed verification"):
        _execute(settings, destination)


# --------------------------------------------------------------------------------
# Independent artifact verification
# --------------------------------------------------------------------------------


def test_verify_artifact_passes_for_an_untouched_backup(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    outcome = _execute(settings, destination)

    result = backup.verify_artifact(destination / outcome.manifest_name)

    assert result.result is backup.Verification.PASSED
    assert result.reasons == ()
    assert result.sha256_actual == result.sha256_expected


def test_verify_artifact_rejects_a_hash_mismatch(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    outcome = _execute(settings, destination)
    artifact = destination / outcome.manifest.artifact_name

    with artifact.open("r+b") as handle:
        handle.seek(0, os.SEEK_END)
        handle.write(b"\x00" * 4096)

    result = backup.verify_artifact(destination / outcome.manifest_name)

    assert result.result is backup.Verification.FAILED
    assert any("SHA-256 mismatch" in reason for reason in result.reasons)
    assert any("size mismatch" in reason for reason in result.reasons)


def test_verify_artifact_rejects_a_missing_artifact(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    outcome = _execute(settings, destination)
    (destination / outcome.manifest.artifact_name).unlink()

    result = backup.verify_artifact(destination / outcome.manifest_name)

    assert result.result is backup.Verification.FAILED
    assert result.reasons == ("the artifact named by the manifest does not exist",)


def test_verify_artifact_rejects_a_missing_or_unparseable_manifest(tmp_path):
    destination = _destination(tmp_path)
    assert backup.verify_artifact(destination / "absent.manifest.json").result is (
        backup.Verification.FAILED
    )

    broken = destination / "broken.manifest.json"
    broken.write_text("{not json", encoding="utf-8")
    result = backup.verify_artifact(broken)
    assert result.result is backup.Verification.FAILED
    assert any("could not be parsed" in reason for reason in result.reasons)


def test_cli_backup_verify_exits_non_zero_on_a_bad_artifact(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    outcome = _execute(settings, destination)
    (destination / outcome.manifest.artifact_name).write_bytes(b"corrupt")

    result = CliRunner().invoke(
        app, ["storage", "backup-verify", "--manifest", str(destination / outcome.manifest_name)]
    )

    assert result.exit_code == 1
    assert "failed" in result.stdout


# --------------------------------------------------------------------------------
# PostgreSQL tooling
# --------------------------------------------------------------------------------


#: The genuine ``subprocess.run``, captured before any fake replaces it. The manifest's
#: code revision comes from a real ``git`` call, so a fake must let that through.
_REAL_RUN = subprocess.run


#: A plausible table of contents listing, as `pg_restore --list` prints one.
_TOC_LISTING = b"; Archive created at 2026-07-30\n2; 1259 TABLE cohort_runs\n"


class _RecordedRun:
    """Stands in for the PostgreSQL client tools and records every invocation.

    It answers the three shapes the module actually issues — ``--version``, ``--list``,
    and the full-read ``--file`` conversion — so a test can assert on the argument
    vector and the child environment without a PostgreSQL installation.
    """

    def __init__(
        self,
        *,
        dump_version: bytes | None = b"pg_dump (PostgreSQL) 16.3\n",
        restore_version: bytes | None = b"pg_restore (PostgreSQL) 16.3\n",
        toc: bytes = _TOC_LISTING,
        full_read_status: int = 0,
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.dump_version = dump_version
        self.restore_version = restore_version
        self.toc = toc
        self.full_read_status = full_read_status

    def __call__(self, argv, **kwargs):
        tool = Path(argv[0]).stem
        if tool not in {"pg_dump", "pg_restore"}:
            # The manifest's code revision comes from a real `git` call; let it through.
            return _REAL_RUN(argv, **kwargs)
        self.calls.append((list(argv), dict(kwargs.get("env") or {})))

        if "--version" in argv:
            reported = self.dump_version if tool == "pg_dump" else self.restore_version
            if reported is None:
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            return subprocess.CompletedProcess(argv, 0, reported, b"")

        if tool == "pg_dump":
            target = next(
                argument.split("=", 1)[1] for argument in argv if argument.startswith("--file=")
            )
            Path(target).write_bytes(b"PGDMP fake custom-format dump payload")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        if "--list" in argv:
            return subprocess.CompletedProcess(argv, 0, self.toc, b"")

        # The full-read stage: `pg_restore --file <scratch> <archive>`. A real run
        # writes the converted SQL script; the status is what the module reads.
        sink = Path(argv[argv.index("--file") + 1])
        if self.full_read_status == 0:
            sink.write_text("-- converted SQL script\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.full_read_status, b"", b"")

    def tools_called(self) -> list[str]:
        """Tool stems in call order, excluding version probes."""
        return [
            Path(argv[0]).stem for argv, _env in self.calls if "--version" not in argv
        ]

    def work_call(self, tool: str) -> tuple[list[str], dict[str, str]]:
        """The first non-``--version`` invocation of ``tool``.

        Version probes are deliberately excluded: they carry a plain environment by
        design, so asserting credential handling against one would prove nothing.
        """
        return next(
            (argv, env)
            for argv, env in self.calls
            if Path(argv[0]).stem == tool and "--version" not in argv
        )


def _version_reply(argv):
    """Answer a ``--version`` probe for a test fake that only models failures."""
    tool = Path(argv[0]).stem
    return subprocess.CompletedProcess(argv, 0, f"{tool} (PostgreSQL) 16.3\n".encode(), b"")


@pytest.fixture
def postgres_backup(tmp_path, monkeypatch):
    """A PostgreSQL-configured backup whose client tools are faked and recorded.

    The SQLite schema stands in for the source so table counts and the revision are
    real. ``factory.database`` is redirected at it, so the sentinel URL is never
    dialled and the offline guarantee holds.
    """
    _settings, source = _migrated_sqlite(tmp_path)
    database = Database(f"sqlite:///{source.as_posix()}")
    monkeypatch.setattr(storage_factory, "database", lambda _settings: database)
    monkeypatch.setattr(backup, "backend_of", lambda _raw: Backend.POSTGRESQL)

    recorder = _RecordedRun()
    monkeypatch.setattr(backup.subprocess, "run", recorder)
    monkeypatch.setattr(
        backup.shutil,
        "which",
        lambda tool: f"/usr/bin/{tool}" if tool in {"pg_dump", "pg_restore"} else None,
    )
    settings = Settings(_env_file=None, database_url=SENTINEL_URL)  # type: ignore[call-arg]
    yield settings, _destination(tmp_path), recorder
    database.dispose()


def test_postgres_backup_invokes_pg_dump_then_checks_toc_then_reads_every_block(
    postgres_backup,
):
    settings, destination, recorder = postgres_backup

    outcome = _execute(settings, destination)

    # Dump, then the cheap TOC parse, then the full read of every data block.
    assert recorder.tools_called() == ["pg_dump", "pg_restore", "pg_restore"]
    assert outcome.manifest.backend is Backend.POSTGRESQL
    assert outcome.manifest.artifact_name.endswith(".dump")
    assert outcome.manifest.verification is backup.Verification.PASSED
    assert outcome.manifest.verification_level is (
        backup.VerificationLevel.POSTGRESQL_ARCHIVE_FULL_READ
    )
    assert outcome.manifest.restore_tested is False
    assert (destination / outcome.manifest_name).is_file()

    dump_argv, _env = recorder.work_call("pg_dump")
    assert "--format=custom" in dump_argv
    assert "--no-password" in dump_argv


def test_no_connection_material_appears_in_the_argument_vector(postgres_backup):
    """The core credential-safety property: ``argv`` is visible to every local process."""
    settings, destination, recorder = postgres_backup

    _execute(settings, destination)

    for argv, _env in recorder.calls:
        _forbidden(" ".join(argv), where="the subprocess argument vector")


def test_connection_material_reaches_the_child_only_through_the_environment(postgres_backup):
    settings, destination, recorder = postgres_backup

    _execute(settings, destination)

    _dump_argv, dump_env = recorder.work_call("pg_dump")
    assert dump_env["PGHOST"] == SENTINEL_HOST
    assert dump_env["PGUSER"] == SENTINEL_USER
    assert dump_env["PGPASSWORD"] == SENTINEL_PASSWORD
    assert dump_env["PGDATABASE"] == SENTINEL_DATABASE
    assert dump_env["PGPORT"] == "5432"
    assert dump_env["PGSSLMODE"] == "verify-full"


def test_no_sentinel_reaches_the_manifest_or_either_rendering(postgres_backup, monkeypatch):
    settings, destination, _recorder = postgres_backup
    outcome = _execute(settings, destination)

    _forbidden(json.dumps(outcome.sanitized_payload()), where="the outcome payload")
    _forbidden(
        (destination / outcome.manifest_name).read_text(encoding="utf-8"),
        where="the manifest",
    )

    monkeypatch.setattr("schwab_trader.storage.cli.get_settings", lambda: settings)
    preflight = CliRunner().invoke(app, ["storage", "backup", "--destination", str(destination)])
    _forbidden(preflight.stdout, where="the preflight rendering")

    document = CliRunner().invoke(
        app, ["storage", "backup", "--destination", str(destination), "--json"]
    )
    _forbidden(document.stdout, where="the JSON preflight rendering")


def test_pg_dump_failure_suppresses_tool_output_that_names_the_host(
    postgres_backup, monkeypatch
):
    settings, destination, _recorder = postgres_backup

    def failing(argv, **kwargs):
        if Path(argv[0]).stem not in {"pg_dump", "pg_restore"}:
            return _REAL_RUN(argv, **kwargs)
        if "--version" in argv:
            # The tools are installed and compatible; it is the *connection* that fails.
            return _version_reply(argv)
        # Exactly what libpq writes when it cannot connect.
        stderr = (
            f'pg_dump: error: connection to server at "{SENTINEL_HOST}" (10.0.0.1), '
            f'port 5432 failed: FATAL:  password authentication failed for user '
            f'"{SENTINEL_USER}"'
        ).encode()
        return subprocess.CompletedProcess(argv, 1, b"", stderr)

    monkeypatch.setattr(backup.subprocess, "run", failing)

    with pytest.raises(backup.BackupError) as caught:
        _execute(settings, destination)

    message = str(caught.value)
    assert "pg_dump exited with status 1" in message
    assert "output is suppressed" in message
    _forbidden(message, where="the BackupError message")


def test_missing_pg_dump_is_an_actionable_sanitized_blocker(postgres_backup, monkeypatch):
    settings, destination, _recorder = postgres_backup
    monkeypatch.setattr(backup.shutil, "which", lambda _tool: None)

    plan = _plan(settings, destination)

    assert plan.ready is False
    assert any("pg_dump was not found on PATH" in blocker for blocker in plan.blockers)
    assert any("pg_restore was not found on PATH" in blocker for blocker in plan.blockers)
    for blocker in plan.blockers:
        _forbidden(blocker, where="a preflight blocker")

    with pytest.raises(backup.BackupError, match="pg_dump was not found"):
        _execute(settings, destination)
    assert list(destination.iterdir()) == []


def test_a_postgres_preflight_without_tools_does_not_claim_the_sqlite_path(
    postgres_backup, monkeypatch
):
    """Regression: a missing pg_dump must not be rendered as the SQLite backup API.

    `plan.tool` is None both for SQLite, which needs no external tool, and for a
    PostgreSQL preflight that could not find pg_dump. The renderer collapsed the two.
    Observed on a real preflight with the PostgreSQL tools off PATH (issue #108).
    """
    settings, destination, _recorder = postgres_backup
    monkeypatch.setattr(backup.shutil, "which", lambda _tool: None)
    monkeypatch.setattr("schwab_trader.storage.cli.get_settings", lambda: settings)
    monkeypatch.setenv("COLUMNS", "400")

    result = CliRunner().invoke(app, ["storage", "backup", "--destination", str(destination)])

    assert result.exit_code == 1
    assert "postgresql" in result.stdout
    assert "sqlite online backup API" not in result.stdout
    assert "pg_dump not found" in result.stdout
    _forbidden(result.stdout, where="a toolless PostgreSQL preflight rendering")


def test_the_reported_tool_name_carries_no_platform_file_extension(postgres_backup, monkeypatch):
    """On Windows `shutil.which` returns `pg_dump.EXE`; the report should not.

    The fake spells its directories with forward slashes on purpose. A backslash is an
    ordinary filename character to `PosixPath`, so a Windows-shaped fake makes `.stem`
    return the whole `C:\\PostgreSQL\\bin\\pg_dump` on Linux and this assertion fails
    over separator conventions rather than the extension stripping it exists to pin.
    Forward slashes parse identically under `PurePosixPath` and `PureWindowsPath`.
    """
    monkeypatch.setattr(
        backup.shutil,
        "which",
        lambda tool: (
            f"C:/PostgreSQL/bin/{tool}.EXE" if tool in {"pg_dump", "pg_restore"} else None
        ),
    )
    settings, destination, _recorder = postgres_backup

    assert _plan(settings, destination).tool == "pg_dump"


def test_empty_pg_dump_table_of_contents_is_rejected(postgres_backup, monkeypatch):
    settings, destination, _recorder = postgres_backup

    def dump_then_empty_listing(argv, **kwargs):
        if Path(argv[0]).stem not in {"pg_dump", "pg_restore"}:
            return _REAL_RUN(argv, **kwargs)
        if "--version" in argv:
            return _version_reply(argv)
        if Path(argv[0]).stem == "pg_dump":
            target = next(a.split("=", 1)[1] for a in argv if a.startswith("--file="))
            Path(target).write_bytes(b"PGDMP")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        # A restore listing with only comments describes an archive that restores nothing.
        return subprocess.CompletedProcess(argv, 0, b"; Archive created at 2026-07-30\n", b"")

    monkeypatch.setattr(backup.subprocess, "run", dump_then_empty_listing)

    with pytest.raises(backup.BackupError, match="failed verification"):
        _execute(settings, destination)


def test_libpq_environment_omits_absent_components():
    environment = backup.libpq_environment("postgresql+psycopg://localhost/app", base={})
    assert environment["PGHOST"] == "localhost"
    assert environment["PGDATABASE"] == "app"
    assert "PGPASSWORD" not in environment
    assert "PGUSER" not in environment
    assert environment["PGCONNECT_TIMEOUT"] == "30"


# --------------------------------------------------------------------------------
# Verification scope (review finding #2)
#
# `pg_restore --list` reads only the archive catalogue. Recording that as an
# unqualified "verified" invited the reader to assume the artifact was known to
# restore. These pin what each level actually claims.
# --------------------------------------------------------------------------------


def test_sqlite_manifest_records_a_full_read_and_no_restore_test(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    outcome = _execute(settings, _destination(tmp_path))

    assert outcome.manifest.verification is backup.Verification.PASSED
    assert outcome.manifest.verification_level is backup.VerificationLevel.SQLITE_FULL_READ
    assert outcome.manifest.restore_tested is False
    assert "every page was read" in outcome.manifest.verification_detail
    assert outcome.manifest.manifest_version == backup.MANIFEST_VERSION == 2


def test_truncation_after_the_table_of_contents_is_caught_by_the_full_read(
    postgres_backup, monkeypatch
):
    """The exact gap the review named: a readable TOC over unreadable data blocks.

    `--list` succeeds because the catalogue at the front of the archive is intact; only
    the full read reaches the truncated data and fails.
    """
    settings, destination, _recorder = postgres_backup
    monkeypatch.setattr(
        backup.subprocess, "run", _RecordedRun(full_read_status=1)
    )

    with pytest.raises(backup.BackupError) as caught:
        _execute(settings, destination)

    message = str(caught.value)
    assert "truncated or corrupt after its table of contents" in message
    _forbidden(message, where="the truncation BackupError")
    names = {path.name for path in destination.iterdir()}
    assert not any(name.endswith(".manifest.json") for name in names)
    assert any(name.endswith(".rejected") for name in names)


def test_a_readable_toc_alone_is_never_recorded_as_a_full_read(postgres_backup, monkeypatch):
    """When the scratch area cannot be made, the weaker level is recorded honestly.

    The artifact is not condemned — nothing about it failed — but the manifest must not
    claim a check that did not run.
    """
    settings, destination, _recorder = postgres_backup

    def no_scratch(*args, **kwargs):
        raise OSError("scratch directory unavailable")

    monkeypatch.setattr(backup.tempfile, "TemporaryDirectory", no_scratch)

    outcome = _execute(settings, destination)

    assert outcome.manifest.verification is backup.Verification.PASSED
    assert outcome.manifest.verification_level is (
        backup.VerificationLevel.POSTGRESQL_ARCHIVE_TOC
    )
    assert "were NOT read" in outcome.manifest.verification_detail
    assert "could not create a scratch directory" in outcome.manifest.verification_detail
    assert outcome.manifest.restore_tested is False


def test_backup_verify_reports_why_the_full_read_was_skipped(postgres_backup, monkeypatch):
    """The skipped-full-read reason must reach `backup-verify`, not only the manifest.

    `execute_backup` folds `PgVerification.reason` into the manifest detail, so the
    manifest says *why* the weaker level was recorded. `verify_artifact` described the
    same artifact under the same condition without it, so the two disagreed about the
    one thing an operator would act on.
    """
    settings, destination, _recorder = postgres_backup

    def no_scratch(*args, **kwargs):
        raise OSError("scratch directory unavailable")

    monkeypatch.setattr(backup.tempfile, "TemporaryDirectory", no_scratch)

    outcome = _execute(settings, destination)
    result = backup.verify_artifact(destination / outcome.manifest_name)

    assert result.result is backup.Verification.PASSED
    assert result.verification_level is backup.VerificationLevel.POSTGRESQL_ARCHIVE_TOC
    # The level sentence was always honest about coverage; the reason for it was not.
    assert "were NOT read" in result.verification_detail
    assert "could not create a scratch directory" in result.verification_detail
    # One artifact, two renderings: they must not disagree.
    assert result.verification_detail == outcome.manifest.verification_detail
    _forbidden(result.verification_detail, where="a skipped-full-read verification detail")


def test_the_full_read_scratch_file_is_always_removed(postgres_backup):
    settings, destination, _recorder = postgres_backup

    outcome = _execute(settings, destination)

    # Only the artifact and its manifest survive; no converted SQL script is left behind.
    assert sorted(path.name for path in destination.iterdir()) == sorted(
        [outcome.manifest.artifact_name, outcome.manifest_name]
    )


def test_every_verification_level_has_a_detail_sentence():
    for level in backup.VerificationLevel:
        assert backup.VERIFICATION_DETAIL[level]


def test_cli_never_calls_an_artifact_restore_tested(postgres_backup, monkeypatch):
    settings, destination, _recorder = postgres_backup
    monkeypatch.setattr("schwab_trader.storage.cli.get_settings", lambda: settings)
    monkeypatch.setenv("COLUMNS", "400")

    written = CliRunner().invoke(
        app, ["storage", "backup", "--destination", str(destination), "--execute"]
    )

    assert written.exit_code == 0
    assert "Not restore-tested" in written.stdout
    # "Backup verified" read as a restore guarantee; it is gone.
    assert "Backup verified" not in written.stdout
    assert "postgresql-archive-full-read" in written.stdout
    _forbidden(written.stdout, where="the execute rendering")

    manifest = next(path for path in destination.iterdir() if path.name.endswith(".json"))
    checked = CliRunner().invoke(
        app, ["storage", "backup-verify", "--manifest", str(manifest)]
    )
    assert checked.exit_code == 0
    assert "Not restore-tested" in checked.stdout


def test_verify_artifact_reports_the_level_it_reached(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    outcome = _execute(settings, destination)

    result = backup.verify_artifact(destination / outcome.manifest_name)

    assert result.result is backup.Verification.PASSED
    assert result.verification_level is backup.VerificationLevel.SQLITE_FULL_READ
    assert result.restore_tested is False
    assert "every page was read" in result.verification_detail


def _rewrite_manifest_to_match(manifest_path: Path) -> None:
    """Point a manifest's size and hash at whatever its artifact currently holds.

    Without this, `verify_artifact` stops at the cheap size/SHA-256 guard and never
    reaches the backend read check — which is the stage these tests are about.
    """
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = manifest_path.parent / document["artifact_name"]
    document["artifact_bytes"] = artifact.stat().st_size
    document["sha256"] = backup.file_sha256(artifact)
    manifest_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_a_failed_read_check_is_never_described_as_a_successful_one(tmp_path):
    """Regression: the detail sentence must not claim a check the artifact just failed.

    Found while validating issue #108 against real `pg_dump`/`pg_restore`. Verifying a
    genuinely post-TOC-truncated archive printed `Checked: postgresql-archive-full-read
    — pg_restore decompressed and parsed every data block`, which is the opposite of
    what happened. The offline suite had never caught it because its only failing-read
    case goes through `execute_backup`, which raises instead of rendering a report.
    """
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    outcome = _execute(settings, destination)

    manifest_path = destination / outcome.manifest_name
    (destination / outcome.manifest.artifact_name).write_bytes(b"not a database at all")
    _rewrite_manifest_to_match(manifest_path)

    result = backup.verify_artifact(manifest_path)

    assert result.result is backup.Verification.FAILED
    assert result.verification_level is backup.VerificationLevel.SQLITE_FULL_READ
    # The pass sentence must be absent, and the detail must say the check failed.
    assert "every page was read" not in result.verification_detail
    assert "did not pass it" in result.verification_detail
    assert "nothing about this artifact was confirmed" in result.verification_detail
    assert any("did not pass its backend's check" in reason for reason in result.reasons)


def test_a_failed_postgres_full_read_is_never_described_as_a_successful_one(
    postgres_backup, monkeypatch
):
    """The PostgreSQL shape of the same regression, at the level real tools reached."""
    settings, destination, _recorder = postgres_backup
    outcome = _execute(settings, destination)
    manifest_path = destination / outcome.manifest_name

    # Same archive, but now the full-read stage fails the way a truncated one does.
    monkeypatch.setattr(backup.subprocess, "run", _RecordedRun(full_read_status=1))

    result = backup.verify_artifact(manifest_path)

    assert result.result is backup.Verification.FAILED
    assert result.verification_level is backup.VerificationLevel.POSTGRESQL_ARCHIVE_FULL_READ
    assert "decompressed and parsed every data block" not in result.verification_detail
    assert "did not pass it" in result.verification_detail
    _forbidden(result.verification_detail, where="a failed verification detail")


def test_a_failed_full_read_does_not_deny_the_stage_that_passed(postgres_backup, monkeypatch):
    """A post-TOC failure must not claim the table of contents never parsed.

    `_verify_pg_artifact` returns before the full read if `pg_restore --list` fails or
    the catalogue is empty, so reaching a POSTGRESQL_ARCHIVE_FULL_READ failure proves the
    header and a non-empty TOC did parse. "nothing about this artifact was confirmed"
    contradicts POSTGRESQL_ARCHIVE_TOC, the level this module records for exactly that.
    """
    settings, destination, _recorder = postgres_backup
    outcome = _execute(settings, destination)
    manifest_path = destination / outcome.manifest_name

    monkeypatch.setattr(backup.subprocess, "run", _RecordedRun(full_read_status=1))

    result = backup.verify_artifact(manifest_path)

    assert result.result is backup.Verification.FAILED
    assert result.verification_level is backup.VerificationLevel.POSTGRESQL_ARCHIVE_FULL_READ
    assert "did not pass it" in result.verification_detail
    assert "nothing about this artifact was confirmed" not in result.verification_detail
    assert "table of contents parsed" in result.verification_detail
    _forbidden(result.verification_detail, where="a failed full-read verification detail")


def test_failed_verification_detail_only_credits_a_stage_that_actually_ran():
    """The narrowing applies only where a stage demonstrably passed underneath.

    A failed TOC parse is the first stage of the PostgreSQL check, with nothing
    established beneath it, so the unqualified sentence stays correct there.
    """
    toc = backup.VerificationLevel.POSTGRESQL_ARCHIVE_TOC
    full = backup.VerificationLevel.POSTGRESQL_ARCHIVE_FULL_READ

    assert "nothing about this artifact was confirmed" in backup.failed_verification_detail(toc)
    assert "table of contents parsed" not in backup.failed_verification_detail(toc)

    assert "nothing about this artifact was confirmed" not in backup.failed_verification_detail(
        full
    )
    assert "table of contents parsed" in backup.failed_verification_detail(full)


def test_a_check_that_never_ran_is_not_described_as_one_that_ran(postgres_backup):
    """Regression: a NOT_RUN outcome keeps the not-run sentence, not the failed one.

    `_verify_pg_artifact` returns NOT_RUN for an artifact that is missing or empty,
    having attempted nothing at all. Routing that through `failed_verification_detail`
    produced "the not-run check was attempted and the artifact did not pass it", which
    contradicts itself in the same breath. Only reachable through a hand-edited or
    corrupt manifest, since `execute_backup` rejects a zero-byte artifact rather than
    writing one — so the manifest here is rewritten to describe the emptied artifact
    accurately, which is what gets past the cheap size and SHA-256 guards.
    """
    settings, destination, _recorder = postgres_backup
    outcome = _execute(settings, destination)

    manifest_path = destination / outcome.manifest_name
    (destination / outcome.manifest.artifact_name).write_bytes(b"")
    _rewrite_manifest_to_match(manifest_path)

    result = backup.verify_artifact(manifest_path)

    not_run = backup.VerificationLevel.NOT_RUN
    assert result.result is backup.Verification.FAILED
    assert result.verification_level is not_run
    assert result.verification_detail == backup.VERIFICATION_DETAIL[not_run]
    assert "was attempted" not in result.verification_detail
    # The detail says nothing ran; the reasons still have to say why it failed.
    assert any("the artifact is missing or empty" in reason for reason in result.reasons)
    _forbidden(result.verification_detail, where="a not-run verification detail")


def test_cli_does_not_print_a_pass_sentence_for_a_failed_artifact(tmp_path):
    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    outcome = _execute(settings, destination)
    manifest_path = destination / outcome.manifest_name
    (destination / outcome.manifest.artifact_name).write_bytes(b"not a database at all")
    _rewrite_manifest_to_match(manifest_path)

    result = CliRunner().invoke(app, ["storage", "backup-verify", "--manifest", str(manifest_path)])

    assert result.exit_code == 1
    assert "every page was read" not in result.stdout
    assert "did not pass it" in result.stdout


# --------------------------------------------------------------------------------
# Client/server version compatibility (review finding #3)
# --------------------------------------------------------------------------------


def test_matching_tool_and_server_versions_produce_no_blockers(postgres_backup):
    settings, destination, _recorder = postgres_backup

    plan = _plan(settings, destination)

    assert plan.ready is True
    assert plan.pg_dump_major == 16
    assert plan.pg_restore_major == 16


def test_version_probe_never_opens_a_connection(postgres_backup):
    """`--version` must be the only argument, so no libpq connection is attempted."""
    settings, destination, recorder = postgres_backup

    _plan(settings, destination)

    probes = [argv for argv, _env in recorder.calls if "--version" in argv]
    assert len(probes) == 2
    for argv in probes:
        assert argv[1:] == ["--version"]


@pytest.mark.parametrize(
    ("dump", "restore", "expected"),
    [
        (b"pg_dump (PostgreSQL) 16.3\n", b"pg_restore (PostgreSQL) 15.6\n", "same client"),
        (None, b"pg_restore (PostgreSQL) 16.3\n", "pg_dump did not report"),
        (b"pg_dump (PostgreSQL) 16.3\n", None, "pg_restore did not report"),
    ],
)
def test_incompatible_client_tools_block_the_preflight(
    postgres_backup, monkeypatch, dump, restore, expected
):
    settings, destination, _recorder = postgres_backup
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        _RecordedRun(dump_version=dump, restore_version=restore),
    )

    plan = _plan(settings, destination)

    assert plan.ready is False
    assert any(expected in blocker for blocker in plan.blockers)
    for blocker in plan.blockers:
        _forbidden(blocker, where="a version blocker")

    with pytest.raises(backup.BackupError):
        _execute(settings, destination)
    assert list(destination.iterdir()) == []


def test_client_older_than_the_server_blocks_the_preflight(postgres_backup, monkeypatch):
    settings, destination, _recorder = postgres_backup
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        _RecordedRun(
            dump_version=b"pg_dump (PostgreSQL) 14.11\n",
            restore_version=b"pg_restore (PostgreSQL) 14.11\n",
        ),
    )
    monkeypatch.setattr(backup, "_server_major_version", lambda _database: 16)

    plan = _plan(settings, destination)

    assert plan.ready is False
    assert plan.server_major == 16
    assert any(
        "pg_dump is major version 14 but the server is 16" in blocker
        for blocker in plan.blockers
    )


def test_a_client_newer_than_the_server_is_allowed(postgres_backup, monkeypatch):
    """PostgreSQL supports dumping an older server with newer client tools."""
    settings, destination, _recorder = postgres_backup
    monkeypatch.setattr(backup, "_server_major_version", lambda _database: 15)

    plan = _plan(settings, destination)

    assert plan.ready is True
    assert (plan.pg_dump_major, plan.server_major) == (16, 15)


def test_an_unreadable_server_version_does_not_block(postgres_backup, monkeypatch):
    """A comparison that cannot be made is skipped, not guessed at."""
    settings, destination, _recorder = postgres_backup
    monkeypatch.setattr(backup, "_server_major_version", lambda _database: None)

    plan = _plan(settings, destination)

    assert plan.ready is True
    assert plan.server_major is None


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (b"pg_dump (PostgreSQL) 16.3\n", 16),
        (b"pg_dump (PostgreSQL) 17.2 (Debian 17.2-1.pgdg120+1)\n", 17),
        (b"pg_dump (PostgreSQL) 9.6.24\n", 9),
        (b"pg_dump (PostgreSQL) 18beta1\n", 18),
        # A vendor build that drops the parenthesised product name still parses.
        (b"pg_dump 18beta1\n", 18),
        (b"\n", None),
        (b"pg_dump: unrecognized option\n", None),
    ],
)
def test_tool_version_parsing_keeps_only_the_integer(monkeypatch, reported, expected):
    monkeypatch.setattr(backup.shutil, "which", lambda _tool: "/usr/bin/pg_dump")
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, reported, b""),
    )

    assert backup.pg_tool_major_version("pg_dump") == expected


def test_version_blockers_contain_only_tool_names_and_integers():
    blockers = backup.pg_version_blockers(dump_major=14, restore_major=15, server_major=16)

    assert len(blockers) == 2
    joined = " ".join(blockers)
    _forbidden(joined, where="version blockers")
    assert "sslmode" not in joined and "://" not in joined


def test_a_missing_tool_reports_no_version_rather_than_raising(monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda _tool: None)

    assert backup.pg_tool_major_version("pg_dump") is None


# --------------------------------------------------------------------------------
# Offline enforcement
# --------------------------------------------------------------------------------


def test_backup_never_opens_a_network_socket(tmp_path, monkeypatch):
    """A regression guard: the SQLite path must not reach for a socket at all."""
    import socket

    def refuse(*args, **kwargs):  # pragma: no cover - only runs on a failure
        raise AssertionError("the backup workflow attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    settings, _source = _migrated_sqlite(tmp_path)
    destination = _destination(tmp_path)
    outcome = _execute(settings, destination)

    assert outcome.manifest.verification is backup.Verification.PASSED
