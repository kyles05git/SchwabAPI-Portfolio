"""Safe, dry-run-first backup and export of application-owned shared storage.

Design rules, all of which have tests:

- **Dry run is the default.** :func:`plan_backup` never writes. Producing an artifact
  requires an explicit execute flag *and* an exact destination directory.
- **No credential ever reaches a process argument.** PostgreSQL connection material is
  passed to ``pg_dump`` through libpq environment variables in the child process only.
  The argument vector contains no URL, host, user, password, or database name.
- **No credential ever reaches output.** ``pg_dump``/``pg_restore`` stderr routinely
  contains ``connection to server at "HOST" (IP), port ...``. It is never surfaced;
  failures report the tool name and exit status only.
- **Nothing is overwritten.** Artifacts are timestamped and the writer refuses a name
  that already exists. Old backups are never deleted.
- **Unverified means failed.** Every artifact is checked before success is reported. A
  corrupt or truncated artifact gets no manifest and is renamed aside so it cannot be
  mistaken for a usable backup.
- **Verification is never a restore.** Nothing here loads an artifact into a database,
  so nothing here can prove one restores. The manifest records *which* checks ran
  (:class:`VerificationLevel`) and states ``restore_tested: false`` outright. Proving
  restorability is the operator rehearsal in
  ``docs/operations/storage-backup-restore.md``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import make_url

from schwab_trader.config import Settings
from schwab_trader.storage import factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.health import (
    Backend,
    alembic_revisions,
    backend_of,
    gather_server_facts,
    present_tables,
    sqlite_path,
    table_counts,
)

#: Bump only for a breaking change to the manifest document.
#:
#: v2 added ``verification_level`` and ``restore_tested``. v1 recorded only
#: ``verification: passed``, which readers could reasonably — and wrongly — take to mean
#: the artifact had been proven restorable. It never meant that, and now it cannot be
#: read that way.
MANIFEST_VERSION = 2

#: How long a subprocess may run before it is killed. Long enough for a real dump of
#: this application's data, short enough that a hung tool does not wedge a scheduled task.
SUBPROCESS_TIMEOUT_SECONDS = 1800

#: ``pg_dump (PostgreSQL) 16.3`` — the standard shape of a client tool's ``--version``.
_PG_VERSION_PATTERN = re.compile(r"\(PostgreSQL\)\s+(\d+)")
#: Fallback for a vendor build that drops the parenthesised product name, and for the
#: server's own ``PostgreSQL 16.3``. The first integer is the major version in every
#: such string: neither ``pg_dump``, ``pg_restore``, nor ``PostgreSQL`` contains a
#: digit, so nothing precedes it that could be mistaken for one. Kept deliberately
#: loose so a beta build (``18beta1``) still yields ``18``.
_PG_VERSION_FALLBACK = re.compile(r"(\d+)")


class BackupError(RuntimeError):
    """A backup could not be produced or could not be verified.

    Its message is always sanitized: it names tools, paths the operator supplied, and
    exit statuses, never connection material.
    """


class Verification(StrEnum):
    """Whether the checks that were run passed.

    Deliberately *not* a statement about restorability. ``PASSED`` means "every check
    this tool performed succeeded"; which checks those were is
    :class:`VerificationLevel`, and whether a restore was attempted is
    ``BackupManifest.restore_tested`` — which is always ``False``, because no command
    in this repository performs a restore.
    """

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not-run"


class VerificationLevel(StrEnum):
    """*What* was checked. The honest scope of the automated guarantee."""

    NOT_RUN = "not-run"

    SQLITE_FULL_READ = "sqlite-full-read"
    """Every page and row was read.

    ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check`` walk the whole
    database, and every application table's row count is compared against the source.
    A truncated or corrupt artifact cannot pass this.
    """

    POSTGRESQL_ARCHIVE_TOC = "postgresql-archive-toc"
    """Only the archive header and table of contents were parsed.

    ``pg_restore --list`` reads the catalogue at the front of a custom-format archive
    and stops. It does **not** decompress the data blocks that follow, so an archive
    truncated after the TOC — a killed ``pg_dump``, a disk that filled mid-write, an
    interrupted file copy — still lists real tables and still passes. This level is
    recorded only when the stronger full read could not be attempted.
    """

    POSTGRESQL_ARCHIVE_FULL_READ = "postgresql-archive-full-read"
    """Every data block was decompressed and parsed.

    ``pg_restore --file=<scratch>`` converts the whole archive to a SQL script without
    connecting to any server, which forces a complete read. This catches truncation and
    corruption after the TOC.

    It still is not a restore: no server executed the statements, so it proves the
    archive is *readable in full*, not that it will load cleanly into a live database.
    Only the rehearsal in ``docs/operations/storage-backup-restore.md`` proves that.
    """


#: One sentence per level, for the manifest and for operator output. Keeping the prose
#: beside the enum is what stops the CLI and the document drifting apart.
VERIFICATION_DETAIL: dict[VerificationLevel, str] = {
    VerificationLevel.NOT_RUN: "no verification was performed",
    VerificationLevel.SQLITE_FULL_READ: (
        "SQLite integrity_check, foreign_key_check, and per-table row counts matched "
        "the source; every page was read"
    ),
    VerificationLevel.POSTGRESQL_ARCHIVE_TOC: (
        "the archive's table of contents parsed and is non-empty; data blocks after "
        "the TOC were NOT read, so truncation after the TOC would not be detected"
    ),
    VerificationLevel.POSTGRESQL_ARCHIVE_FULL_READ: (
        "pg_restore decompressed and parsed every data block without connecting to a "
        "server; no restore was executed"
    ),
}


def failed_verification_detail(level: VerificationLevel) -> str:
    """The detail sentence for a check that was attempted and *failed*.

    Every sentence in :data:`VERIFICATION_DETAIL` is written as accomplished fact —
    "decompressed and parsed every data block". Printing one of those beside a failure
    states the opposite of what happened: a real full read over a truncated archive
    stops partway through, so claiming every block was parsed is simply false. Observed
    against real ``pg_restore`` while validating issue #108; the offline suite had never
    reached this path, because its only failing-read case raises before a report is
    rendered.

    The trailing clause is level-aware. A full read is only *reached* after
    ``pg_restore --list`` parsed a non-empty table of contents, so denying that stage
    would contradict :attr:`VerificationLevel.POSTGRESQL_ARCHIVE_TOC` — the level this
    module records for precisely that much. Every other level is the first stage of its
    backend's check, with nothing established beneath it.
    """
    confirmed = (
        "its table of contents parsed, but nothing beyond that was confirmed"
        if level is VerificationLevel.POSTGRESQL_ARCHIVE_FULL_READ
        else "nothing about this artifact was confirmed"
    )
    return f"the {level.value} check was attempted and the artifact did not pass it; {confirmed}"


class BackupPlan(BaseModel):
    """The result of a preflight. Producing one writes nothing."""

    model_config = ConfigDict(frozen=True)

    backend: Backend
    destination: str
    artifact_name: str
    manifest_name: str
    tool: str | None = None
    alembic_revision: str | None = None
    table_counts: dict[str, int] = Field(default_factory=dict)
    estimated_source_bytes: int | None = None
    #: Client-tool and server major versions, when they could be determined. Plain
    #: integers; nothing here identifies a host, database, or role.
    pg_dump_major: int | None = None
    pg_restore_major: int | None = None
    server_major: int | None = None
    ready: bool
    blockers: tuple[str, ...] = ()

    def sanitized_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class BackupManifest(BaseModel):
    """The durable, verifiable description of one artifact."""

    model_config = ConfigDict(frozen=True)

    manifest_version: int = MANIFEST_VERSION
    created_at: datetime
    backend: Backend
    code_revision: str | None = None
    alembic_revision: str | None = None
    table_counts: dict[str, int] = Field(default_factory=dict)
    artifact_name: str
    artifact_bytes: int
    sha256: str
    verification: Verification
    verification_level: VerificationLevel = VerificationLevel.NOT_RUN
    verification_detail: str = VERIFICATION_DETAIL[VerificationLevel.NOT_RUN]
    restore_tested: bool = False
    """Always ``False``.

    No command in this repository restores an artifact into a database. The field is
    written explicitly, rather than left to be inferred, so a reader of the manifest
    cannot mistake ``verification: passed`` for "this backup is known to restore".
    Proving that is the operator rehearsal in
    ``docs/operations/storage-backup-restore.md``.
    """

    def sanitized_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class BackupOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest: BackupManifest
    destination: str
    manifest_name: str

    def sanitized_payload(self) -> dict[str, Any]:
        payload = self.manifest.sanitized_payload()
        payload["destination"] = self.destination
        payload["manifest_name"] = self.manifest_name
        return payload


# --------------------------------------------------------------------------------
# Destination validation
# --------------------------------------------------------------------------------


def _contains(ancestor: Path, candidate: Path) -> bool:
    """True when ``candidate`` *is* ``ancestor`` or lives anywhere beneath it.

    Both arguments must already be canonically resolved. ``Path.is_relative_to`` is the
    right primitive rather than a string prefix comparison, and deliberately so:

    - it compares parsed path *components*, so ``C:\\repo-backups`` is not treated as
      living inside ``C:\\repo`` the way ``str.startswith`` would;
    - on Windows the comparison is case- and separator-insensitive, so ``C:/REPO/Data``
      and ``C:\\repo\\data`` both match ``C:\\repo``;
    - across drives it returns ``False`` instead of raising, so ``D:\\backups`` against
      a ``C:\\`` repository is simply "not contained".
    """
    return candidate == ancestor or candidate.is_relative_to(ancestor)


def resolve_destination(raw: str | Path, *, repo_root: Path | None = None) -> Path:
    """Validate a destination *directory*, or raise :class:`BackupError`.

    Broad or ambiguous destinations are rejected rather than interpreted. The failure
    mode this prevents is an operator writing a multi-hundred-megabyte artifact into
    a filesystem root, their home directory, or the repository working tree — each of
    which is either unsafe, gets committed by accident, or is impossible to clean up
    without risking something else.

    Containment is checked against *canonically resolved* paths, so a destination that
    reaches the repository through ``..``, a symlink, or a Windows junction is rejected
    on where it actually lands rather than on how it was spelled.
    """
    text = str(raw)
    if not text.strip():
        raise BackupError("a destination directory is required")
    # An unexpanded ``$VAR`` or ``%VAR%`` means the caller's shell did not substitute
    # it. Writing to a literal directory named ``$BACKUP_DIR`` is never the intent.
    if "$" in text or "%" in text:
        raise BackupError("the destination contains an unresolved environment variable")

    candidate = Path(text)
    if ".." in candidate.parts:
        raise BackupError("the destination must not contain '..' path traversal")
    if not candidate.is_absolute():
        raise BackupError("the destination must be an absolute path, not a relative one")

    resolved = candidate.resolve()
    if resolved == Path(resolved.anchor):
        raise BackupError("the destination must not be a filesystem or drive root")

    home = Path.home().resolve()
    if resolved == home:
        raise BackupError("the destination must not be the user's home directory root")
    if home != Path(home.anchor) and home.is_relative_to(resolved):
        raise BackupError("the destination must not be an ancestor of the user's home directory")

    root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
    # Anywhere inside the working tree is refused, not just the root itself. An artifact
    # written to a tracked path — or to a gitignored one that a later `git add -A` picks
    # up anyway — puts the entire contents of the shared database into version-control
    # history. This module cannot rely on `.gitignore` to prevent that, so it refuses the
    # whole subtree.
    if _contains(root, resolved):
        raise BackupError(
            "the destination must not be the repository working tree or any directory "
            "inside it; choose a location outside the checkout"
        )
    if _contains(resolved, root):
        raise BackupError("the destination must not be an ancestor of the repository root")

    if not resolved.exists():
        # Creating a deep tree from a typo'd path is exactly the ambiguity this
        # command must not resolve on the operator's behalf.
        raise BackupError("the destination directory does not exist; create it deliberately first")
    if not resolved.is_dir():
        raise BackupError("the destination must be a directory, not a file")
    return resolved


def artifact_names(backend: Backend, *, now: datetime) -> tuple[str, str]:
    """``(artifact, manifest)`` filenames for this backend and instant."""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = "sqlite3" if backend is Backend.SQLITE else "dump"
    artifact = f"schwab-storage-{backend.value}-{stamp}.{suffix}"
    return artifact, f"{artifact}.manifest.json"


# --------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _code_revision(root: Path) -> str | None:
    """The reviewed Git revision, or ``None``. A dirty tree is recorded, not refused.

    A backup taken from a dirty checkout is still a valid backup; hiding that fact in
    the manifest is what would be wrong.
    """
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
        return None
    if len(revision) != 40:
        return None
    return f"{revision}-dirty" if dirty else revision


def _source_database(settings: Settings) -> tuple[Database, Backend]:
    raw = settings.database_url.get_secret_value().strip()
    backend = backend_of(raw)
    if backend is Backend.LOCAL_SQLITE_FILES:
        raise BackupError(
            "no shared database is configured; SCHWAB_DATABASE_URL must name the "
            "storage to back up"
        )
    if backend is Backend.UNKNOWN:
        raise BackupError("the configured database backend is not supported for backup")
    database = factory.database(settings)
    if database is None:  # pragma: no cover - guarded by the backend check above
        raise BackupError("the configured database could not be opened")
    return database, backend


def _revision_of(database: Database, root: Path) -> str | None:
    current, _expected, _error = alembic_revisions(database, root)
    return ",".join(current) if current else None


# --------------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------------


def plan_backup(
    settings: Settings,
    destination: str | Path,
    *,
    now: datetime | None = None,
    repo_root: Path | None = None,
) -> BackupPlan:
    """Check everything an execution needs, and write nothing.

    This is the default mode. It reports the exact artifact it *would* create so an
    operator can confirm the destination before anything lands on disk.
    """
    stamp = now or datetime.now(UTC)
    root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
    database, backend = _source_database(settings)
    resolved = resolve_destination(destination, repo_root=root)
    artifact, manifest = artifact_names(backend, now=stamp)

    blockers: list[str] = []
    tool: str | None = None
    source_bytes: int | None = None
    dump_major: int | None = None
    restore_major: int | None = None
    server_major: int | None = None

    if backend is Backend.SQLITE:
        path = sqlite_path(settings.database_url.get_secret_value().strip())
        if path is None or not path.is_file():
            blockers.append("the configured SQLite database file does not exist")
        else:
            source_bytes = path.stat().st_size
    else:
        tool = shutil.which("pg_dump")
        if tool is None:
            blockers.append(
                "pg_dump was not found on PATH; install the PostgreSQL client tools "
                "matching the server major version, then re-run the preflight"
            )
        if shutil.which("pg_restore") is None:
            blockers.append(
                "pg_restore was not found on PATH; it is required to verify the "
                "artifact, and an unverified artifact is treated as a failed backup"
            )
        if not blockers:
            # Only worth asking once both tools exist; ``--version`` opens no connection.
            dump_major = pg_tool_major_version("pg_dump")
            restore_major = pg_tool_major_version("pg_restore")
            server_major = _server_major_version(database)
            blockers.extend(
                pg_version_blockers(
                    dump_major=dump_major,
                    restore_major=restore_major,
                    server_major=server_major,
                )
            )

    if (resolved / artifact).exists() or (resolved / manifest).exists():
        blockers.append("an artifact with the planned timestamped name already exists")

    try:
        present = present_tables(database)
        counts = table_counts(database, present)
        revision = _revision_of(database, root)
    except Exception as exc:
        counts = {}
        revision = None
        blockers.append(f"the source database could not be read ({type(exc).__name__})")

    return BackupPlan(
        backend=backend,
        destination=str(resolved),
        artifact_name=artifact,
        manifest_name=manifest,
        # `stem`, not `name`: on Windows `shutil.which` resolves through PATHEXT and
        # returns `pg_dump.EXE`, which rendered as a shouty `Tool: pg_dump.EXE` beside a
        # `pg_dump 18` version row. The stem reads the same on every platform.
        tool=Path(tool).stem if tool else None,
        alembic_revision=revision,
        table_counts=counts,
        estimated_source_bytes=source_bytes,
        pg_dump_major=dump_major,
        pg_restore_major=restore_major,
        server_major=server_major,
        ready=not blockers,
        blockers=tuple(blockers),
    )


# --------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------


def execute_backup(
    settings: Settings,
    destination: str | Path,
    *,
    now: datetime | None = None,
    repo_root: Path | None = None,
) -> BackupOutcome:
    """Produce and verify one artifact, or raise :class:`BackupError`."""
    stamp = now or datetime.now(UTC)
    root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
    plan = plan_backup(settings, destination, now=stamp, repo_root=root)
    if not plan.ready:
        raise BackupError("; ".join(plan.blockers))

    resolved = Path(plan.destination)
    artifact_path = resolved / plan.artifact_name
    manifest_path = resolved / plan.manifest_name
    backend = plan.backend

    if backend is Backend.SQLITE:
        _sqlite_online_backup(settings, artifact_path)
        ok = _verify_sqlite_artifact(artifact_path, expected_counts=plan.table_counts)
        result = PgVerification(
            ok,
            VerificationLevel.SQLITE_FULL_READ,
            None if ok else "integrity, foreign-key, or row-count checks did not match",
        )
    else:
        _pg_dump(settings, artifact_path)
        result = _verify_pg_artifact(artifact_path)

    if not result.ok:
        _reject(artifact_path)
        raise BackupError(
            f"the produced artifact failed verification and was renamed to "
            f"{artifact_path.name}.rejected; no manifest was written"
            + (f". {result.reason}" if result.reason else "")
        )

    manifest = BackupManifest(
        created_at=stamp.astimezone(UTC),
        backend=backend,
        code_revision=_code_revision(root),
        alembic_revision=plan.alembic_revision,
        table_counts=plan.table_counts,
        artifact_name=artifact_path.name,
        artifact_bytes=artifact_path.stat().st_size,
        sha256=file_sha256(artifact_path),
        verification=Verification.PASSED,
        verification_level=result.level,
        # When the full read had to be skipped, ``reason`` says why; that belongs in the
        # manifest rather than only in a terminal an operator has since closed.
        verification_detail=(
            VERIFICATION_DETAIL[result.level]
            + (f" ({result.reason})" if result.reason else "")
        ),
        restore_tested=False,
    )
    manifest_path.write_text(
        json.dumps(manifest.sanitized_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return BackupOutcome(
        manifest=manifest,
        destination=str(resolved),
        manifest_name=manifest_path.name,
    )


def _reject(artifact_path: Path) -> None:
    """Rename a bad artifact aside. Never deletes; a human decides what to remove."""
    # A failed rename is not itself a failure: leaving the artifact in place with no
    # manifest is still unambiguous, because this module treats "manifest absent" as
    # "not a usable backup".
    with contextlib.suppress(OSError):
        artifact_path.replace(artifact_path.with_name(artifact_path.name + ".rejected"))


def _sqlite_online_backup(settings: Settings, artifact_path: Path) -> None:
    """Copy a live SQLite database with the online backup API.

    Not a file copy: a plain copy of an actively written database yields a torn page
    image that ``PRAGMA integrity_check`` may still pass while the WAL tail is lost.
    The backup API takes a consistent snapshot and restarts itself if the source is
    written during the copy.
    """
    source_path = sqlite_path(settings.database_url.get_secret_value().strip())
    if source_path is None:  # pragma: no cover - guarded by the preflight
        raise BackupError("the configured SQLite database has no file to back up")

    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        # Read-only URI: the source is never modified, not even its WAL, by this path.
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(artifact_path, timeout=30)
        source.backup(target, pages=512)
    except sqlite3.Error as exc:
        raise BackupError(f"the SQLite online backup failed ({type(exc).__name__})") from exc
    finally:
        for connection in (target, source):
            if connection is not None:
                connection.close()


def _verify_sqlite_artifact(artifact_path: Path, *, expected_counts: dict[str, int]) -> bool:
    """Integrity-check the artifact and confirm it holds the rows we counted."""
    if not artifact_path.is_file() or artifact_path.stat().st_size == 0:
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{artifact_path.as_posix()}?mode=ro", uri=True, timeout=30
        )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            return False
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            return False
        for table, expected in expected_counts.items():
            row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            if row is None or int(row[0]) != expected:
                return False
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()
    return True


def libpq_environment(raw_url: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child environment carrying connection material for ``pg_dump``.

    This is the whole reason no credential appears in an argument vector: libpq reads
    ``PGHOST``/``PGUSER``/``PGPASSWORD``/… from the environment, so the command line
    stays free of anything sensitive. The returned mapping is passed straight to
    :func:`subprocess.run` and is never logged, echoed, or stored.
    """
    url = make_url(raw_url)
    environment = dict(base if base is not None else os.environ)
    if url.host:
        environment["PGHOST"] = url.host
    if url.port:
        environment["PGPORT"] = str(url.port)
    if url.username:
        environment["PGUSER"] = url.username
    if url.password:
        environment["PGPASSWORD"] = url.password
    if url.database:
        environment["PGDATABASE"] = url.database
    sslmode = url.query.get("sslmode")
    if isinstance(sslmode, tuple):
        sslmode = sslmode[-1] if sslmode else None
    if sslmode:
        environment["PGSSLMODE"] = str(sslmode)
    # Never let libpq stop for an interactive password prompt inside a scheduled task.
    environment["PGCONNECT_TIMEOUT"] = environment.get("PGCONNECT_TIMEOUT", "30")
    return environment


