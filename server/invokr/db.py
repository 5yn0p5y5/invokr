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


def configure(path: Path) -> None:
    """Point the module at a database file and create or migrate the schema."""
    global _db_path
    _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    return dict(row) if row else None


def find_track_by_video(video_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tracks WHERE video_id = ? ORDER BY updated_at DESC LIMIT 1",
            (video_id,),
        ).fetchone()
    return dict(row) if row else None


def downloaded_path(spotify_id: str) -> str | None:
    """Return the archived path for a track, or None if we do not have it."""
    with connect() as conn:
        row = conn.execute(
            "SELECT path FROM tracks WHERE spotify_id = ? AND status = 'done'",
            (spotify_id,),
        ).fetchone()
    return row["path"] if row else None


def track_by_path(path: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tracks WHERE path = ?", (path,)
        ).fetchone()
    return dict(row) if row else None


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
            return dict(row)
        stored = [part.strip() for part in (row["artists"] or "").split(",")]
        if any(normalise(part) == want_artist for part in stored if part):
            return dict(row)
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
                path,
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
            (status, path, error, 1 if bump_attempts else 0, utcnow(), spotify_id),
        )


def all_tracks(limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tracks ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


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
            (status, path, error, utcnow(), job_id),
        )


def get_job(job_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def recent_jobs(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


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
    return dict(row)


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
