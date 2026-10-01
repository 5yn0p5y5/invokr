"""
Configuration for the invokr server.

The config lives at ``server/config.json`` and is created automatically on first
load with a freshly generated bearer token, so there is no bootstrap step. It is
JSON rather than env vars so it is easy to edit by hand and easy to diff.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = SERVER_DIR.parent
CONFIG_PATH = SERVER_DIR / "config.json"


@dataclass
class Config:
    """Runtime configuration, persisted as JSON."""

    # --- network -----------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""

    # --- library -----------------------------------------------------------
    library: str = "~/Music/invokr"
    # spotDL template variables. NOTE: the extension variable is {output-ext},
    # not {ext}. Verified against spotDL 4.5.2.
    output_template: str = r"{artist} - {title}.{output-ext}"

    # --- download ----------------------------------------------------------
    # yt-dlp fetches the audio, mutagen writes the tags. spotDL was dropped
    # because reinit_song() calls the Spotify API for every track, so it cannot
    # run at all without working Spotify credentials.
    format: str = "m4a"
    bitrate: str = "disable"  # "disable" == move, do not re-encode
    cookie_file: str | None = None  # optional cookies.txt for YT Music Premium
    download_timeout_seconds: int = 900

    # --- metadata providers ------------------------------------------------
    # Searched in this order, best-scoring result first. Both need no account.
    # Spotify is deliberately absent: its shared application is rate limited and
    # a replacement app requires Spotify Premium, for no extra fields.
    provider_order: list[str] = field(default_factory=lambda: ["itunes", "deezer"])

    # --- misc --------------------------------------------------------------
    log_dir: str = "logs"
    review_ttl_minutes: int = 30

    # --- auto-accept -------------------------------------------------------
    # Download without asking when the top match is unambiguous. Every signal
    # has to agree, and the margin over the runner-up matters most: it is what
    # separates the studio version from the MTV Unplugged version, and picking
    # the wrong one is silently wrong forever.
    auto_accept: bool = True
    auto_accept_min_score: float = 85.0
    auto_accept_min_title: float = 88.0
    auto_accept_min_artist: float = 85.0
    auto_accept_min_margin: float = 8.0

    # ------------------------------------------------------------- paths ---

    @property
    def library_path(self) -> Path:
        return self._resolve(self.library)

    @property
    def log_path(self) -> Path:
        return self._resolve(self.log_dir)

    @staticmethod
    def _resolve(value: str) -> Path:
        """
        Expand ``~`` and environment variables, and resolve a relative path
        against the project directory.

        This is what lets the shipped config say ``~/Music/invokr`` and
        ``logs`` instead of hard-coding one machine's layout.
        """
        path = Path(os.path.expandvars(value)).expanduser()
        return path if path.is_absolute() else WORKSPACE_DIR / path

    @property
    def db_path(self) -> Path:
        return SERVER_DIR / "invokr.db"

    @property
    def spotdl_errors_path(self) -> Path:
        return self.log_path / "spotdl-errors.log"

    @property
    def output_template_full(self) -> str:
        """The absolute ``--output`` template handed to spotDL."""
        return str(self.library_path / self.output_template)

    # --------------------------------------------------------- lifecycle ---

    def ensure_directories(self) -> None:
        self.library_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            cfg = cls(**{k: v for k, v in raw.items() if k in known})
            if not cfg.token:
                cfg.token = secrets.token_urlsafe(32)
                cfg.save()
        else:
            cfg = cls(token=secrets.token_urlsafe(32))
            cfg.save()

        cfg.ensure_directories()
        return cfg

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8"
        )

    def public(self) -> dict:
        """A config view safe to return over HTTP — never includes the token."""
        return {
            "host": self.host,
            "port": self.port,
            "library": str(self.library_path),
            "output_template": self.output_template,
            "format": self.format,
            "bitrate": self.bitrate,
            "database": str(self.db_path),
            "providers": self.provider_order,
            "auto_accept": self.auto_accept,
            "auto_accept_thresholds": {
                "score": self.auto_accept_min_score,
                "title": self.auto_accept_min_title,
                "artist": self.auto_accept_min_artist,
                "margin": self.auto_accept_min_margin,
            },
        }


CONFIG = Config.load()


def get_config() -> Config:
    """Accessor used by the FastAPI dependency layer and tests."""
    return CONFIG