def _run_pg_tool(
    tool: str, arguments: list[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """Run a PostgreSQL client tool without ever surfacing its output.

    ``pg_dump`` writes ``connection to server at "HOST" (ADDR), port PORT failed`` to
    stderr. Capturing it and dropping it — rather than letting it reach a terminal,
    a log, or an exception message — is what keeps the host out of the record.
    """
    executable = shutil.which(tool)
    if executable is None:
        raise BackupError(
            f"{tool} was not found on PATH; install the PostgreSQL client tools "
            f"matching the server major version and re-run the command"
        )
    try:
        # Fixed argument vector, no shell, and nothing sensitive in ``arguments``.
        return subprocess.run(
            [executable, *arguments],
            capture_output=True,
            env=environment,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"{tool} exceeded the {SUBPROCESS_TIMEOUT_SECONDS}s timeout and was stopped"
        ) from exc
    except OSError as exc:
        raise BackupError(f"{tool} could not be started ({type(exc).__name__})") from exc


def _pg_dump(settings: Settings, artifact_path: Path) -> None:
    """Take a consistent custom-format dump. No connection material in ``argv``."""
    environment = libpq_environment(settings.database_url.get_secret_value().strip())
    completed = _run_pg_tool(
        "pg_dump",
        [
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--no-password",
            "--encoding=UTF8",
            f"--file={artifact_path}",
        ],
        environment,
    )
    if completed.returncode != 0:
        _reject(artifact_path)
        raise BackupError(
            f"pg_dump exited with status {completed.returncode}; its output is "
            "suppressed because it can contain connection details. Check the server "
            "and the locally configured role, then re-run the preflight."
        )


class PgVerification(NamedTuple):
    """Outcome of checking a custom-format archive, and how far the check got."""

    ok: bool
    level: VerificationLevel
    reason: str | None = None


def _verify_pg_artifact(artifact_path: Path) -> PgVerification:
    """Check a custom-format dump as thoroughly as is possible without a server.

    Two stages, because they prove different things:

    1. ``pg_restore --list`` parses the archive header and table of contents. Cheap,
       and enough to reject a file that is not an archive at all — but it stops at the
       TOC, so an archive truncated *after* the catalogue still passes.
    2. ``pg_restore --file=<scratch>`` converts the whole archive to a SQL script. This
       decompresses and parses every data block, which is what actually catches
       truncation and corruption past the TOC. It connects to nothing.

    Neither stage is a restore. The strongest claim available here is "the archive is
    readable in full", which is what :class:`VerificationLevel` records.
    """
    if not artifact_path.is_file() or artifact_path.stat().st_size == 0:
        return PgVerification(False, VerificationLevel.NOT_RUN, "the artifact is missing or empty")

    listed = _run_pg_tool("pg_restore", ["--list", str(artifact_path)], dict(os.environ))
    if listed.returncode != 0:
        return PgVerification(
            False,
            VerificationLevel.POSTGRESQL_ARCHIVE_TOC,
            f"pg_restore --list exited with status {listed.returncode}",
        )
    # An archive whose table of contents is empty restored nothing and is not a backup.
    if not any(
        line.strip() and not line.lstrip().startswith(";")
        for line in listed.stdout.decode("utf-8", errors="replace").splitlines()
    ):
        return PgVerification(
            False,
            VerificationLevel.POSTGRESQL_ARCHIVE_TOC,
            "the archive's table of contents is empty",
        )

    # The scratch script is written beside the artifact: that directory is
    # operator-chosen, already holds a file of comparable size, and is known writable.
    # It is removed unconditionally — a verification step must not leave a second copy
    # of the database lying around.
    try:
        with tempfile.TemporaryDirectory(
            prefix="schwab-pg-verify-", dir=artifact_path.parent
        ) as scratch:
            converted = _run_pg_tool(
                "pg_restore",
                ["--file", str(Path(scratch) / "archive.sql"), str(artifact_path)],
                dict(os.environ),
            )
    except OSError as exc:
        # Could not create the scratch area at all. The archive is not implicated, so
        # report the weaker level truthfully instead of condemning a possibly good file.
        return PgVerification(
            True,
            VerificationLevel.POSTGRESQL_ARCHIVE_TOC,
            f"the full-read check could not create a scratch directory ({type(exc).__name__})",
        )

    if converted.returncode != 0:
        return PgVerification(
            False,
            VerificationLevel.POSTGRESQL_ARCHIVE_FULL_READ,
            (
                f"pg_restore could not read the whole archive (status "
                f"{converted.returncode}); its output is suppressed because it can "
                "contain connection details. The archive is most likely truncated or "
                "corrupt after its table of contents; a full disk on the destination "
                "would also produce this."
            ),
        )
    return PgVerification(True, VerificationLevel.POSTGRESQL_ARCHIVE_FULL_READ)


def pg_tool_major_version(tool: str) -> int | None:
    """The major version a PostgreSQL client tool reports, or ``None``.

    ``--version`` never opens a connection, so this is safe to call from a preflight.
    Only the integer is kept; the tool's raw output is discarded so nothing from it can
    reach a report.
    """
    if shutil.which(tool) is None:
        return None
    try:
        completed = _run_pg_tool(tool, ["--version"], dict(os.environ))
    except BackupError:
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.decode("utf-8", errors="replace").strip()
    match = _PG_VERSION_PATTERN.search(text) or _PG_VERSION_FALLBACK.search(text)
    return int(match.group(1)) if match else None


def _server_major_version(database: Database) -> int | None:
    """The server's major version, or ``None`` when it cannot be read.

    Derived from the same sanitized ``SHOW server_version`` probe the health command
    uses, so no new connection behavior is introduced and no URL is touched.
    """
    try:
        facts = gather_server_facts(database)
    except Exception:
        return None
    if not facts.connected or not facts.server_version:
        return None
    match = _PG_VERSION_FALLBACK.search(facts.server_version)
    return int(match.group(1)) if match else None


def pg_version_blockers(
    *, dump_major: int | None, restore_major: int | None, server_major: int | None
) -> tuple[str, ...]:
    """Sanitized, actionable blockers for a client/server version mismatch.

    A mismatch is one of the most common causes of a dump or restore failing, and
    without this it surfaces only as an opaque ``pg_dump exited with status N`` — the
    output that would explain it is deliberately suppressed. Checking versions up front
    is the one place the diagnosis can be given plainly.

    Every message contains tool names and integers only.
    """
    blockers: list[str] = []
    if dump_major is None:
        blockers.append(
            "pg_dump did not report a usable version; confirm the PostgreSQL client "
            "tools are installed and runnable before backing up"
        )
    if restore_major is None:
        blockers.append(
            "pg_restore did not report a usable version; confirm the PostgreSQL client "
            "tools are installed and runnable before backing up"
        )
    if dump_major is not None and restore_major is not None and dump_major != restore_major:
        blockers.append(
            f"pg_dump is major version {dump_major} but pg_restore is {restore_major}; "
            "they must come from the same client installation, because pg_restore "
            "verifies what pg_dump writes"
        )
    if server_major is not None and dump_major is not None and dump_major < server_major:
        # PostgreSQL supports dumping an older server with a newer pg_dump, never the
        # reverse: an older pg_dump does not know the newer server's catalogue.
        blockers.append(
            f"pg_dump is major version {dump_major} but the server is {server_major}; "
            "install client tools at least as new as the server"
        )
    return tuple(blockers)


# --------------------------------------------------------------------------------
# Independent artifact verification
# --------------------------------------------------------------------------------


class ArtifactVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_name: str
    manifest_name: str
    result: Verification
    verification_level: VerificationLevel = VerificationLevel.NOT_RUN
    verification_detail: str = VERIFICATION_DETAIL[VerificationLevel.NOT_RUN]
    restore_tested: bool = False
    """Always ``False``; this command reads an artifact, it never restores one."""

    reasons: tuple[str, ...] = ()
    sha256_expected: str | None = None
    sha256_actual: str | None = None

    def sanitized_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def verify_artifact(manifest_path: str | Path) -> ArtifactVerification:
    """Re-verify an existing artifact against its manifest. Read-only.

    Used by the restore rehearsal: before an artifact is trusted enough to restore
    into a scratch database, its size, hash, and structure must still match what the
    manifest recorded.
    """
    manifest_file = Path(manifest_path).resolve()
    reasons: list[str] = []
    if not manifest_file.is_file():
        return ArtifactVerification(
            artifact_name="",
            manifest_name=manifest_file.name,
            result=Verification.FAILED,
            reasons=("the manifest does not exist",),
        )
    try:
        manifest = BackupManifest.model_validate_json(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return ArtifactVerification(
            artifact_name="",
            manifest_name=manifest_file.name,
            result=Verification.FAILED,
            reasons=(f"the manifest could not be parsed ({type(exc).__name__})",),
        )

    artifact_path = manifest_file.parent / manifest.artifact_name
    if not artifact_path.is_file():
        return ArtifactVerification(
            artifact_name=manifest.artifact_name,
            manifest_name=manifest_file.name,
            result=Verification.FAILED,
            reasons=("the artifact named by the manifest does not exist",),
            sha256_expected=manifest.sha256,
        )

    actual_bytes = artifact_path.stat().st_size
    if actual_bytes != manifest.artifact_bytes:
        reasons.append(
            f"size mismatch: manifest {manifest.artifact_bytes} bytes, artifact {actual_bytes}"
        )
    actual_hash = file_sha256(artifact_path)
    if actual_hash != manifest.sha256:
        reasons.append("SHA-256 mismatch: the artifact is not the one the manifest describes")

    level = VerificationLevel.NOT_RUN
    detail = VERIFICATION_DETAIL[level]
    if not reasons:
        if manifest.backend is Backend.SQLITE:
            ok = _verify_sqlite_artifact(artifact_path, expected_counts=manifest.table_counts)
            checked = PgVerification(ok, VerificationLevel.SQLITE_FULL_READ)
        else:
            checked = _verify_pg_artifact(artifact_path)
        level = checked.level
        # A failed check must not be described with the sentence that describes a pass.
        # NOT_RUN is the exception: nothing was attempted, so "no verification was
        # performed" is already the honest sentence, and the failed-check phrasing would
        # claim the not-run check ran.
        if checked.ok:
            # Mirror ``execute_backup``: when the full read had to be skipped, ``reason``
            # says why. Dropping it here left this report and the manifest describing the
            # very same artifact disagreeing about why the weaker level was recorded.
            detail = VERIFICATION_DETAIL[level] + (
                f" ({checked.reason})" if checked.reason else ""
            )
        elif level is VerificationLevel.NOT_RUN:
            detail = VERIFICATION_DETAIL[level]
        else:
            detail = failed_verification_detail(level)
        if not checked.ok:
            reasons.append(
                "the artifact did not pass its backend's check"
                + (f": {checked.reason}" if checked.reason else "")
            )

    return ArtifactVerification(
        artifact_name=manifest.artifact_name,
        manifest_name=manifest_file.name,
        result=Verification.FAILED if reasons else Verification.PASSED,
        verification_level=level,
        verification_detail=detail,
        restore_tested=False,
        reasons=tuple(reasons),
        sha256_expected=manifest.sha256,
        sha256_actual=actual_hash,
    )


__all__ = [
    "MANIFEST_VERSION",
    "SUBPROCESS_TIMEOUT_SECONDS",
    "VERIFICATION_DETAIL",
    "ArtifactVerification",
    "BackupError",
    "BackupManifest",
    "BackupOutcome",
    "BackupPlan",
    "PgVerification",
    "Verification",
    "VerificationLevel",
    "artifact_names",
    "execute_backup",
    "failed_verification_detail",
    "file_sha256",
    "libpq_environment",
    "pg_tool_major_version",
    "pg_version_blockers",
    "plan_backup",
    "resolve_destination",
    "verify_artifact",
]
