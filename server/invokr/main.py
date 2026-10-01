"""
FastAPI application for the invokr server.

Every ``/api/*`` endpoint requires the shared bearer token. The ``/review/*``
pages are opened by your browser, which does not have the token, so they are
authorized by an unguessable, single-use, time-limited review ID instead — and
those IDs can only be minted by an authenticated API call.
"""

from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from . import __version__, db, library, native, preflight, review, worker
from .catalog import Catalog, clean_title, decide, search_artwork
from .config import CONFIG
from .schemas import (
    ChoiceRequest,
    LibraryImportRequest,
    ManualMetadataRequest,
    ReviewRequest,
    UnlikeRequest,
)

logger = logging.getLogger("invokr")

_worker_thread = None
_worker_stop = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _worker_thread, _worker_stop

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    db.configure(CONFIG.db_path)
    requeued = db.requeue_stale_jobs()
    db.record_event(
        "server_start",
        detail={"host": CONFIG.host, "port": CONFIG.port, "requeued_jobs": requeued},
    )

    _worker_thread, _worker_stop = worker.start(CONFIG)
    logger.info("invokr %s listening on %s:%s", __version__, CONFIG.host, CONFIG.port)

    try:
        yield
    finally:
        if _worker_stop is not None:
            _worker_stop.set()
        if _worker_thread is not None:
            _worker_thread.join(timeout=5)
        db.record_event("server_stop")


