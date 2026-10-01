"""
Background download worker.

One thread, one download at a time. YouTube rate limits aggressive parallel
fetching, and serialising keeps the logs readable and the failure modes obvious.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from . import db, native
from .catalog import TrackMeta
from .config import Config

logger = logging.getLogger("invokr.worker")

IDLE_SLEEP_SECONDS = 2.0


def process_job(cfg: Config, job: dict) -> None:
    """Run one job to completion, recording the outcome either way."""
    job_id = job["job_id"]
    spotify_id = job["spotify_id"]

    try:
        meta = TrackMeta(**json.loads(job.get("meta_json") or "{}"))
    except (ValueError, TypeError) as exc:
        message = f"stored metadata is unreadable: {exc}"
        db.update_job(job_id, "failed", error=message)
        db.set_track_status(spotify_id, "failed", error=message, bump_attempts=True)
        db.record_event("download_failed", video_id=job["video_id"], detail=message)
        logger.error("job %s: %s", job_id, message)
        return

    logger.info("downloading %s - %s (%s)", meta.artist, meta.title, job["yt_url"])
    db.set_track_status(spotify_id, "downloading", bump_attempts=True)

    try:
        result = native.download(
            cfg,
            yt_url=job["yt_url"],
            meta=meta,
            video_id=job["video_id"],
        )
    except Exception as exc:  # noqa: BLE001 - any failure must be recorded, not crash the worker
        message = f"{type(exc).__name__}: {exc}"
        db.update_job(job_id, "failed", error=message)
        db.set_track_status(spotify_id, "failed", error=message)
        db.record_event(
            "download_failed",
            video_id=job["video_id"],
            spotify_id=spotify_id,
            detail=message,
        )
        logger.error("job %s failed: %s", job_id, message)
        return

    # The file on disk is the source of truth. We never infer success from a
    # return code alone.
    db.update_job(job_id, "done", path=str(result.path))
    db.set_track_status(spotify_id, "done", path=str(result.path))
    db.record_event(
        "download_done",
        video_id=job["video_id"],
        spotify_id=spotify_id,
        detail={"path": str(result.path), "bytes": result.bytes},
    )
    logger.info("job %s done: %s (%s bytes)", job_id, result.path, f"{result.bytes:,}")


def loop(cfg: Config, stop_event: threading.Event) -> None:
    logger.info("worker started")
    removed = native.cleanup(cfg)
    if removed:
        logger.info("cleaned %s leftover temp item(s)", removed)

    while not stop_event.is_set():
        job = db.claim_next_job()
        if job is None:
            stop_event.wait(IDLE_SLEEP_SECONDS)
            continue
        process_job(cfg, job)
    logger.info("worker stopped")


def start(cfg: Config) -> tuple[threading.Thread, threading.Event]:
    """Start the worker thread. Returns the thread and its stop event."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=loop, args=(cfg, stop_event), name="invokr-worker", daemon=True
    )
    thread.start()
    return thread, stop_event


def drain(cfg: Config, timeout: float = 300.0) -> None:
    """Process queued jobs until the queue is empty. Used by tests and CLI."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = db.claim_next_job()
        if job is None:
            return
        process_job(cfg, job)
