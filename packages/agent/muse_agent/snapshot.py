import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

SQLITE_SIDE_FILES = ("", "-journal", "-wal", "-shm")
DRIVE = re.compile(r"^[A-Za-z]:/")


class SnapshotError(Exception):
    pass


@dataclass(frozen=True)
class DiskFile:
    relative: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class SnapshotStats:
    rows: int
    rewritten: int
    missing: int


def make_snapshot(catalog_db: Path, library_dir: Path, out: Path) -> SnapshotStats:
    temp = out.with_name(f"{out.name}.tmp")
    discard(temp)
    try:
        with closing(sqlite3.connect(temp)) as copy:
            copy_live(catalog_db, copy)
            mode = copy.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode != "delete":
                raise SnapshotError(f"journal mode stayed {mode}")
            stats = relocate_tracks(copy, library_dir, disk_files(library_dir))
            copy.commit()
            verdict = copy.execute("PRAGMA integrity_check").fetchall()
        if verdict != [("ok",)]:
            raise SnapshotError(f"integrity check failed: {verdict[:3]}")
        temp.replace(out)
    except BaseException:
        discard(temp)
        raise
    return stats


def copy_live(catalog_db: Path, copy: sqlite3.Connection) -> None:
    uri = f"{catalog_db.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as live:
        live.backup(copy)


def disk_files(library_dir: Path) -> dict[str, DiskFile]:
    files: dict[str, DiskFile] = {}
    pending = [("", library_dir)]
    while pending:
        prefix, folder = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                relative = f"{prefix}{entry.name}"
                if entry.is_dir(follow_symlinks=False):
                    pending.append((f"{relative}/", Path(entry.path)))
                elif entry.is_file():
                    seen = entry.stat()
                    files[relative.casefold()] = DiskFile(relative, seen.st_size, seen.st_mtime_ns)
    return files


def relocate_tracks(
    db: sqlite3.Connection, library_dir: Path, on_disk: dict[str, DiskFile]
) -> SnapshotStats:
    root = library_dir.as_posix().rstrip("/") + "/"
    db.execute("ALTER TABLE tracks ADD COLUMN size INTEGER")
    db.execute("ALTER TABLE tracks ADD COLUMN mtime_ns INTEGER")
    query = "SELECT id, path, status FROM tracks WHERE path IS NOT NULL"
    rows = db.execute(query).fetchall()
    changes: list[tuple[str | None, int | None, int | None, int]] = []
    missing = 0
    rewritten = 0
    for track_id, stored, status in rows:
        relative = library_relative(stored, root)
        found = on_disk.get(relative.casefold()) if relative is not None else None
        if found is None and status == "ok":
            missing += 1
        relocated = found.relative if found else relative
        if relocated != stored:
            rewritten += 1
        if relocated != stored or found:
            changes.append(
                (
                    relocated,
                    found.size if found else None,
                    found.mtime_ns if found else None,
                    track_id,
                )
            )
    db.executemany("UPDATE tracks SET path = ?, size = ?, mtime_ns = ? WHERE id = ?", changes)
    return SnapshotStats(rows=len(rows), rewritten=rewritten, missing=missing)


def library_relative(stored: str, root: str) -> str | None:
    path = stored.replace("\\", "/")
    if path[: len(root)].casefold() == root.casefold():
        return path[len(root) :]
    if path.startswith("/") or DRIVE.match(path):
        return None
    return path


def discard(database: Path) -> None:
    for suffix in SQLITE_SIDE_FILES:
        database.with_name(database.name + suffix).unlink(missing_ok=True)
