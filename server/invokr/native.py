"""
Native download backend: yt-dlp for audio, mutagen for tags.

This replaces spotDL for the download step. The reason is concrete rather than
aesthetic:

* spotDL's ``reinit_song()`` calls ``Song.from_url()`` for every track, so its
  download path hard-requires the Spotify API.
* spotDL's bundled Spotify application returned ``HTTP 429, Retry-After: 86400``.
* spotDL exits ``0`` even when a download fails, so success has to be inferred
  from the filesystem anyway.

Doing it ourselves means real exceptions, no hidden Spotify dependency, and the
file on disk is the source of truth.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import requests
import yt_dlp
from mutagen.mp4 import MP4, MP4Cover

from .catalog import TrackMeta, clean_title
from .config import Config

# yt-dlp format selection. On YouTube Music the free tier tops out around
# 130 kbps: format 140 is m4a/AAC and 251 is webm/opus. Prefer m4a so the file
# needs no container conversion.
FORMAT_M4A = "bestaudio[ext=m4a]/bestaudio/best"
FORMAT_WEBM = "bestaudio[ext=webm]/bestaudio/best"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_COLLAPSE = re.compile(r"\s+")
MAX_COMPONENT = 120


class DownloadError(RuntimeError):
    """Raised when a track could not be downloaded or tagged."""


@dataclass
class DownloadResult:
    path: Path
    bytes: int
    video_id: str
    title: str
    ext: str


# ------------------------------------------------------------------ naming


def sanitise(component: str, fallback: str = "unknown") -> str:
    """Make one path component safe for Windows."""
    cleaned = _ILLEGAL.sub("_", component or "")
    cleaned = _COLLAPSE.sub(" ", cleaned).strip().rstrip(". ")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > MAX_COMPONENT:
        cleaned = cleaned[:MAX_COMPONENT].rstrip(". ")
    return cleaned


def render_output_path(cfg: Config, meta: TrackMeta, ext: str, video_id: str) -> Path:
    """
    Expand the configured template into a concrete path.

    Unknown variables are left untouched so a typo shows up in the filename
    rather than silently producing an empty path.
    """
    mapping = {
        "artist": meta.artist or "unknown artist",
        "artists": ", ".join(meta.artists) if meta.artists else (meta.artist or "unknown artist"),
        "album-artist": meta.album_artist or meta.artist or "unknown artist",
        "album": meta.album or "unknown album",
        "title": meta.title or "unknown title",
        "track-number": f"{meta.track_number:02d}" if meta.track_number else "00",
        "track-count": str(meta.track_total or ""),
        "disc-number": str(meta.disc_number or ""),
        "year": meta.year or "",
        "isrc": meta.isrc or "",
        "source-id": meta.source_id,
        "video-id": video_id,
        "output-ext": ext,
    }

    rendered = cfg.output_template
    for key, value in mapping.items():
        rendered = rendered.replace("{" + key + "}", sanitise(value, "unknown"))

    # Sanitise each path segment independently so separators survive.
    parts = [sanitise(part) for part in re.split(r"[\\/]+", rendered) if part]
    if not parts:
        raise DownloadError("output template produced an empty path")
    return cfg.library_path.joinpath(*parts)


# ---------------------------------------------------------------- cover art


def _fetch_cover(url: str | None) -> tuple[bytes, int] | None:
    """Return (bytes, MP4Cover format constant) or None."""
    if not url:
        return None
    try:
        response = requests.get(url, timeout=30, headers={"User-Agent": "invokr/0.1"})
        response.raise_for_status()
        blob = response.content
    except requests.RequestException:
        return None

    if blob.startswith(b"\xff\xd8\xff"):
        return blob, MP4Cover.FORMAT_JPEG
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return blob, MP4Cover.FORMAT_PNG
    return None


# ------------------------------------------------------------------ tagging


def tag_file(path: Path, meta: TrackMeta, *, yt_url: str, cover: tuple[bytes, int] | None) -> None:
    """Write iTunes-style metadata into an MP4/M4A container."""
    audio = MP4(path)

    if meta.title:
        audio["\xa9nam"] = [meta.title]
    if meta.artist:
        audio["\xa9ART"] = [meta.artist]
    if meta.album_artist or meta.artist:
        audio["aART"] = [meta.album_artist or meta.artist]
    if meta.album:
        audio["\xa9alb"] = [meta.album]
    if meta.date or meta.year:
        audio["\xa9day"] = [meta.date or meta.year]
    if meta.genre:
        audio["\xa9gen"] = [meta.genre]
    if meta.track_number:
        audio["trkn"] = [(meta.track_number, meta.track_total or 0)]
    if meta.disc_number:
        audio["disk"] = [(meta.disc_number, meta.disc_total or 0)]
    if meta.isrc:
        audio["----:com.apple.iTunes:ISRC"] = [meta.isrc.encode("utf-8")]
    # Keep provenance: where the audio came from and which catalogue described it.
    audio["\xa9cmt"] = [f"{yt_url} | {meta.source}:{meta.source_id}"]
    if meta.url:
        audio["----:com.apple.iTunes:SOURCE"] = [meta.url.encode("utf-8")]

    if cover:
        blob, fmt = cover
        audio["covr"] = [MP4Cover(blob, imageformat=fmt)]

    audio.save()


# --------------------------------------------------------------- downloading


def download(
    cfg: Config,
    *,
    yt_url: str,
    meta: TrackMeta,
    video_id: str = "",
    expect_ext: str | None = None,
) -> DownloadResult:
    """
    Download the exact YouTube video and tag it with catalogue metadata.

    ``yt_url`` is the link the user actually liked, so there is no search step
    and no chance of matching the wrong recording.
    """
    cfg.ensure_directories()
    # A temp dir inside the library keeps the final move on the same volume,
    # so it is a rename rather than a copy.
    work_dir = cfg.library_path / ".invokr-tmp"
    work_dir.mkdir(parents=True, exist_ok=True)

    ext_pref = (expect_ext or cfg.format or "m4a").lower()
    fmt = FORMAT_M4A if ext_pref == "m4a" else FORMAT_WEBM

    ydl_opts: dict = {
        "format": fmt,
        "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "socket_timeout": 30,
    }
    if cfg.cookie_file:
        ydl_opts["cookiefile"] = cfg.cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(yt_url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(f"yt-dlp could not download {yt_url}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"yt-dlp failed for {yt_url}: {type(exc).__name__}: {exc}") from exc

    if not info:
        raise DownloadError(f"yt-dlp returned no metadata for {yt_url}")

    video_id = video_id or info.get("id") or ""
    downloaded = sorted(work_dir.glob(f"{info.get('id')}.*"))
    downloaded = [p for p in downloaded if not p.name.endswith(".part")]
    if not downloaded:
        raise DownloadError(f"yt-dlp reported success but wrote no file for {yt_url}")

    temp_file = downloaded[0]
    ext = temp_file.suffix.lstrip(".").lower()

    target = render_output_path(cfg, meta, ext, video_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Tag while still in the temp dir so a partially tagged file never appears
    # in the library.
    cover = _fetch_cover(meta.cover_url)
    if ext in {"m4a", "mp4", "m4b"}:
        tag_file(temp_file, meta, yt_url=yt_url, cover=cover)

    shutil.move(str(temp_file), str(target))

    size = target.stat().st_size
    if size <= 0:
        target.unlink(missing_ok=True)
        raise DownloadError(f"downloaded file is empty: {target}")

    return DownloadResult(
        path=target,
        bytes=size,
        video_id=video_id,
        title=meta.title,
        ext=ext,
    )


_VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
_ytmusic_client_cache = None


def video_id_from_url(url: str) -> str:
    match = _VIDEO_ID.search(url or "")
    return match.group(1) if match else ""


def _ytmusic_client():
    """Lazily build a ytmusicapi client. It already ships as a spotDL dependency."""
    global _ytmusic_client_cache
    if _ytmusic_client_cache is None:
        from ytmusicapi import YTMusic

        _ytmusic_client_cache = YTMusic()
    return _ytmusic_client_cache


def probe_metadata(yt_url: str, cfg: Config | None = None, video_id: str = "") -> dict:
    """
    Resolve title/artist/duration from YouTube, without downloading.

    Needed because a like made from a playlist row or the "…" menu arrives with
    nothing but a video id: the player bar is showing some other track.

    YouTube Music's own API is preferred because it returns *structured* data —
    ``title='Take On Me'``, ``author='a-ha'`` — whereas yt-dlp hands back the raw
    video title, ``'a-ha - Take On Me (Official Video) [4K]'``, which searches a
    catalogue badly.
    """
    vid = video_id or video_id_from_url(yt_url)

    if vid:
        try:
            client = _ytmusic_client()
            details = (client.get_song(vid) or {}).get("videoDetails") or {}
            title = details.get("title") or ""
            if title:
                return {
                    "videoId": vid,
                    "title": title,
                    "author": details.get("author") or "",
                    "album": "",
                    "duration": int(details.get("lengthSeconds") or 0),
                }
        except Exception:  # noqa: BLE001 - fall through to yt-dlp
            pass

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
    }
    if cfg is not None and cfg.cookie_file:
        opts["cookiefile"] = cfg.cookie_file

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(yt_url, download=False)
    except Exception as exc:  # noqa: BLE001 - caller falls back to an empty title
        raise DownloadError(f"could not resolve metadata for {yt_url}: {exc}") from exc

    if not info:
        raise DownloadError(f"no metadata returned for {yt_url}")

    author = info.get("artist") or info.get("creator") or info.get("uploader") or ""
    raw_title = info.get("track") or info.get("title") or ""

    return {
        "videoId": info.get("id") or vid,
        "title": clean_title(raw_title, author),
        "author": author,
        "album": info.get("album") or "",
        "duration": info.get("duration") or 0,
    }


def cleanup(cfg: Config) -> int:
    """Remove leftovers from interrupted downloads. Returns files removed."""
    work_dir = cfg.library_path / ".invokr-tmp"
    if not work_dir.exists():
        return 0
    removed = 0
    for item in work_dir.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed += 1
        except OSError:
            continue
    return removed
