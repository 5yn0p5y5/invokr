"""
Review sessions and the validation page.

The page exists because automatic matching is good but not perfect: the player
knows the song's exact duration, and iTunes/Deezer disagree about which album a
track belongs to. Showing the candidates and letting a human click is cheaper
than getting the metadata subtly wrong on disk forever.

Authorization model: the review page is opened by the browser, which does not
have the bearer token. The unguessable, single-use, TTL-bounded review ID in the
URL *is* the capability. Sessions are created only by an authenticated API call.
"""

from __future__ import annotations

import hashlib
import html
import secrets
import threading
import time
from dataclasses import dataclass, field

from .catalog import TrackMeta, normalise
from .config import Config

ESC = html.escape


@dataclass
class ReviewSession:
    review_id: str
    video_id: str
    yt_url: str
    yt_title: str
    yt_artist: str
    yt_album: str
    duration_s: float | None
    candidates: list[TrackMeta] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    confirmed: bool = False
    chosen: TrackMeta | None = None
    job_id: str | None = None
    already: dict | None = None
    # Why this was not auto-accepted, shown in the popup.
    reason: str = ""

    def expires_in(self, ttl_minutes: int) -> float:
        return self.created_at + ttl_minutes * 60 - time.time()


_sessions: dict[str, ReviewSession] = {}
_lock = threading.Lock()


def create(
    *,
    cfg: Config,
    video_id: str,
    yt_url: str,
    title: str,
    artists: list[str],
    album: str,
    duration_s: float | None,
    candidates: list[TrackMeta],
    errors: list[str],
    already: dict | None = None,
    reason: str = "",
) -> ReviewSession:
    session = ReviewSession(
        review_id=secrets.token_urlsafe(24),
        video_id=video_id,
        yt_url=yt_url,
        yt_title=title,
        yt_artist=", ".join(artists) if artists else "",
        yt_album=album,
        duration_s=duration_s,
        candidates=candidates,
        errors=errors,
        already=already,
        reason=reason,
    )
    with _lock:
        purge(cfg)
        _sessions[session.review_id] = session
    return session


def get(review_id: str) -> ReviewSession | None:
    with _lock:
        return _sessions.get(review_id)


def purge(cfg: Config) -> int:
    """Drop expired sessions. Callers must hold the lock."""
    expired = [
        key
        for key, session in _sessions.items()
        if session.expires_in(cfg.review_ttl_minutes) <= 0
    ]
    for key in expired:
        del _sessions[key]
    return len(expired)


def replace_candidates(session: ReviewSession, candidates: list[TrackMeta], errors: list[str]) -> None:
    session.candidates = candidates
    session.errors = errors


def discard(session: ReviewSession) -> None:
    """Remove a session outright, without recording it as a skip."""
    with _lock:
        _sessions.pop(session.review_id, None)


def discard_all() -> int:
    """Drop every pending session. Returns how many were removed."""
    with _lock:
        removed = len(_sessions)
        _sessions.clear()
    return removed


def pick(session: ReviewSession, index: int) -> TrackMeta:
    """Select a candidate by index, raising IndexError when out of range."""
    if index < 0 or index >= len(session.candidates):
        raise IndexError(f"no candidate at index {index}")
    return session.candidates[index]


def pending(cfg: Config) -> list[dict]:
    """
    Unconfirmed, unexpired sessions, newest first.

    The extension popup is opened by the user, not by the like itself, so it
    needs to ask what is waiting rather than being handed it.
    """
    with _lock:
        purge(cfg)
        items = [
            {
                "reviewId": session.review_id,
                "videoId": session.video_id,
                "title": session.yt_title,
                "artist": session.yt_artist,
                "album": session.yt_album,
                "durationSec": session.duration_s,
                "candidateCount": len(session.candidates),
                "createdAt": session.created_at,
                "expiresInSec": int(max(0, session.expires_in(cfg.review_ttl_minutes))),
            }
            for session in _sessions.values()
            if not session.confirmed
        ]
    items.sort(key=lambda item: item["createdAt"], reverse=True)
    return items


def detail(session: ReviewSession) -> dict:
    """Full session payload for the popup to render client-side."""
    return {
        "reviewId": session.review_id,
        "videoId": session.video_id,
        "ytUrl": session.yt_url,
        "title": session.yt_title,
        "artist": session.yt_artist,
        "album": session.yt_album,
        "durationSec": session.duration_s,
        "candidates": [candidate.to_dict() for candidate in session.candidates],
        "errors": session.errors,
        "already": session.already,
        "reason": session.reason,
    }


