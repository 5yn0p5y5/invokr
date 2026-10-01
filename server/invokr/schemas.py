"""Request and response models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ReviewRequest(BaseModel):
    """
    Payload the extension sends when you like a song.

    Aliases are camelCase so the extension can post the same shape it reads out
    of the YouTube Music page, while the Python side stays snake_case.
    """

    model_config = ConfigDict(populate_by_name=True)

    video_id: str = Field(alias="videoId")
    yt_url: str | None = Field(default=None, alias="ytUrl")
    title: str = ""
    artists: list[str] = Field(default_factory=list)
    album: str = ""
    duration_sec: float | None = Field(default=None, alias="durationSec")
    # Re-download even if this video is already archived.
    force: bool = False


class UnlikeRequest(BaseModel):
    """
    An un-like is recorded and nothing more.

    Files are never deleted or moved, so a mis-click cannot destroy an archive.
    """

    model_config = ConfigDict(populate_by_name=True)

    video_id: str = Field(alias="videoId")
    title: str = ""


class LibraryImportRequest(BaseModel):
    """
    Register files that are already on disk.

    ``path`` defaults to the configured library folder, but any directory works
    — old spotDL output living somewhere else is the main use case.
    """

    path: str | None = None
    dry_run: bool = Field(default=False, alias="dryRun")
    recursive: bool = True

    model_config = ConfigDict(populate_by_name=True)


class ChoiceRequest(BaseModel):
    """Index into the candidate list, for the popup's JSON confirm."""

    choice: int = 0


class ManualMetadataRequest(BaseModel):
    """Hand-entered metadata, for tracks no catalogue carries."""

    model_config = ConfigDict(populate_by_name=True)

    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = Field(default="", alias="albumArtist")
    year: str = ""
    track_number: int | None = Field(default=None, alias="trackNumber")
    genre: str = ""
    cover_url: str | None = Field(default=None, alias="coverUrl")