app = FastAPI(title="invokr", version=__version__, lifespan=lifespan)


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Gate every real endpoint behind the shared bearer token."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = authorization[7:].strip()
    if not secrets.compare_digest(supplied, CONFIG.token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _form(request: Request) -> dict[str, str]:
    """Parse an urlencoded form body without requiring python-multipart."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _catalogue_key(meta) -> str:
    """Stable identity for a track. The column is named spotify_id historically."""
    return f"{meta.source}:{meta.source_id}"


def _archived(row) -> dict | None:
    """Shape a tracks row into an 'already archived' marker, or None."""
    if row and row.get("status") == "done" and row.get("path"):
        return {"path": row["path"], "title": row.get("title")}
    return None


def _enqueue(session: "review.ReviewSession", meta) -> str:
    """Create a download job and record the intent. Shared by both review paths."""
    key = _catalogue_key(meta)
    job_id = db.create_job(
        spotify_id=key,
        video_id=session.video_id,
        yt_url=session.yt_url,
        spotify_url=meta.url or "",
        meta_json=json.dumps(meta.to_dict()),
    )
    db.upsert_track(
        spotify_id=key,
        video_id=session.video_id,
        yt_url=session.yt_url,
        title=meta.title,
        artists=", ".join(meta.artists) or meta.artist,
        album=meta.album,
        duration_s=int(meta.duration_s) if meta.duration_s else None,
        status="queued",
    )
    db.record_event(
        "job_created",
        video_id=session.video_id,
        spotify_id=key,
        detail={"job_id": job_id, "source": meta.source, "title": meta.title},
    )
    return job_id


# ------------------------------------------------------------------ liveness


@app.get("/")
def root() -> dict:
    """Unauthenticated liveness probe. Deliberately leaks nothing."""
    return {"service": "invokr", "version": __version__, "status": "ok"}


@app.get("/api/health")
def health(_: None = Depends(require_token)) -> dict:
    """Full preflight report. Reaching this at all proves the token is valid."""
    report = preflight.run(CONFIG)
    report["config"] = CONFIG.public()
    return report


# --------------------------------------------------------------------- API


@app.post("/api/review", status_code=status.HTTP_201_CREATED)
def create_review(
    payload: ReviewRequest, _: None = Depends(require_token)
) -> dict:
    """
    Search the catalogue for a liked track and open a review session.

    If this video has already been archived we return ``skipped`` instead of
    minting a session, so re-liking a song does not spam browser tabs.
    """
    video_id = payload.video_id.strip()
    if not video_id:
        raise HTTPException(status_code=422, detail="videoId is required")

    yt_url = payload.yt_url or f"https://music.youtube.com/watch?v={video_id}"

    already = _archived(db.find_track_by_video(video_id))
    if already and not payload.force:
        db.record_event(
            "like_duplicate", video_id=video_id, detail={"path": already["path"]}
        )
        return {"skipped": True, "reason": "already archived", "already": already}

    title = payload.title.strip()
    artists = [a for a in (payload.artists or []) if a]
    album = payload.album or ""
    duration = payload.duration_sec

    if not title:
        # A like from a playlist row or the "…" menu arrives with only a video
        # id, so ask YouTube what the track actually is before searching.
        try:
            probed = native.probe_metadata(yt_url, CONFIG)
        except Exception as exc:  # noqa: BLE001
            db.record_event(
                "like_unresolved", video_id=video_id, detail=str(exc)
            )
            probed = {}
        title = probed.get("title") or ""
        album = album or probed.get("album") or ""
        duration = duration or probed.get("duration") or None
        if not artists and probed.get("author"):
            artists = [probed["author"]]

    # Files imported from an existing collection may carry no video id, so fall
    # back to matching on title and artist before downloading a second copy.
    if already is None and title and not payload.force:
        already = _archived(
            db.find_archived_by_title_artist(title, artists[0] if artists else "")
        )
        if already:
            db.record_event(
                "like_duplicate",
                video_id=video_id,
                detail={"path": already["path"], "matched": "title/artist"},
            )
            return {
                "skipped": True,
                "reason": "already archived (matched by title/artist)",
                "already": already,
            }

    catalog = Catalog(CONFIG)
    # Search with the cleaned title, but keep the original for display: a
    # YouTube title like "a-ha - Take On Me (Official Video) [4K]" searches
    # catalogues badly.
    search_title = clean_title(title, artists[0] if artists else "")
    candidates, errors = catalog.search(
        title=search_title,
        artists=artists,
        duration_s=duration,
    )

    decision = decide(candidates, cfg=CONFIG)

    session = review.create(
        cfg=CONFIG,
        video_id=video_id,
        yt_url=yt_url,
        title=title,
        artists=artists,
        album=album,
        duration_s=duration,
        candidates=candidates,
        errors=errors,
        already=already,
        reason=decision.reason,
    )

    db.record_event(
        "like_received",
        video_id=video_id,
        detail={
            "title": title,
            "artists": artists,
            "candidates": len(candidates),
            "provider_errors": errors,
        },
    )

    base = f"http://{CONFIG.host}:{CONFIG.port}"

    # Obviously-correct matches download without asking. The session is still
    # created and immediately marked confirmed, so the decision stays auditable
    # and the same enqueue path is used either way.
    if decision.verdict == "auto" and decision.chosen is not None:
        chosen = catalog.enrich(decision.chosen)
        session.confirmed = True
        session.chosen = chosen
        job_id = _enqueue(session, chosen)
        db.record_event(
            "auto_accepted",
            video_id=video_id,
            spotify_id=_catalogue_key(chosen),
            detail={
                "job_id": job_id,
                "score": chosen.score,
                "title": chosen.title,
                "artist": chosen.artist,
            },
        )
        return {
            "skipped": False,
            "auto": True,
            "jobId": job_id,
            "title": chosen.title,
            "artist": chosen.artist,
            "album": chosen.album,
            "source": chosen.source,
            "coverUrl": chosen.cover_url,
            "reviewUrl": f"{base}/review/{session.review_id}",
            "decision": decision.to_dict(),
        }

    return {
        "skipped": False,
        "auto": False,
        "reviewId": session.review_id,
        "reviewUrl": f"{base}/review/{session.review_id}",
        "candidateCount": len(candidates),
        "topCandidate": candidates[0].to_dict() if candidates else None,
        "decision": decision.to_dict(),
        "errors": errors,
        "already": already,
    }


@app.post("/api/unlike", status_code=status.HTTP_202_ACCEPTED)
def record_unlike(payload: UnlikeRequest, _: None = Depends(require_token)) -> dict:
    """
    Log an un-like. Deliberately does nothing else: the archive is append-only,
    so a mis-click can never lose a file.
    """
    db.record_event(
        "unlike", video_id=payload.video_id, detail={"title": payload.title}
    )
    return {"logged": True}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, _: None = Depends(require_token)) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return job


@app.get("/api/tracks")
def get_tracks(limit: int = 100, _: None = Depends(require_token)) -> dict:
    return {"tracks": db.all_tracks(limit=limit)}


@app.get("/api/events")
def get_events(limit: int = 50, _: None = Depends(require_token)) -> dict:
    return {"events": db.recent_events(limit=limit)}


@app.get("/api/queue")
def get_queue(_: None = Depends(require_token)) -> dict:
    return {"depth": db.queue_depth()}


# ------------------------------------------------------------- review pages


def _not_found() -> HTMLResponse:
    return HTMLResponse(
        review.render_done(
            CONFIG,
            title="This review link is no longer valid",
            body="<p>It may have expired, or already been used. Like the song again "
            "to start a new review.</p>",
        ),
        status_code=404,
    )


@app.get("/review/{review_id}", response_class=HTMLResponse)
def review_page(review_id: str) -> HTMLResponse:
    session = review.get(review_id)
    if session is None or session.confirmed:
        return _not_found()
    return HTMLResponse(review.render(session, CONFIG))


@app.post("/review/{review_id}/confirm", response_class=HTMLResponse)
async def review_confirm(review_id: str, request: Request) -> HTMLResponse:
    session = review.get(review_id)
    if session is None or session.confirmed:
        return _not_found()

    form = await _form(request)
    try:
        index = int(form.get("choice", ""))
        chosen = session.candidates[index]
    except (ValueError, IndexError):
        return HTMLResponse(
            review.render(session, CONFIG, message="Pick a candidate first."),
            status_code=400,
        )

    catalog = Catalog(CONFIG)
    chosen = catalog.enrich(chosen)
    session.confirmed = True
    session.chosen = chosen

    job_id = _enqueue(session, chosen)

    return HTMLResponse(
        review.render_done(
            CONFIG,
            title="Queued for download",
            body=(
                f"<p><strong>{chosen.artist} — {chosen.title}</strong></p>"
                f"<p class='sub'>Audio from <code>{session.yt_url}</code><br>"
                f"Metadata from {chosen.source}</p>"
            ),
            job_id=job_id,
        )
    )


@app.post("/review/{review_id}/skip", response_class=HTMLResponse)
async def review_skip(review_id: str) -> HTMLResponse:
    session = review.get(review_id)
    if session is None:
        return _not_found()
    session.confirmed = True
    db.record_event("like_skipped", video_id=session.video_id)
    return HTMLResponse(
        review.render_done(
            CONFIG, title="Skipped", body="<p>Nothing was downloaded.</p>"
        )
    )


@app.post("/review/{review_id}/research")
async def review_research(review_id: str, request: Request) -> RedirectResponse:
    session = review.get(review_id)
    if session is None or session.confirmed:
        return RedirectResponse(url=f"/review/{review_id}", status_code=303)

    form = await _form(request)
    title = (form.get("title") or session.yt_title).strip()
    artist = (form.get("artist") or "").strip()

    catalog = Catalog(CONFIG)
    candidates, errors = catalog.search(
        title=title,
        artist=artist,
        artists=[artist] if artist else [],
        duration_s=session.duration_s,
    )
    session.yt_title = title
    session.yt_artist = artist
    review.replace_candidates(session, candidates, errors)
    return RedirectResponse(url=f"/review/{review_id}", status_code=303)


# ------------------------------------------------- manual metadata + artwork


@app.get("/review/{review_id}/art")
def review_art(review_id: str, q: str = "") -> dict:
    """
    Artwork candidates for the manual picker.

    Authorized by the review id like the page itself, because the browser calls
    this endpoint directly and has no bearer token.
    """
    session = review.get(review_id)
    if session is None or session.confirmed:
        raise HTTPException(status_code=404, detail="unknown or used review")
    return {"query": q, "results": [item.to_dict() for item in search_artwork(q)]}


@app.post("/review/{review_id}/manual", response_class=HTMLResponse)
async def review_manual(review_id: str, request: Request) -> HTMLResponse:
    """
    Download using metadata typed by hand.

    This exists for tracks no catalogue carries — an unreleased or leaked song
    gets correct tags anyway, with artwork borrowed from another release.
    """
    session = review.get(review_id)
    if session is None or session.confirmed:
        return _not_found()

    form = await _form(request)
    meta = review.manual_meta(form, session)
    if not meta.title:
        return HTMLResponse(
            review.render(session, CONFIG, message="A title is required."),
            status_code=400,
        )

    session.confirmed = True
    session.chosen = meta
    job_id = _enqueue(session, meta)

    db.record_event(
        "manual_metadata",
        video_id=session.video_id,
        spotify_id=_catalogue_key(meta),
        detail={
            "title": meta.title,
            "artist": meta.artist,
            "album": meta.album,
            "cover": bool(meta.cover_url),
        },
    )

    return HTMLResponse(
        review.render_done(
            CONFIG,
            title="Queued with your metadata",
            body=(
                f"<p><strong>{review.ESC(meta.artist)} — {review.ESC(meta.title)}</strong></p>"
                f"<p class='sub'>Album: {review.ESC(meta.album) or '—'}<br>"
                f"Artwork: {'yes' if meta.cover_url else 'none'}<br>"
                f"Audio from <code>{review.ESC(session.yt_url)}</code></p>"
            ),
            job_id=job_id,
        )
    )


# ---------------------------------------------------------- library import


@app.post("/api/library/import")
def library_import(
    payload: LibraryImportRequest, _: None = Depends(require_token)
) -> dict:
    """
    Register songs you already have, so the extension stops re-fetching them.

    Reads the YouTube URL from the comment tag and the Spotify URL from spotDL's
    WOAS atom, so existing spotDL output is identified exactly rather than by
    guessing at filenames. Idempotent: safe to re-run over the whole collection.
    """
    root = Path(payload.path) if payload.path else CONFIG.library_path
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"{root} does not exist")

    records = library.scan(root, recursive=payload.recursive)
    report = library.import_records(records, dry_run=payload.dry_run, root=str(root))
    return report.to_dict()


# ------------------------------------------- JSON review API (toolbar popup)
#
# The popup is an extension page, so it cannot embed the server-rendered review
# page. It fetches this JSON instead and draws the same information itself.
# These mirror the /review/... form endpoints above and share the same helpers.


def _open_session(review_id: str) -> review.ReviewSession:
    session = review.get(review_id)
    if session is None or session.confirmed:
        raise HTTPException(status_code=404, detail="unknown or used review")
    return session


@app.get("/api/reviews")
def list_reviews(_: None = Depends(require_token)) -> dict:
    """Review sessions still waiting for a decision, newest first."""
    return {"reviews": review.pending(CONFIG)}


@app.get("/api/review/{review_id}")
def get_review(review_id: str, _: None = Depends(require_token)) -> dict:
    return review.detail(_open_session(review_id))


@app.get("/api/review/{review_id}/art")
def api_review_art(
    review_id: str, q: str = "", _: None = Depends(require_token)
) -> dict:
    _open_session(review_id)
    return {"query": q, "results": [item.to_dict() for item in search_artwork(q)]}


@app.post("/api/review/{review_id}/confirm")
def api_review_confirm(
    review_id: str, payload: ChoiceRequest, _: None = Depends(require_token)
) -> dict:
    session = _open_session(review_id)
    try:
        chosen = review.pick(session, payload.choice)
    except IndexError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    chosen = Catalog(CONFIG).enrich(chosen)
    session.confirmed = True
    session.chosen = chosen
    job_id = _enqueue(session, chosen)

    return {
        "jobId": job_id,
        "title": chosen.title,
        "artist": chosen.artist,
        "album": chosen.album,
        "source": chosen.source,
    }


@app.post("/api/review/{review_id}/manual")
def api_review_manual(
    review_id: str, payload: ManualMetadataRequest, _: None = Depends(require_token)
) -> dict:
    session = _open_session(review_id)

    meta = review.manual_meta(payload.model_dump(), session)
    if not meta.title:
        raise HTTPException(status_code=400, detail="a title is required")

    session.confirmed = True
    session.chosen = meta
    job_id = _enqueue(session, meta)

    db.record_event(
        "manual_metadata",
        video_id=session.video_id,
        spotify_id=_catalogue_key(meta),
        detail={
            "title": meta.title,
            "artist": meta.artist,
            "album": meta.album,
            "cover": bool(meta.cover_url),
            "via": "popup",
        },
    )
    return {
        "jobId": job_id,
        "title": meta.title,
        "artist": meta.artist,
        "album": meta.album,
    }


@app.post("/api/review/{review_id}/skip")
def api_review_skip(review_id: str, _: None = Depends(require_token)) -> dict:
    session = review.get(review_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown review")
    session.confirmed = True
    db.record_event("like_skipped", video_id=session.video_id)
    return {"skipped": True}


# ---------------------------------------------------------------- cancelling


@app.delete("/api/review/{review_id}")
def api_review_cancel(review_id: str, _: None = Depends(require_token)) -> dict:
    """
    Discard a pending review.

    Unlike skip, nothing is recorded as a decision — this is "forget it ever
    asked", which is what you want for a like that was itself a mistake.
    """
    session = review.get(review_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown review")

    video_id = session.video_id
    review.discard(session)
    db.record_event("like_cancelled", video_id=video_id)
    return {"cancelled": True, "videoId": video_id}


@app.delete("/api/reviews")
def api_reviews_cancel_all(_: None = Depends(require_token)) -> dict:
    """Clear every pending review in one go."""
    removed = review.discard_all()
    db.record_event("reviews_cancelled", detail={"count": removed})
    return {"cancelled": removed}


@app.get("/api/jobs")
def api_list_jobs(limit: int = 10, _: None = Depends(require_token)) -> dict:
    return {"jobs": db.recent_jobs(limit=limit)}


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str, _: None = Depends(require_token)) -> dict:
    """
    Cancel a download that has not started yet.

    Only 'queued' jobs qualify: once the worker has claimed one, yt-dlp is
    already running and there is nothing to signal.
    """
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")

    if job["status"] != "queued":
        return {
            "cancelled": False,
            "status": job["status"],
            "detail": "only queued downloads can be cancelled",
        }

    db.update_job(job_id, "cancelled")
    db.set_track_status(job["spotify_id"], "cancelled")
    db.record_event(
        "job_cancelled", video_id=job["video_id"], detail={"job_id": job_id}
    )
    return {"cancelled": True, "jobId": job_id}
