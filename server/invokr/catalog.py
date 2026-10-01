"""
Music catalogue lookups.

Two providers, chosen because neither needs an account:

* **iTunes** — returns track number, track count, disc numbers, release date,
  genre, duration and cover art in a single call.
* **Deezer** — 1000x1000 covers, and an ISRC via ``/track/{id}``.

Spotify was removed. It cannot work here: spotDL's bundled shared application
returns ``HTTP 429, Retry-After: 86400``, and a replacement development-mode app
requires the owner to hold Spotify Premium since February 2026. It also bought
nothing — iTunes and Deezer carry the same fields.

Every provider is normalised into :class:`TrackMeta` and scored centrally, so
the review panel can rank results from all of them together.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

import requests
from rapidfuzz import fuzz

from .config import SERVER_DIR, Config

USER_AGENT = "invokr/0.1 (personal music archiver)"

_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

# Words that appear in YouTube Music titles but not in catalogue track names.
_NOISE = {
    "official", "video", "audio", "lyric", "lyrics", "visualizer", "hd", "hq",
    "explicit", "remaster", "remastered", "mv", "m/v", "4k",
}


def normalise(text: str) -> str:
    """
    Fold a title or artist name into a comparable form for *lenient* matching.

    Bracketed qualifiers are dropped, so "Take On Me (MTV Unplugged)" becomes
    "take on me". Use :func:`normalise_strict` when the qualifier matters.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = _BRACKETED.sub(" ", text)
    text = text.replace("&", " and ")
    text = _NON_WORD.sub(" ", text)
    words = [w for w in _WHITESPACE.split(text) if w and w not in _NOISE]
    return " ".join(words)


def normalise_strict(text: str) -> str:
    """
    Normalise but keep bracketed qualifiers, so "(Live)" and "(Remix)" survive.

    Pairing this with a strict ratio is what stops a live or remixed version
    from outranking the studio recording it happens to sound identical to.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("&", " and ")
    text = _NON_WORD.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


_SEPARATORS = (" - ", " – ", " — ", " ~ ", " | ")


def clean_title(title: str, artist: str = "") -> str:
    """
    Turn a YouTube video title into something worth searching a catalogue for.

    YouTube titles look like ``a-ha - Take On Me (Official Video) [4K]``. The
    artist prefix is only stripped when it actually matches the known artist, so
    legitimate titles containing a dash survive.
    """
    if not title:
        return ""

    cleaned = unicodedata.normalize("NFKC", title)

    if artist:
        lowered = cleaned.lower()
        for separator in _SEPARATORS:
            prefix = (artist + separator).lower()
            if lowered.startswith(prefix):
                cleaned = cleaned[len(prefix) :]
                break

    cleaned = _BRACKETED.sub(" ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" -–—~|")
    return cleaned or title


@dataclass
class TrackMeta:
    """A single catalogue result, normalised across providers."""

    source: str
    source_id: str
    title: str
    artist: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    album_artist: str = ""
    year: str = ""
    date: str = ""
    duration_s: float = 0.0
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    cover_url: str | None = None
    url: str | None = None
    isrc: str | None = None
    explicit: bool = False
    genre: str = ""
    provider_rank: int = 0

    # --- scoring, filled in by score_candidates ---------------------------
    title_score: float = 0.0
    artist_score: float = 0.0
    duration_delta_s: float = 0.0
    duration_ok: bool = True
    score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------- http


def _get_json(url: str, params: dict | None = None, timeout: int = 20) -> dict:
    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


# ------------------------------------------------------------------- iTunes


class ITunesProvider:
    """Apple's public search API. No account, one call, very complete."""

    name = "itunes"
    ENDPOINT = "https://itunes.apple.com/search"

    def search(self, title: str, artist: str = "", limit: int = 10) -> list[TrackMeta]:
        term = f"{artist} {title}".strip() if artist else title
        try:
            payload = _get_json(
                self.ENDPOINT,
                {"term": term, "entity": "song", "limit": limit, "country": "US"},
            )
        except requests.RequestException:
            return []

        results: list[TrackMeta] = []
        for index, item in enumerate(payload.get("results") or []):
            if item.get("wrapperType") != "track" or item.get("kind") != "song":
                continue
            results.append(self._to_meta(item, index))
        return results

    @staticmethod
    def _to_meta(item: dict, index: int) -> TrackMeta:
        artist_name = item.get("artistName") or ""
        return TrackMeta(
            source="itunes",
            source_id=str(item.get("trackId") or ""),
            title=item.get("trackName") or "",
            artist=artist_name,
            artists=[artist_name] if artist_name else [],
            album=item.get("collectionName") or "",
            # iTunes only exposes collectionArtistName on some storefronts.
            album_artist=item.get("collectionArtistName") or artist_name,
            year=(item.get("releaseDate") or "")[:4],
            date=(item.get("releaseDate") or "")[:10],
            duration_s=round((item.get("trackTimeMillis") or 0) / 1000, 1),
            track_number=item.get("trackNumber"),
            track_total=item.get("trackCount"),
            disc_number=item.get("discNumber"),
            disc_total=item.get("discCount"),
            cover_url=upscale_itunes_artwork(item.get("artworkUrl100")),
            url=item.get("trackViewUrl"),
            explicit=item.get("trackExplicitness") == "explicit",
            genre=item.get("primaryGenreName") or "",
            provider_rank=index,
        )


