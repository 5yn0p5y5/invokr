"""
SQLite persistence.

Three tables:

* ``tracks``  — one row per catalogue track we know about. The catalogue key is
  the primary key, which is what makes "already downloaded" cheap and reliable.
  (The column is still called ``spotify_id`` for historical reasons.)
  We deliberately do not rely on spotDL's ``--overwrite skip`` for dedupe: it
  reports success when it skips a file, and it cannot tell us that it skipped.
* ``jobs``    — one row per download attempt, so a failed job is inspectable.
* ``events``  — an append-only log. Un-likes land here (we log them, never act
  on them) as do server lifecycle events and download outcomes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    spotify_id  TEXT PRIMARY KEY,
    video_id    TEXT NOT NULL,
    yt_url      TEXT NOT NULL,
    title       TEXT,
    artists     TEXT,
    album       TEXT,
    duration_s  INTEGER,
    status      TEXT NOT NULL,
    path        TEXT,
    meta_json   TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_video_id ON tracks (video_id);
CREATE INDEX IF NOT EXISTS idx_tracks_status   ON tracks (status);

CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    spotify_id  TEXT NOT NULL,
    video_id    TEXT NOT NULL,
    yt_url      TEXT NOT NULL,
    spotify_url TEXT NOT NULL,
    status      TEXT NOT NULL,
    path        TEXT,
    error       TEXT,
    meta_json   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    video_id   TEXT,
    spotify_id TEXT,
    detail     TEXT,
    at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_at ON events (at);
"""

_db_path: Path | None = None
# The library root that stored paths are kept relative to. None means this
# install has no master folder, and paths are stored exactly as given.
_root: Path | None = None
# WAL is right on a local disk and wrong on a network share: SMB and NFS do not
# provide the locking and shared-memory semantics WAL assumes, and a lock that
# silently does nothing can corrupt the database. On a network path we fall back
# to the rollback journal.
_journal_mode: str = "WAL"
_on_network: bool = False

# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# alter an existing table, so additive changes need an explicit migration.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("tracks", "meta_json", "TEXT"),
    ("jobs", "meta_json", "TEXT"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, declaration in _MIGRATIONS:
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def configure(path: Path, *, library_root: Path | None = None) -> None:
    """
    Point the module at a database file and create or migrate the schema.

    ``library_root`` is the folder that stored track paths are kept relative to,
    which is what lets the whole archive move to another drive or machine
    without rewriting every row.
    """
    global _db_path, _root, _journal_mode, _on_network
    _db_path = Path(path)
    _root = Path(library_root) if library_root else None
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _on_network = is_network_path(_db_path)
    _journal_mode = "DELETE" if _on_network else "WAL"
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Short-lived connection. SQLite in WAL mode handles our concurrency fine."""
    if _db_path is None:
        raise RuntimeError("db.configure() has not been called")

    conn = sqlite3.connect(_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA journal_mode={_journal_mode}")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def journal_mode() -> str:
    """The journal actually in use — reported by preflight and /api/config."""
    return _journal_mode


def on_network_path() -> bool:
    return _on_network


def is_network_path(path: Path) -> bool:
    """
    Is this path on a network share?

    On Windows a mapped drive reports DRIVE_REMOTE even while the share is
    disconnected, so an unreachable ``Z:`` is still recognised. Elsewhere,
    ``/proc/mounts`` names the filesystem type.
    """
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True

    if os.name == "nt":
        drive = os.path.splitdrive(text)[0]
        if not drive:
            return False
        try:
            import ctypes

            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4
        except Exception:
            return False

    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    network_fs = {"nfs", "nfs4", "cifs", "smbfs", "smb3", "afp", "fuse.sshfs", "9p"}
    target = Path(text).resolve()
    best_len, best_fs = -1, ""
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point = Path(parts[1].replace("\\040", " "))
        if target == mount_point or mount_point in target.parents:
            if len(str(mount_point)) > best_len:
                best_len, best_fs = len(str(mount_point)), parts[2]

    return best_fs in network_fs


# ------------------------------------------------------- stored track paths ---
#
# Paths are persisted relative to the library root when they sit inside it, so
# copying the master folder to another drive or machine keeps the index valid.
# A file that lives outside the library stays absolute, which is the only thing
# that could sensibly be meant by it.


def _to_stored(path: str | None) -> str | None:
    """Filesystem path -> what we persist."""
    if not path or _root is None:
        return path
    try:
        return str(Path(path).resolve().relative_to(_root))
    except ValueError:
        return str(path)


def _to_absolute(stored: str | None) -> str | None:
    """What we persist -> a path usable on this machine."""
    if not stored:
        return stored
    candidate = Path(stored)
    if candidate.is_absolute() or _root is None:
        return str(candidate)
    return str(_root / candidate)


def _row(row) -> dict:
    data = dict(row)
    if "path" in data:
        data["path"] = _to_absolute(data.get("path"))
    return data


def _rows(rows) -> list[dict]:
    return [_row(row) for row in rows]


# --------------------------------------------------------------------- events


def record_event(
    kind: str,
    *,
    video_id: str | None = None,
    spotify_id: str | None = None,
    detail: Any = None,
) -> None:
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, default=str)

    with connect() as conn:
        conn.execute(
            "INSERT INTO events (kind, video_id, spotify_id, detail, at)"
            " VALUES (?, ?, ?, ?, ?)",
            (kind, video_id, spotify_id, detail, utcnow()),
        )


def recent_events(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------- tracks


def get_track(spotify_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tracks WHERE spotify_id = ?", (spotify_id,)
        ).fetchone()
    return _row(row) if row else None


def find_track_by_video(video_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tracks WHERE video_id = ? ORDER BY updated_at DESC LIMIT 1",
            (video_id,),
        ).fetchone()
    return _row(row) if row else None


def downloaded_path(spotify_id: str) -> str | None:
    """Return the archived path for a track, or None if we do not have it."""
    with connect() as conn:
        row = conn.execute(
            "SELECT path FROM tracks WHERE spotify_id = ? AND status = 'done'",
            (spotify_id,),
        ).fetchone()
    return _to_absolute(row["path"]) if row else None


def track_by_path(path: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tracks WHERE path = ?", (_to_stored(path),)
        ).fetchone()
    return _row(row) if row else None


def find_archived_by_title_artist(title: str, artist: str) -> dict | None:
    """
    Find an archived track by normalised title and artist.

    This is the fallback for files imported from elsewhere: a manually
    downloaded song may carry no video id, so matching on the video id alone
    would let the extension download it a second time.
    """
    from .catalog import normalise

    want_title = normalise(title)
    want_artist = normalise(artist)
    if not want_title:
        return None

    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE status = 'done' AND path IS NOT NULL"
        ).fetchall()

    for row in rows:
        if normalise(row["title"] or "") != want_title:
            continue
        if not want_artist:
            return _row(row)
        stored = [part.strip() for part in (row["artists"] or "").split(",")]
        if any(normalise(part) == want_artist for part in stored if part):
            return _row(row)
    return None


def register_imported(
    *,
    key: str,
    path: str,
    title: str | None = None,
    artists: str | None = None,
    album: str | None = None,
    video_id: str | None = None,
    yt_url: str | None = None,
    duration_s: int | None = None,
    detail: dict | None = None,
) -> None:
    """Record a file that is already on disk as archived."""
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tracks (spotify_id, video_id, yt_url, title, artists, album,
                                duration_s, status, path, meta_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'done', ?, ?, ?, ?)
            ON CONFLICT (spotify_id) DO UPDATE SET
                video_id   = COALESCE(NULLIF(excluded.video_id, ''), tracks.video_id),
                yt_url     = COALESCE(NULLIF(excluded.yt_url, ''), tracks.yt_url),
                title      = COALESCE(excluded.title, tracks.title),
                artists    = COALESCE(excluded.artists, tracks.artists),
                album      = COALESCE(excluded.album, tracks.album),
                duration_s = COALESCE(excluded.duration_s, tracks.duration_s),
                status     = 'done',
                path       = excluded.path,
                updated_at = excluded.updated_at
            """,
            (
                key,
                video_id or "",
                yt_url or "",
                title,
                artists,
                album,
                duration_s,
                _to_stored(path),
                json.dumps(detail or {}, default=str),
                now,
                now,
            ),
        )


def upsert_track(
    *,
    spotify_id: str,
    video_id: str,
    yt_url: str,
    title: str | None = None,
    artists: str | None = None,
    album: str | None = None,
    duration_s: int | None = None,
    status: str = "queued",
) -> None:
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tracks (spotify_id, video_id, yt_url, title, artists, album,
                                duration_s, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (spotify_id) DO UPDATE SET
                video_id   = excluded.video_id,
                yt_url     = excluded.yt_url,
                title      = COALESCE(excluded.title, tracks.title),
                artists    = COALESCE(excluded.artists, tracks.artists),
                album      = COALESCE(excluded.album, tracks.album),
                duration_s = COALESCE(excluded.duration_s, tracks.duration_s),
                status     = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                spotify_id,
                video_id,
                yt_url,
                title,
                artists,
                album,
                duration_s,
                status,
                now,
                now,
            ),
        )


