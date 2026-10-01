"""
Read the tags of files that are already on disk and register them.

The point is to stop the extension re-downloading things you already have. Two
sources of files matter:

* **Files this tool wrote** — the comment tag holds ``<yt url> | <source>:<id>``,
  so both identity and provenance are recoverable exactly.
* **Files spotDL wrote** — spotDL puts the YouTube URL in the comment tag and the
  Spotify URL in a ``WOAS`` freeform atom, which is enough to recover the video
  id and a Spotify track id.

Anything else falls back to filename parsing, and is keyed by a hash of the
normalised title and artist so it still suppresses a re-download.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from .catalog import normalise

AUDIO_SUFFIXES = {".m4a", ".mp4", ".mp3", ".flac", ".ogg", ".opus", ".wav"}

VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
SPOTIFY_ID_RE = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]{22})")
PROVENANCE_RE = re.compile(r"\|\s*([a-z]+):([A-Za-z0-9_\-:.]+)\s*$")
FILENAME_RE = re.compile(r"^\s*(?P<artist>.+?)\s+-\s+(?P<title>.+?)\s*$")


@dataclass
class LibraryRecord:
    path: Path
    title: str = ""
    artist: str = ""
    artists: list[str] = field(default_factory=list)
    album: str = ""
    album_artist: str = ""
    year: str = ""
    date: str = ""
    genre: str = ""
    duration_s: int | None = None
    track_number: int | None = None
    disc_number: int | None = None
    video_id: str | None = None
    catalogue_key: str | None = None
    has_cover: bool = False
    source_hint: str = ""

    @property
    def key(self) -> str:
        """Stable identity for the tracks table."""
        if self.catalogue_key:
            return self.catalogue_key
        digest = hashlib.sha1(
            f"{normalise(self.title)}|{normalise(self.artist)}".encode("utf-8")
        ).hexdigest()[:16]
        return f"local:{digest}"

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "year": self.year,
            "duration_s": self.duration_s,
            "video_id": self.video_id,
            "key": self.key,
            "has_cover": self.has_cover,
            "source_hint": self.source_hint,
        }


def _first(value) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


def _read_mp4(audio: MP4) -> dict:
    tags = audio.tags or {}
    cover = tags.get("covr")
    freeform = {}
    for key in tags:
        if isinstance(key, str) and key.startswith("----:"):
            try:
                freeform[key] = _first(tags[key])
            except Exception:  # noqa: BLE001
                pass

    track = tags.get("trkn") or [(0, 0)]
    disc = tags.get("disk") or [(0, 0)]
    return {
        "title": _first(tags.get("\xa9nam")),
        "artist": _first(tags.get("\xa9ART")),
        "album_artist": _first(tags.get("aART")),
        "album": _first(tags.get("\xa9alb")),
        "date": _first(tags.get("\xa9day")),
        "genre": _first(tags.get("\xa9gen")),
        "comment": _first(tags.get("\xa9cmt")),
        "track_number": track[0][0] or None,
        "disc_number": disc[0][0] or None,
        "has_cover": bool(cover),
        "spotify_url": freeform.get("----:spotdl:WOAS")
        or freeform.get("----:com.apple.iTunes:SPOTIFY", ""),
    }


def _read_id3(tags: ID3) -> dict:
    def frame(name):
        value = tags.get(name)
        return str(value) if value is not None else ""

    comment = ""
    for key in tags:
        if key.startswith("COMM"):
            comment = str(tags[key])
            break

    spotify_url = ""
    for key in tags:
        if key.startswith("TXXX") and ("WOAS" in key or "SPOTIFY" in key.upper()):
            spotify_url = str(tags[key])
            break

    track_number = None
    raw_track = frame("TRCK")
    if raw_track:
        try:
            track_number = int(raw_track.split("/")[0])
        except ValueError:
            track_number = None

    disc_number = None
    raw_disc = frame("TPOS")
    if raw_disc:
        try:
            disc_number = int(raw_disc.split("/")[0])
        except ValueError:
            disc_number = None

    return {
        "title": frame("TIT2"),
        "artist": frame("TPE1"),
        "album_artist": frame("TPE2"),
        "album": frame("TALB"),
        "date": frame("TDRC") or frame("TYER"),
        "genre": frame("TCON"),
        "comment": comment,
        "track_number": track_number,
        "disc_number": disc_number,
        "has_cover": any(key.startswith("APIC") for key in tags),
        "spotify_url": spotify_url,
    }


def _read_vorbis(tags) -> dict:
    def get(*names):
        for name in names:
            if name in tags:
                return _first(tags[name])
        return ""

    comment = get("comment", "description")
    spotify_url = get("woas", "spotify_url")
    track_number = None
    raw_track = get("tracknumber")
    if raw_track:
        try:
            track_number = int(raw_track.split("/")[0])
        except ValueError:
            track_number = None

    return {
        "title": get("title"),
        "artist": get("artist"),
        "album_artist": get("albumartist", "album artist"),
        "album": get("album"),
        "date": get("date", "year"),
        "genre": get("genre"),
        "comment": comment,
        "track_number": track_number,
        "disc_number": None,
        "has_cover": bool(get("metadata_block_picture")) or False,
        "spotify_url": spotify_url,
    }


def read_file(path: Path) -> LibraryRecord:
    """Read whatever identity a file carries. Never raises for missing tags."""
    record = LibraryRecord(path=path)

    audio = MutagenFile(str(path))
    if audio is not None:
        try:
            if audio.info and getattr(audio.info, "length", None):
                record.duration_s = int(round(audio.info.length))
        except Exception:  # noqa: BLE001
            pass

        raw: dict = {}
        try:
            if isinstance(audio, MP4):
                raw = _read_mp4(audio)
            elif isinstance(audio, (FLAC, OggVorbis, OggOpus)):
                raw = _read_vorbis(audio.tags or {})
            elif audio.tags is not None and isinstance(audio.tags, ID3):
                raw = _read_id3(audio.tags)
            elif audio.tags is not None:
                raw = _read_vorbis(audio.tags)
        except Exception:  # noqa: BLE001
            raw = {}

        record.title = raw.get("title") or ""
        record.artist = raw.get("artist") or ""
        record.album_artist = raw.get("album_artist") or ""
        record.album = raw.get("album") or ""
        record.date = (raw.get("date") or "")[:10]
        record.year = (raw.get("date") or "")[:4]
        record.genre = raw.get("genre") or ""
        record.track_number = raw.get("track_number")
        record.disc_number = raw.get("disc_number")
        record.has_cover = bool(raw.get("has_cover"))

        comment = raw.get("comment") or ""
        spotify_url = raw.get("spotify_url") or ""

        video = VIDEO_ID_RE.search(comment)
        if video:
            record.video_id = video.group(1)

        # Our own files hold "<yt url> | <source>:<id>" in the comment.
        provenance = PROVENANCE_RE.search(comment)
        if provenance:
            record.catalogue_key = f"{provenance.group(1)}:{provenance.group(2)}"
            record.source_hint = provenance.group(1)

        # spotDL files hold the Spotify URL in a WOAS atom.
        if not record.catalogue_key and spotify_url:
            spotify = SPOTIFY_ID_RE.search(spotify_url)
            if spotify:
                record.catalogue_key = f"spotify:{spotify.group(1)}"
                record.source_hint = "spotdl"

        if not record.video_id and spotify_url:
            video = VIDEO_ID_RE.search(spotify_url)
            if video:
                record.video_id = video.group(1)

    # Last resort: "Artist - Title.m4a"
    if not record.title or not record.artist:
        stem = path.stem
        match = FILENAME_RE.match(stem)
        if match:
            record.artist = record.artist or match.group("artist").strip()
            record.title = record.title or match.group("title").strip()
        else:
            record.title = record.title or stem

    if not record.album_artist:
        record.album_artist = record.artist
    record.artists = [record.artist] if record.artist else []

    return record


def scan(root: Path, *, recursive: bool = True, limit: int | None = None) -> list[LibraryRecord]:
    """Read every audio file under a directory."""
    root = Path(root)
    if not root.exists():
        return []

    pattern = "**/*" if recursive else "*"
    records: list[LibraryRecord] = []
    for path in sorted(root.glob(pattern)):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        if ".invokr-tmp" in path.parts:
            continue
        records.append(read_file(path))
        if limit is not None and len(records) >= limit:
            break
    return records


ADDED_SAMPLE_LIMIT = 500
UNREADABLE_SAMPLE_LIMIT = 50


@dataclass
class ImportReport:
    root: str
    dry_run: bool
    scanned: int = 0
    added: list[dict] = field(default_factory=list)
    already_known_count: int = 0
    unreadable: list[dict] = field(default_factory=list)
    unreadable_count: int = 0

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "added_count": len(self.added),
            "added": self.added[:ADDED_SAMPLE_LIMIT],
            "already_known_count": self.already_known_count,
            "unreadable_count": self.unreadable_count,
            "unreadable": self.unreadable[:UNREADABLE_SAMPLE_LIMIT],
        }


def import_records(records: list[LibraryRecord], *, dry_run: bool = False, root: str = "") -> ImportReport:
    """
    Register already-downloaded files so the extension stops fetching them.

    Idempotent: a file already recorded as done is reported as known and left
    alone, so this is safe to re-run over the whole collection.
    """
    from . import db

    report = ImportReport(root=root, dry_run=dry_run, scanned=len(records))

    for record in records:
        if not record.title and not record.video_id:
            report.unreadable_count += 1
            report.unreadable.append({"path": str(record.path), "reason": "no title or video id"})
            continue

        existing = db.track_by_path(str(record.path))
        if existing is None and record.video_id:
            existing = db.find_track_by_video(record.video_id)
        if existing is not None and existing.get("status") == "done":
            report.already_known_count += 1
            continue

        entry = record.to_dict()
        report.added.append(entry)

        if dry_run:
            continue

        db.register_imported(
            key=record.key,
            path=str(record.path),
            title=record.title or None,
            artists=", ".join(record.artists) or None,
            album=record.album or None,
            video_id=record.video_id,
            yt_url=f"https://music.youtube.com/watch?v={record.video_id}" if record.video_id else None,
            duration_s=record.duration_s,
            detail={
                "imported": True,
                "source_hint": record.source_hint,
                "has_cover": record.has_cover,
                "year": record.year,
            },
        )
        db.record_event(
            "library_imported",
            video_id=record.video_id,
            spotify_id=record.key,
            detail={"path": str(record.path), "title": record.title, "artist": record.artist},
        )

    return report