def upscale_itunes_artwork(url: str | None, size: str = "1000x1000bb") -> str | None:
    """
    iTunes serves artwork at a fixed size encoded in the URL path.

    ``.../100x100bb.jpg`` can simply be rewritten; verified to return a real
    1000x1000 JPEG (HTTP 200, 199 KB) rather than a 404.
    """
    if not url:
        return None
    return re.sub(r"\d+x\d+bb", size, url)


class ArtworkResult:
    """A single cover-art candidate for the manual metadata picker."""

    def __init__(self, *, url: str, thumb: str, album: str, artist: str, source: str, year: str = ""):
        self.url = url
        self.thumb = thumb
        self.album = album
        self.artist = artist
        self.source = source
        self.year = year

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "thumb": self.thumb,
            "album": self.album,
            "artist": self.artist,
            "source": self.source,
            "year": self.year,
        }


def search_artwork(query: str, limit: int = 12) -> list[ArtworkResult]:
    """
    Search for cover art by album or artist name.

    Needed for tracks that exist in no catalogue — an unreleased or leaked song
    still deserves artwork, and the practical answer is to borrow the art from
    another release by the same artist.
    """
    results: list[ArtworkResult] = []
    if not query or not query.strip():
        return results

    # iTunes: entity=album returns collection-level artwork.
    try:
        payload = _get_json(
            "https://itunes.apple.com/search",
            {"term": query, "entity": "album", "limit": limit, "country": "US"},
        )
        for item in payload.get("results") or []:
            art = item.get("artworkUrl100")
            if not art:
                continue
            results.append(
                ArtworkResult(
                    url=upscale_itunes_artwork(art, "1000x1000bb") or art,
                    thumb=upscale_itunes_artwork(art, "300x300bb") or art,
                    album=item.get("collectionName") or "",
                    artist=item.get("artistName") or "",
                    source="itunes",
                    year=(item.get("releaseDate") or "")[:4],
                )
            )
    except requests.RequestException:
        pass

    # Deezer: /search/album, 1000x1000 covers available directly.
    try:
        payload = _get_json(
            "https://api.deezer.com/search/album", {"q": query, "limit": limit}
        )
        for item in payload.get("data") or []:
            cover = item.get("cover_xl") or item.get("cover_big")
            if not cover:
                continue
            results.append(
                ArtworkResult(
                    url=cover,
                    thumb=item.get("cover_medium") or cover,
                    album=item.get("title") or "",
                    artist=(item.get("artist") or {}).get("name") or "",
                    source="deezer",
                )
            )
    except requests.RequestException:
        pass

    return _rank_artwork(results, query, limit * 2)


def _rank_artwork(
    results: list[ArtworkResult], query: str, limit: int
) -> list[ArtworkResult]:
    """
    Rank covers, preferring the artist's own releases.

    A plain search for an artist name is dominated by "feat." collaborations, so
    an exact artist match has to outrank a merely relevant one. Duplicate covers
    (the same album on both iTunes and Deezer) collapse to the first.
    """
    want = normalise(query)
    scored: list[tuple[float, ArtworkResult]] = []
    for item in results:
        exact_artist = 1.0 if normalise(item.artist) == want else 0.0
        exact_album = 1.0 if normalise(item.album) == want else 0.0
        score = (
            3.0 * exact_artist
            + 1.5 * exact_album
            + 0.6 * fuzz.token_set_ratio(want, normalise(item.artist))
            + 0.2 * fuzz.token_set_ratio(want, normalise(item.album))
        )
        scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    seen: set[str] = set()
    ranked: list[ArtworkResult] = []
    for _, item in scored:
        if item.url in seen:
            continue
        seen.add(item.url)
        ranked.append(item)
        if len(ranked) >= limit:
            break
    return ranked



# ------------------------------------------------------------------- Deezer