def set_track_status(
    spotify_id: str,
    status: str,
    *,
    path: str | None = None,
    error: str | None = None,
    bump_attempts: bool = False,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE tracks SET
                status     = ?,
                path       = COALESCE(?, path),
                last_error = ?,
                attempts   = attempts + ?,
                updated_at = ?
            WHERE spotify_id = ?
            """,
            (status, _to_stored(path), error, 1 if bump_attempts else 0, utcnow(), spotify_id),
        )


def all_tracks(limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tracks ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return _rows(rows)


# ----------------------------------------------------------------------- jobs


def create_job(
    *,
    spotify_id: str,
    video_id: str,
    yt_url: str,
    spotify_url: str,
    meta_json: str = "{}",
) -> str:
    job_id = uuid.uuid4().hex[:16]
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (job_id, spotify_id, video_id, yt_url, spotify_url,
                              status, meta_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (job_id, spotify_id, video_id, yt_url, spotify_url, meta_json, now, now),
        )
    return job_id


def update_job(
    job_id: str, status: str, *, path: str | None = None, error: str | None = None
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE jobs SET status = ?, path = COALESCE(?, path),
                            error = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (status, _to_stored(path), error, utcnow(), job_id),
        )


def get_job(job_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row(row) if row else None


def recent_jobs(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return _rows(rows)


def claim_next_job() -> dict | None:
    """
    Atomically take the oldest queued job. Returns None when the queue is empty.

    ``BEGIN IMMEDIATE`` takes the write lock before the SELECT, so two workers
    could not claim the same row even if we later run more than one.
    """
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET status = 'downloading', updated_at = ? WHERE job_id = ?",
            (utcnow(), row["job_id"]),
        )
    return _row(row)


def requeue_stale_jobs() -> int:
    """
    Put jobs stuck in 'downloading' back in the queue.

    Called at startup: if the server died mid-download, that job would otherwise
    sit in 'downloading' forever.
    """
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'queued', updated_at = ?"
            " WHERE status = 'downloading'",
            (utcnow(),),
        )
        return cursor.rowcount


def queue_depth() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
    return {row["status"]: row["n"] for row in rows}


# ------------------------------------------------------------------ backups ---
#
# On a network share the database cannot lean on WAL's locking, so a consistent
# snapshot is cheap insurance: VACUUM INTO writes a compacted copy and is safe
# to run against a live database. The archive is also rebuildable from the tags
# on disk (see import_library.py), so even a lost database is recoverable.


def snapshot_to(destination: Path) -> Path:
    """Write a consistent copy of the database to a specific file."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with connect() as conn:
        conn.execute("VACUUM INTO ?", (str(destination),))
    return destination


def backup(keep: int = 7) -> Path | None:
    """
    Snapshot into ``backups/`` beside the database, keeping the newest ``keep``.

    Returns the snapshot path, or None when there is nothing to copy yet.
    """
    if _db_path is None or not _db_path.exists():
        return None

    folder = _db_path.parent / "backups"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = folder / f"{_db_path.stem}-{stamp}.db"
    if target.exists():
        # Already snapshotted within this second; VACUUM INTO cannot overwrite.
        return target
    snapshot_to(target)
    _prune_backups(folder, _db_path.stem, keep)
    return target


def _prune_backups(folder: Path, stem: str, keep: int) -> None:
    if keep <= 0:
        return
    snapshots = sorted(folder.glob(f"{stem}-*.db"), key=lambda p: p.name, reverse=True)
    for stale in snapshots[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def latest_backup() -> Path | None:
    if _db_path is None:
        return None
    folder = _db_path.parent / "backups"
    snapshots = sorted(folder.glob(f"{_db_path.stem}-*.db"), key=lambda p: p.name)
    return snapshots[-1] if snapshots else None


# ------------------------------------------------------------ relocating -----


def rebase_paths(old_root: str | Path) -> int:
    """
    Re-point recorded paths after the archive has been physically moved.

    A row is rewritten only when its stored path genuinely starts with
    ``old_root``, so this is deterministic: it never guesses which file was
    meant. Rows pointing outside the library are left alone, and nothing is
    checked on disk — the caller is asserting the move already happened.

    Rows already stored relative need no help; they follow the folder by
    definition. Returns the number of rows updated.
    """
    if _root is None:
        raise RuntimeError("configure() with library_root before rebasing")

    prefix = Path(old_root)
    updated = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT spotify_id, path FROM tracks WHERE path IS NOT NULL AND path != ''"
        ).fetchall()
        for row in rows:
            stored = Path(row["path"])
            if not stored.is_absolute():
                continue
            try:
                tail = stored.relative_to(prefix)
            except ValueError:
                continue
            conn.execute(
                "UPDATE tracks SET path = ?, updated_at = ? WHERE spotify_id = ?",
                (_to_stored(_root / tail), utcnow(), row["spotify_id"]),
            )
            updated += 1
    return updated
