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

# A download usually fails for a transient reason — a YouTube bot check, a
# dropped connection, a 403 on the media URL — and the very same job almost
# always succeeds a moment later. One retry, a second apart, is enough to make
# those disappear; a second failure is real and is reported.
DOWNLOAD_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1.0


def _clear_temp_dir(cfg: Config) -> None:
    """
    Best effort: a leftover temp file must never become a failed job.

    This runs inside an exception handler, so it must not raise — an exception
    escaping here would take the worker thread with it and stop every future
    download.
    """
    try:
        native.cleanup(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not clear the temp dir: %s", exc)


def _attempt_download(cfg: Config, job: dict, meta: TrackMeta):
    """One download attempt. Raises whatever the download raised."""
    return native.download(
        cfg,
        yt_url=job["yt_url"],
        meta=meta,
        video_id=job["video_id"],
    )


def process_job(cfg: Config, job: dict) -> None:
    """Run one job to completion, retrying once, and record the outcome either way."""
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

    result = None

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            result = _attempt_download(cfg, job, meta)
            break
        except Exception as exc:  # noqa: BLE001 - any failure must be recorded, not crash the worker
            message = f"{type(exc).__name__}: {exc}"

            if attempt < DOWNLOAD_ATTEMPTS:
                logger.warning(
                    "job %s attempt %s/%s failed: %s - retrying in %.0fs",
                    job_id,
                    attempt,
                    DOWNLOAD_ATTEMPTS,
                    message,
                    RETRY_DELAY_SECONDS,
                )
                db.record_event(
                    "download_retry",
                    video_id=job["video_id"],
                    spotify_id=spotify_id,
                    detail={"job_id": job_id, "attempt": attempt, "error": message},
                )
                # The download writes into one shared temp dir and then picks
                # its own file back up by glob, so a leftover from the failed
                # attempt could be mistaken for the result of the next one.
                # Clearing it first is what makes the retry safe.
                _clear_temp_dir(cfg)
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            db.update_job(job_id, "failed", error=message)
            db.set_track_status(spotify_id, "failed", error=message)
            db.record_event(
                "download_failed",
                video_id=job["video_id"],
                spotify_id=spotify_id,
                detail=message,
            )
            logger.error(
                "job %s failed after %s attempt(s): %s", job_id, DOWNLOAD_ATTEMPTS, message
            )
            _clear_temp_dir(cfg)
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
        try:
            process_job(cfg, job)
        except Exception as exc:  # noqa: BLE001
            # process_job records its own failures, so reaching here means the
            # recording itself broke. Losing the thread would stop every future
            # download silently, so log it, park the job, and carry on.
            message = f"{type(exc).__name__}: {exc}"
            logger.exception("job %s crashed the worker loop", job["job_id"])
            try:
                db.update_job(job["job_id"], "failed", error=message)
                db.set_track_status(job["spotify_id"], "failed", error=message)
                db.record_event(
                    "download_failed",
                    video_id=job["video_id"],
                    spotify_id=job["spotify_id"],
                    detail=message,
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record the crash for job %s", job["job_id"])
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