class DeezerProvider:
    """Deezer's public API. No account, excellent cover art, has ISRC."""

    name = "deezer"
    ENDPOINT = "https://api.deezer.com/search"

    def search(self, title: str, artist: str = "", limit: int = 10) -> list[TrackMeta]:
        query = f"{artist} {title}".strip() if artist else title
        try:
            payload = _get_json(self.ENDPOINT, {"q": query, "limit": limit})
        except requests.RequestException:
            return []

        results: list[TrackMeta] = []
        for index, item in enumerate(payload.get("data") or []):
            meta = self._to_meta(item, index)
            if meta is not None:
                results.append(meta)
        return results

    @staticmethod
    def _to_meta(item: dict, index: int) -> TrackMeta | None:
        title = item.get("title")
        if not title:
            return None
        artist_name = (item.get("artist") or {}).get("name") or ""
        album = item.get("album") or {}
        return TrackMeta(
            source="deezer",
            source_id=str(item.get("id") or ""),
            title=title,
            artist=artist_name,
            artists=[artist_name] if artist_name else [],
            album=album.get("title") or "",
            album_artist=artist_name,
            duration_s=float(item.get("duration") or 0),
            cover_url=album.get("cover_xl") or album.get("cover_big"),
            url=item.get("link"),
            explicit=bool(item.get("explicit_lyrics")),
            provider_rank=index,
        )

    def enrich(self, meta: TrackMeta) -> TrackMeta:
        """
        Fetch track position, disc number, release date and ISRC.

        Deezer's search endpoint omits these, so this costs one extra request
        and is only worth doing for the result the user actually picks.
        """
        if meta.source != "deezer" or not meta.source_id:
            return meta
        try:
            detail = _get_json(f"https://api.deezer.com/track/{meta.source_id}")
        except requests.RequestException:
            return meta

        album = detail.get("album") or {}
        meta.isrc = detail.get("isrc") or meta.isrc
        meta.track_number = detail.get("track_position") or meta.track_number
        meta.disc_number = detail.get("disk_number") or meta.disc_number
        meta.track_total = album.get("nb_tracks") or meta.track_total
        released = detail.get("release_date") or ""
        meta.date = meta.date or released
        meta.year = meta.year or released[:4]
        meta.genre = meta.genre or ((detail.get("genres") or {}).get("data") or [{}])[0].get("name", "")
        return meta


# ----------------------------------------------------------------- catalog


def score_candidates(
    candidates: list[TrackMeta], title: str, artists: list[str], duration_s: float | None
) -> list[TrackMeta]:
    """Fill in scores, best first."""
    want_title = normalise(title)
    want_title_strict = normalise_strict(title)
    want_artists = [normalise(a) for a in artists if a]

    for cand in candidates:
        # token_set_ratio alone is too forgiving about extra words. Compare on
        # two axes: loosely (ignoring qualifiers) and strictly (keeping them),
        # so "Take On Me (MTV Unplugged)" scores below plain "Take On Me".
        cand.title_score = round(
            0.6 * fuzz.token_set_ratio(want_title, normalise(cand.title))
            + 0.4 * fuzz.ratio(want_title_strict, normalise_strict(cand.title)),
            1,
        )

        got_artists = [normalise(a) for a in (cand.artists or [cand.artist]) if a]
        cand.artist_score = max(
            (
                float(fuzz.token_set_ratio(want, got))
                for want in want_artists
                for got in got_artists
            ),
            default=0.0,
        )

        duration_bonus = 0.0
        if duration_s:
            cand.duration_delta_s = round(cand.duration_s - duration_s, 1)
            tolerance = max(10.0, 0.08 * duration_s)
            cand.duration_ok = abs(cand.duration_delta_s) <= tolerance
            drift = abs(cand.duration_delta_s)
            duration_bonus = max(0.0, 15.0 * (1 - drift / tolerance))

        # Provider order breaks ties: each provider returns its own best match
        # first, so a lower rank index should win when all else is equal.
        rank_bonus = max(0.0, 6.0 - cand.provider_rank)

        # A tribute/cover version has the right title and duration but the wrong
        # artist. Sink those rather than letting them outrank real results.
        artist_penalty = 15.0 if cand.artist_score < 50 else 0.0

        cand.score = round(
            0.50 * cand.title_score
            + 0.27 * cand.artist_score
            + duration_bonus
            + rank_bonus
            - artist_penalty,
            1,
        )

    candidates.sort(key=lambda c: (c.duration_ok, c.score), reverse=True)
    return candidates