# --------------------------------------------------------------------- html


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "?"
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _card(session: ReviewSession, index: int, cand: TrackMeta) -> str:
    # Pre-select the best-ranked candidate so the common case is one click.
    checked = "checked" if index == 0 else ""

    badge = f'<span class="badge src-{ESC(cand.source)}">{ESC(cand.source)}</span>'
    warn = ""
    if session.duration_s and not cand.duration_ok:
        warn = (
            f'<span class="badge warn">duration {cand.duration_delta_s:+.1f}s</span>'
        )
    elif session.duration_s and abs(cand.duration_delta_s) > 2:
        warn = f'<span class="badge ok">duration {cand.duration_delta_s:+.1f}s</span>'

    cover = (
        f'<img class="cover" src="{ESC(cand.cover_url)}" alt="" loading="lazy">'
        if cand.cover_url
        else '<div class="cover empty"></div>'
    )

    meta_bits = [
        f"Album: {ESC(cand.album)}" if cand.album else "",
        f"Year: {ESC(cand.year)}" if cand.year else "",
        f"Track: {cand.track_number}/{cand.track_total}" if cand.track_number else "",
        f"Disc: {cand.disc_number}" if cand.disc_number else "",
        f"ISRC: {ESC(cand.isrc)}" if cand.isrc else "",
    ]
    meta_line = " · ".join(bit for bit in meta_bits if bit)

    return f"""
    <label class="card">
      <input type="radio" name="choice" value="{index}" {checked}>
      {cover}
      <div class="card-body">
        <div class="card-title">{ESC(cand.artist)} — {ESC(cand.title)}</div>
        <div class="card-meta">{meta_line}</div>
        <div class="card-tags">
          {badge}{warn}
          <span class="badge neutral">{_fmt_duration(cand.duration_s)}</span>
          <span class="badge neutral">score {cand.score}</span>
        </div>
      </div>
    </label>"""


_ART_PICKER_JS = """
<script>
(() => {
  "use strict";
  const reviewId = "__REVIEW_ID__";
  const ytId = "__YT_ID__";
  const coverInput = document.getElementById("coverUrl");
  const results = document.getElementById("artResults");
  const chosen = document.getElementById("artChosen");
  const query = document.getElementById("artQuery");
  if (!coverInput) return;

  function select(url, label) {
    coverInput.value = url;
    chosen.innerHTML = "";
    const img = document.createElement("img");
    img.src = url;
    img.alt = "";
    const span = document.createElement("span");
    span.className = "sub";
    span.textContent = label || "artwork selected";
    chosen.appendChild(img);
    chosen.appendChild(span);
    for (const el of results.querySelectorAll(".artitem")) {
      el.classList.toggle("sel", el.dataset.url === url);
    }
  }

  async function search() {
    const q = query.value.trim();
    if (!q) return;
    results.textContent = "searching...";
    try {
      const response = await fetch(`/review/${reviewId}/art?q=${encodeURIComponent(q)}`);
      const data = await response.json();
      results.innerHTML = "";
      for (const hit of data.results) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "artitem";
        button.dataset.url = hit.url;
        button.title = `${hit.artist} - ${hit.album} (${hit.source})`;
        const img = document.createElement("img");
        img.src = hit.thumb;
        img.loading = "lazy";
        img.alt = hit.album || "artwork";
        button.appendChild(img);
        button.addEventListener("click", () =>
          select(hit.url, `${hit.artist} - ${hit.album} (${hit.year || hit.source})`)
        );
        results.appendChild(button);
      }
      if (!data.results.length) results.textContent = "no artwork found";
    } catch (error) {
      results.textContent = `search failed: ${error.message}`;
    }
  }

  document.getElementById("artSearch").addEventListener("click", search);
  query.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      search();
    }
  });
  document.getElementById("artYT").addEventListener("click", () => {
    if (!ytId) return;
    select(`https://i.ytimg.com/vi/${ytId}/maxresdefault.jpg`, "YouTube thumbnail");
  });
})();
</script>
"""


def manual_meta(form: dict, session: "ReviewSession") -> TrackMeta:
    """
    Build metadata from the manual entry form.

    Used for tracks no catalogue has: an unreleased or leaked song still gets
    proper tags, and its artwork is borrowed from whatever release you pick.
    """
    title = (form.get("title") or session.yt_title or "").strip()
    artist = (form.get("artist") or "").strip()
    album = (form.get("album") or "").strip()
    album_artist = (form.get("album_artist") or artist).strip()
    year = (form.get("year") or "").strip()[:4]
    genre = (form.get("genre") or "").strip()
    cover = (form.get("cover_url") or "").strip() or None

    try:
        track_number = int(form.get("track_number") or 0) or None
    except (TypeError, ValueError):
        track_number = None

    digest = hashlib.sha1(
        f"{normalise(title)}|{normalise(artist)}".encode("utf-8")
    ).hexdigest()[:16]

    return TrackMeta(
        source="manual",
        source_id=digest,
        title=title,
        artist=artist,
        artists=[artist] if artist else [],
        album=album,
        album_artist=album_artist,
        year=year,
        date=year,
        duration_s=float(session.duration_s or 0),
        track_number=track_number,
        cover_url=cover,
        genre=genre,
    )


