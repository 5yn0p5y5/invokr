"""
Regression tests for the download retry in invokr/worker.py.

    python tools/test-download-retry.py

A download usually fails for a transient reason, so the worker gets a second
attempt a second later before it gives up — and only a job that has spent both
attempts is reported as failed. This drives the real ``process_job`` against a
real (temporary) database with a scripted download, and checks the state it
leaves behind.

No network and no yt-dlp: the download itself is replaced. It does import the
server package, so run it from the repository root.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import invokr  # noqa: E402

# config.py builds CONFIG at import time from server/config.json, which points
# at the real library — possibly on a network share. Stub the module out so
# these tests never touch a user's archive.
_fake_config = types.ModuleType("invokr.config")


class _Config:
    pass


_fake_config.Config = _Config
_fake_config.CONFIG = _Config()
_fake_config.SERVER_DIR = ROOT / "server"
sys.modules["invokr.config"] = _fake_config

from invokr import db, worker  # noqa: E402

DB_FILE = ROOT / "server" / "_retry_test.db"
VIDEO_ID = "VIDEOID0001"
YT_URL = f"https://music.youtube.com/watch?v={VIDEO_ID}"


class Meta:
    artist = "Artist"
    title = "Title"

    def to_dict(self):
        return {"artist": self.artist, "title": self.title}


results = []
slept = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def clean_database():
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_FILE) + suffix).unlink(missing_ok=True)


def run(cfg, script, spotify_id, cleanup=None):
    """
    Run one job whose download follows `script`: an exception raises, anything
    else counts as success. Returns (job_id, download calls, cleanup calls).
    """
    calls = []
    cleanups = []
    steps = iter(script)

    def fake_download(cfg, *, yt_url, meta, video_id=""):
        calls.append(video_id)
        step = next(steps)
        if isinstance(step, Exception):
            raise step
        return types.SimpleNamespace(path=ROOT / "out.m4a", bytes=1234)

    worker.native = types.SimpleNamespace(
        download=fake_download,
        cleanup=cleanup or (lambda cfg: cleanups.append(1)),
    )

    job_id = db.create_job(
        spotify_id=spotify_id,
        video_id=VIDEO_ID,
        yt_url=YT_URL,
        spotify_url="",
        meta_json='{"artist": "Artist", "title": "Title"}',
    )
    # What main._enqueue does alongside create_job. process_job keeps this row
    # in step, and the retry endpoint has to move it too.
    db.upsert_track(
        spotify_id=spotify_id,
        video_id=VIDEO_ID,
        yt_url=YT_URL,
        title="Title",
        artists="Artist",
        status="queued",
    )
    worker.process_job(cfg, db.get_job(job_id))
    return job_id, calls, cleanups


def main() -> int:
    clean_database()
    db.configure(DB_FILE, library_root=ROOT / "server")

    worker.TrackMeta = lambda **kw: Meta()
    worker.time = types.SimpleNamespace(sleep=lambda seconds: slept.append(seconds))

    cfg = types.SimpleNamespace(library_path=ROOT / "server")

    # --- the first attempt fails and the retry succeeds --------------------
    job_id, calls, cleanups = run(cfg, [RuntimeError("bot check"), "ok"], "itunes:1")
    job = db.get_job(job_id)
    events = [event["kind"] for event in db.recent_events(limit=20)]

    check("retry succeeds -> the job is done", job["status"] == "done", job["status"])
    check("two download attempts were made", len(calls) == 2, len(calls))
    check("the temp dir was cleared before the retry", len(cleanups) == 1, len(cleanups))
    check("the retry waited one second", slept == [1.0], slept)
    check("the retry was recorded", "download_retry" in events, events)

    # --- both attempts fail -------------------------------------------------
    slept.clear()
    job_id, calls, cleanups = run(
        cfg, [RuntimeError("bot check"), RuntimeError("bot check again")], "itunes:2"
    )
    job = db.get_job(job_id)
    events = [event["kind"] for event in db.recent_events(limit=20)]

    check("both attempts fail -> the job failed", job["status"] == "failed", job["status"])
    check("two download attempts were made", len(calls) == 2, len(calls))
    check("the second error is the one reported", "bot check again" in (job["error"] or ""), job["error"])
    check("no temp files are left behind", len(cleanups) == 2, len(cleanups))
    check("the failure was recorded", "download_failed" in events, events)

    # --- a cleanup that raises must not derail the job ----------------------
    def exploding_cleanup(cfg):
        raise OSError("temp dir vanished")

    slept.clear()
    job_id, calls, _ = run(
        cfg, [RuntimeError("nope"), RuntimeError("nope")], "itunes:3", exploding_cleanup
    )
    check(
        "a failing cleanup still reaches a terminal state",
        db.get_job(job_id)["status"] == "failed",
        db.get_job(job_id)["status"],
    )

    # --- what POST /api/jobs/{id}/retry does to a failed job ----------------
    before = db.get_job(job_id)
    db.update_job(job_id, "queued", error=None)
    db.set_track_status(before["spotify_id"], "queued", error=None)
    after = db.get_job(job_id)
    track = [t for t in db.all_tracks(limit=50) if t["spotify_id"] == before["spotify_id"]]

    check("retry re-queues the job", after["status"] == "queued", after["status"])
    check("retry clears the stored error", after["error"] is None, after["error"])
    check(
        "the track follows the job back to queued",
        bool(track) and track[0]["status"] == "queued",
        track[0]["status"] if track else "no track row",
    )
    check("the worker can claim it again", db.claim_next_job() is not None)

    # --- a failure that is fixed by liking the song again -------------------
    # The heart of the reported bug: re-liking does not retry the job, it runs
    # the whole like path and creates a NEW one, so the old row stays "failed"
    # for ever and the panel keeps offering to retry a song it already has.
    slept.clear()
    stale_job, _, _ = run(cfg, [RuntimeError("bot check"), RuntimeError("bot check")], "itunes:4")
    check("the first job is a failure", db.get_job(stale_job)["status"] == "failed")
    check(
        "and it is reported while the track has no file",
        any(job["job_id"] == stale_job for job in db.outstanding_failures()),
    )

    fresh_job, _, _ = run(cfg, ["ok"], "itunes:4")
    check("the new job for the same track succeeds", db.get_job(fresh_job)["status"] == "done")
    check(
        "the old failure is no longer outstanding",
        not db.failure_outstanding(db.get_job(stale_job)),
    )
    check(
        "and it is dropped from the failure list",
        not any(job["job_id"] == stale_job for job in db.outstanding_failures()),
        [job["job_id"] for job in db.outstanding_failures()],
    )
    check(
        "the track is archived",
        db.track_state("itunes:4")["status"] == "done"
        and bool(db.track_state("itunes:4")["path"]),
    )

    # A retry already in flight counts as handled too.
    db.set_track_status("itunes:4", "queued", error=None)
    check(
        "a failure whose track is queued again is not outstanding",
        not db.failure_outstanding(db.get_job(stale_job)),
    )

    # A genuine failure is still reported.
    genuine, _, _ = run(cfg, [RuntimeError("nope"), RuntimeError("nope")], "itunes:5")
    check(
        "a failure with nothing archived behind it is still reported",
        any(job["job_id"] == genuine for job in db.outstanding_failures()),
        [job["job_id"] for job in db.outstanding_failures()],
    )

    failed = len([result for result in results if not result])
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        clean_database()