def _dedupe(candidates: list[TrackMeta], order: list[str]) -> list[TrackMeta]:
    """
    Collapse the same song returned by several providers.

    Re-sorts on the way out: when a higher-priority provider replaces an entry
    for the same key, the replacement keeps the earlier position, which would
    otherwise leave a lower score sitting above a higher one.
    """
    best: dict[tuple[str, str, int], TrackMeta] = {}
    for cand in candidates:
        # Round the duration so 213.0 and 213.5 collapse together.
        key = (normalise(cand.title), normalise(cand.artist), round(cand.duration_s / 5))
        existing = best.get(key)
        if existing is None:
            best[key] = cand
            continue
        existing_rank = order.index(existing.source) if existing.source in order else 99
        cand_rank = order.index(cand.source) if cand.source in order else 99
        if cand_rank < existing_rank:
            best[key] = cand
    return sorted(
        best.values(), key=lambda c: (c.duration_ok, c.score), reverse=True
    )


# --------------------------------------------------------------- decisions


@dataclass
class Decision:
    """Whether the top match is trustworthy enough to skip the review step."""

    verdict: str  # "auto" | "review" | "none"
    reason: str
    chosen: TrackMeta | None = None
    runner_up_score: float | None = None

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "runnerUpScore": self.runner_up_score,
        }


def decide(candidates: list[TrackMeta], *, cfg: Config) -> Decision:
    """
    Decide whether the best match is obviously correct.

    Auto-accepting is only safe when every signal agrees. The margin over the
    runner-up does the heavy lifting: "Take On Me" and "Take On Me (MTV
    Unplugged)" both match the title perfectly and have compatible durations,
    so only the score gap reveals that a human should choose.
    """
    if not candidates:
        return Decision("none", "no catalogue results")

    if not cfg.auto_accept:
        return Decision("review", "auto-accept is switched off", candidates[0])

    top = candidates[0]

    if not top.duration_ok:
        return Decision(
            "review", f"duration differs by {top.duration_delta_s:+.0f}s", top
        )
    if top.title_score < cfg.auto_accept_min_title:
        return Decision("review", f"title match only {top.title_score:.0f}", top)
    if top.artist_score < cfg.auto_accept_min_artist:
        return Decision("review", f"artist match only {top.artist_score:.0f}", top)
    if top.score < cfg.auto_accept_min_score:
        return Decision("review", f"score only {top.score:.0f}", top)

    if len(candidates) > 1:
        runner = candidates[1]
        margin = round(top.score - runner.score, 1)
        if margin < cfg.auto_accept_min_margin:
            return Decision(
                "review",
                f"too close to call — {top.score} vs {runner.score} "
                f"for \"{runner.title}\"",
                top,
                runner.score,
            )

    return Decision("auto", "unambiguous match", top)


class Catalog:
    """Searches every configured provider and returns one ranked list."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self.itunes = ITunesProvider()
        self.deezer = DeezerProvider()

    @property
    def order(self) -> list[str]:
        return [p for p in self._cfg.provider_order if p in {"itunes", "deezer"}]

    def search(
        self,
        *,
        title: str,
        artist: str = "",
        artists: list[str] | None = None,
        duration_s: float | None = None,
    ) -> tuple[list[TrackMeta], list[str]]:
        """
        Returns ``(ranked_candidates, errors)``.

        A provider failing is reported, not raised: one dead provider must never
        stop the others from answering.
        """
        artist_names = list(artists or [])
        if artist and artist not in artist_names:
            artist_names.insert(0, artist)
        primary_artist = artist_names[0] if artist_names else ""

        collected: list[TrackMeta] = []
        errors: list[str] = []

        for name in self.order:
            provider = getattr(self, name)
            try:
                collected.extend(provider.search(title, primary_artist, limit=10))
            except Exception as exc:  # noqa: BLE001 - never fail the whole search
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

        ranked = score_candidates(collected, title, artist_names, duration_s)
        return _dedupe(ranked, self.order), errors

    def enrich(self, meta: TrackMeta) -> TrackMeta:
        """Fill in provider-specific detail (currently Deezer only)."""
        if meta.source == "deezer":
            try:
                return self.deezer.enrich(meta)
            except Exception:  # noqa: BLE001
                return meta
        return meta


def check_providers(cfg: Config) -> list[dict]:
    """Per-provider status for /api/health."""
    catalog = Catalog(cfg)
    checks: list[dict] = []

    for name in catalog.order:
        provider = getattr(catalog, name)
        try:
            found = provider.search("Never Gonna Give You Up", "Rick Astley", limit=3)
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {
                    "name": name,
                    "ok": False,
                    "severity": "required",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        checks.append(
            {
                "name": name,
                "ok": bool(found),
                "severity": "required",
                "detail": f"{len(found)} result(s)"
                + (f", top: {found[0].artist} - {found[0].title}" if found else ""),
            }
        )
    return checks