def render(session: ReviewSession, cfg: Config, message: str = "") -> str:
    """Render the whole review page. Inline CSS, and JS only for the art picker."""
    ordered = session.candidates
    cards = "\n".join(_card(session, i, c) for i, c in enumerate(ordered))
    if not cards:
        cards = (
            '<p class="empty-note">No candidates found. Edit the title/artist below '
            "and search again.</p>"
        )

    errors = ""
    if session.errors:
        errors = (
            '<div class="notice">'
            + "<br>".join(ESC(e) for e in session.errors)
            + "</div>"
        )

    already = ""
    if session.already:
        already = (
            f'<div class="notice">Already archived at '
            f"<code>{ESC(str(session.already.get('path')))}</code> — "
            f"confirming will download it again.</div>"
        )

    thumb = (
        f'<img class="thumb" src="https://i.ytimg.com/vi/{ESC(session.video_id)}/mqdefault.jpg" alt="">'
        if session.video_id
        else ""
    )

    manual_block = f"""
  <details class="manual">
    <summary>Can&rsquo;t find it? Enter the metadata manually</summary>
    <form method="post" action="/review/{ESC(session.review_id)}/manual">
      <div class="grid2">
        <label>Title<input type="text" name="title" value="{ESC(session.yt_title)}"></label>
        <label>Artist<input type="text" name="artist" value="{ESC(session.yt_artist)}"></label>
        <label>Album<input type="text" name="album" value="{ESC(session.yt_album)}"></label>
        <label>Album artist<input type="text" name="album_artist" value="{ESC(session.yt_artist)}"></label>
        <label>Year<input type="text" name="year" value=""></label>
        <label>Track no.<input type="text" name="track_number" value=""></label>
        <label>Genre<input type="text" name="genre" value=""></label>
      </div>

      <div class="sub">Artwork &mdash; search by artist or album, or just borrow the YouTube thumbnail.</div>
      <input type="hidden" name="cover_url" id="coverUrl" value="">
      <div class="row">
        <input type="text" id="artQuery" value="{ESC(session.yt_artist)}" placeholder="search artwork...">
        <button type="button" id="artSearch">Search artwork</button>
        <button type="button" id="artYT">Use YouTube thumbnail</button>
      </div>
      <div id="artResults" class="artgrid"></div>
      <div id="artChosen" class="chosen"></div>

      <div class="row">
        <button type="submit" class="primary">Download with this metadata</button>
      </div>
    </form>
  </details>"""

    script = _ART_PICKER_JS.replace("__REVIEW_ID__", session.review_id).replace(
        "__YT_ID__", session.video_id or ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>invokr — confirm metadata</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px; background: #14161a; color: #e8eaed;
         font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }}
  h1 {{ font-size: 19px; margin: 0 0 4px; }}
  .sub {{ color: #9aa0a6; font-size: 13px; }}
  .wrap {{ max-width: 860px; margin: 0 auto; }}
  .source {{ display: flex; gap: 14px; align-items: center; padding: 14px;
             background: #1d2025; border: 1px solid #2b2f36; border-radius: 10px;
             margin: 18px 0; }}
  .thumb {{ width: 120px; border-radius: 6px; flex: none; }}
  .notice {{ background: #3a2a12; border: 1px solid #6b4a1c; padding: 10px 12px;
             border-radius: 8px; margin: 10px 0; font-size: 13px; }}
  .card {{ display: flex; gap: 12px; align-items: center; padding: 10px;
           background: #1a1d22; border: 1px solid #2b2f36; border-radius: 10px;
           margin-bottom: 8px; cursor: pointer; }}
  .card:hover {{ border-color: #4a515c; background: #1f232a; }}
  .card input {{ flex: none; width: 18px; height: 18px; accent-color: #7c4dff; }}
  .cover {{ width: 64px; height: 64px; border-radius: 6px; object-fit: cover; flex: none; }}
  .cover.empty {{ background: #2b2f36; }}
  .card-body {{ min-width: 0; flex: 1; }}
  .card-title {{ font-weight: 600; }}
  .card-meta {{ color: #9aa0a6; font-size: 12.5px; margin-top: 2px; }}
  .card-tags {{ margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }}
  .badge {{ font-size: 11px; padding: 2px 7px; border-radius: 999px;
            border: 1px solid #3a3f47; color: #c8ccd2; }}
  .badge.warn {{ border-color: #8a5a12; color: #f0b357; }}
  .badge.ok {{ border-color: #2f6b3a; color: #7fd18f; }}
  .badge.src-itunes {{ border-color: #5c4a7a; }}
  .badge.src-deezer {{ border-color: #7a4a5c; }}
  .row {{ display: flex; gap: 8px; margin: 14px 0; flex-wrap: wrap; }}
  input[type=text] {{ background: #1a1d22; border: 1px solid #2b2f36; color: #e8eaed;
                      padding: 8px 10px; border-radius: 8px; flex: 1; min-width: 200px; }}
  button {{ padding: 9px 16px; border-radius: 8px; border: 1px solid #2b2f36;
            background: #23272e; color: #e8eaed; cursor: pointer; font-size: 14px; }}
  button.primary {{ background: #7c4dff; border-color: #7c4dff; color: #fff; font-weight: 600; }}
  button:hover {{ filter: brightness(1.12); }}
  code {{ background: #23272e; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  .empty-note {{ color: #9aa0a6; }}
  .manual {{ margin-top: 24px; border-top: 1px solid #2b2f36; padding-top: 14px; }}
  .manual summary {{ cursor: pointer; color: #c8ccd2; font-size: 13.5px; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 14px 0; }}
  .grid2 label {{ font-size: 11.5px; color: #9aa0a6; }}
  .grid2 input {{ width: 100%; margin-top: 3px; }}
  .artgrid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(92px, 1fr));
              gap: 8px; margin-top: 10px; }}
  .artitem {{ padding: 0; border: 2px solid transparent; border-radius: 8px;
              background: none; cursor: pointer; overflow: hidden; line-height: 0; }}
  .artitem img {{ width: 100%; display: block; border-radius: 6px; }}
  .artitem:hover {{ border-color: #4a515c; }}
  .artitem.sel {{ border-color: #7c4dff; }}
  .chosen {{ display: flex; gap: 10px; align-items: center; margin-top: 10px; }}
  .chosen img {{ width: 56px; height: 56px; border-radius: 6px; object-fit: cover; }}
  .footer {{ color: #6b7078; font-size: 12px; margin-top: 26px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Confirm the metadata for this track</h1>
  <div class="sub">Audio always comes from the YouTube video you liked. Only the tags and cover art come from the catalogue.</div>

  <div class="source">
    {thumb}
    <div>
      <div><strong>{ESC(session.yt_title) or "(unknown title)"}</strong></div>
      <div class="sub">{ESC(session.yt_artist)}</div>
      <div class="sub">Album: {ESC(session.yt_album) or "?"} · Duration: {_fmt_duration(session.duration_s)} · Video: <code>{ESC(session.video_id)}</code></div>
    </div>
  </div>

  {already}
  {errors}
  {f'<div class="notice">{ESC(message)}</div>' if message else ""}

  <form method="post" action="/review/{ESC(session.review_id)}/confirm">
    {cards}
    <div class="row">
      <button type="submit" class="primary">Download selected</button>
      <button type="submit" formaction="/review/{ESC(session.review_id)}/skip">Skip</button>
    </div>
  </form>

  <form method="post" action="/review/{ESC(session.review_id)}/research">
    <div class="sub">Wrong song or missing from the list? Adjust and search again.</div>
    <div class="row">
      <input type="text" name="title" value="{ESC(session.yt_title)}" placeholder="title">
      <input type="text" name="artist" value="{ESC(session.yt_artist)}" placeholder="artist">
      <button type="submit">Search again</button>
    </div>
  </form>

  {manual_block}

  <div class="footer">Session expires in {int(max(0, session.expires_in(cfg.review_ttl_minutes)) // 60)} min · single use</div>
</div>
{script}
</body>
</html>"""


def render_done(cfg: Config, *, title: str, body: str, job_id: str | None = None) -> str:
    job_line = f'<div class="sub">Job: <code>{ESC(job_id)}</code></div>' if job_id else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>invokr</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; padding: 40px 24px; background: #14161a; color: #e8eaed;
         font: 15px/1.6 system-ui, -apple-system, Segoe UI, sans-serif; }}
  .wrap {{ max-width: 640px; margin: 0 auto; }}
  h1 {{ font-size: 20px; }}
  .sub {{ color: #9aa0a6; font-size: 13px; }}
  code {{ background: #23272e; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  a {{ color: #9d7cff; }}
</style></head>
<body><div class="wrap">
  <h1>{ESC(title)}</h1>
  <div>{body}</div>
  {job_line}
  <p class="sub">You can close this tab. Downloads run in the background.</p>
</div></body></html>"""
