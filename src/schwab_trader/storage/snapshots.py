"""Immutable SQLite-backup snapshots for reviewed migration execution."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from schwab_trader.storage.inventory import (
    InventoryReport,
    SourceInventory,
    SourceType,
    _manifest_inventory,
    _sqlite_inventory,
)


class SnapshotSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    relative_path: str
    kind: str
    source_hash: str
    snapshot_hash: str
    content_hash: str
    schema_version: int | None
    table_counts: dict[str, int]
    table_checksums: dict[str, str]


class SnapshotSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    migration_id: str
    source_set_hash: str
    created_at: datetime
    writers_stopped_asserted: bool
    sources: tuple[SnapshotSource, ...]


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    """Use SQLite's online backup API and publish only a complete copy."""
    partial = destination.with_name(f".{destination.name}.partial")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if partial.exists():
        # A partial lives only inside the exact migration backup directory. It is
        # never a source and is safe to replace during interrupted-copy recovery.
        partial.unlink()
    source_conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    destination_conn = sqlite3.connect(partial)
    try:
        source_conn.execute("PRAGMA query_only = ON")
        source_conn.backup(destination_conn)
        destination_conn.commit()
        check = destination_conn.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise RuntimeError("SQLite backup failed its integrity check")
    finally:
        destination_conn.close()
        source_conn.close()
    with contextlib.suppress(OSError):
        os.chmod(partial, 0o600)
    os.replace(partial, destination)


def _verify_existing_snapshot(
    destination: Path,
    snapshot_root: Path,
    source: SourceInventory,
) -> str:
    if source.source_type is SourceType.SQLITE:
        observed = _sqlite_inventory(destination, snapshot_root, source.kind)
    else:
        observed = _manifest_inventory(destination, snapshot_root)
    if observed.content_hash != source.content_hash:
        raise RuntimeError(
            f"existing backup for {source.relative_path} conflicts with source inventory"
        )
    return observed.source_hash


def create_snapshot_set(
    inventory: InventoryReport,
    backup_dir: Path,
    *,
    writers_stopped: bool,
) -> tuple[Path, SnapshotSet]:
    """Create or resume immutable snapshots without changing source files."""
    if not writers_stopped:
        raise ValueError(
            "final snapshot requires confirmation that cohort runners and paper writers are stopped"
        )
    migration_id = inventory.source_set_hash
    snapshot_root = backup_dir.resolve() / migration_id
    sources_root = snapshot_root / "sources"
    sources_root.mkdir(parents=True, exist_ok=True)
    snapshot_sources: list[SnapshotSource] = []
    source_root = Path(inventory.source_root)

    for source in inventory.eligible_sources:
        original = source_root / Path(source.relative_path)
        destination = sources_root / Path(source.relative_path)
        if destination.exists():
            snapshot_hash = _verify_existing_snapshot(destination, sources_root, source)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.source_type is SourceType.SQLITE:
                _snapshot_sqlite(original, destination)
            else:
                shutil.copy2(original, destination)
                with contextlib.suppress(OSError):
                    os.chmod(destination, 0o600)
            snapshot_hash = _verify_existing_snapshot(destination, sources_root, source)
        snapshot_sources.append(
            SnapshotSource(
                relative_path=source.relative_path,
                kind=source.kind,
                source_hash=source.source_hash,
                snapshot_hash=snapshot_hash,
                content_hash=source.content_hash,
                schema_version=source.schema_version,
                table_counts=source.table_counts,
                table_checksums=source.table_checksums,
            )
        )

    snapshot_set = SnapshotSet(
        migration_id=migration_id,
        source_set_hash=inventory.source_set_hash,
        created_at=datetime.now(UTC),
        writers_stopped_asserted=True,
        sources=tuple(snapshot_sources),
    )
    _atomic_json(snapshot_root / "inventory.json", inventory.sanitized_payload())
    _atomic_json(
        snapshot_root / "snapshot.json",
        snapshot_set.model_dump(mode="json"),
    )
    return snapshot_root, snapshot_set


def load_snapshot_set(snapshot_root: Path) -> SnapshotSet:
    raw = (snapshot_root / "snapshot.json").read_text(encoding="utf-8")
    return SnapshotSet.model_validate_json(raw)


def verify_snapshot_set(snapshot_root: Path, snapshot: SnapshotSet) -> None:
    """Re-hash immutable backup copies before import or verification-only reruns."""
    sources_root = (snapshot_root / "sources").resolve()
    for source in snapshot.sources:
        path = (sources_root / Path(source.relative_path)).resolve()
        if not path.is_relative_to(sources_root) or not path.is_file():
            raise RuntimeError("migration snapshot source is missing or outside its root")
        if source.kind == "cohort-manifest":
            observed = _manifest_inventory(path, sources_root)
        else:
            observed = _sqlite_inventory(path, sources_root, source.kind)
        if (
            observed.source_hash != source.snapshot_hash
            or observed.content_hash != source.content_hash
            or observed.schema_version != source.schema_version
            or observed.table_counts != source.table_counts
            or observed.table_checksums != source.table_checksums
        ):
            raise RuntimeError(
                f"migration snapshot integrity check failed for {source.relative_path}"
            )
