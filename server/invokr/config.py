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

    # --- master folder -----------------------------------------------------
    # One portable folder that holds everything invokr owns:
    #
    #     <root>/library/   the audio
    #     <root>/db/        invokr.db and its backups
    #     <root>/logs/
    #
    # Setting this is what makes an archive relocatable: copy the folder, point
    # `root` at the copy, and the index comes with the music. It also decides
    # where new files land when downloaded.
    root: str = ""

    # --- library -----------------------------------------------------------
    # Optional overrides, for layouts that predate `root`. Left empty, each path
    # is derived from `root` instead, which is the portable arrangement.
    library: str = ""
    db: str = ""
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
    # Empty means "derive from root", exactly like library and db above.
    log_dir: str = ""
    review_ttl_minutes: int = 30
    # Consistent VACUUM INTO snapshots, kept beside the database. Set the
    # interval to 0 to turn them off.
    backup_interval_minutes: int = 360
    backup_keep: int = 7

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
    def root_path(self) -> Path | None:
        """The master folder, or None when this config predates ``root``."""
        return self._resolve(self.root) if self.root else None

    @property
    def library_path(self) -> Path:
        if self.library:
            return self._resolve(self.library)
        if self.root:
            return self._resolve(self.root) / "library"
        return self._resolve("~/Music/invokr")

    @property
    def log_path(self) -> Path:
        if self.log_dir:
            return self._resolve(self.log_dir)
        if self.root:
            return self._resolve(self.root) / "logs"
        return self._resolve("logs")

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
        if self.db:
            return self._resolve(self.db)
        if self.root:
            return self._resolve(self.root) / "db" / "invokr.db"
        # Legacy layout: the database sat next to the server code. Kept so an
        # install that predates `root` keeps working untouched.
        return SERVER_DIR / "invokr.db"

    @property
    def legacy_db_path(self) -> Path:
        """Where the database lived before master folders existed."""
        return SERVER_DIR / "invokr.db"

    @property
    def backup_dir(self) -> Path:
        return self.db_path.parent / "backups"

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
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def adopt_root(self, value: str) -> None:
        """
        Switch this install to a master folder.

        Retires the per-path overrides: leaving ``library`` or ``log_dir`` set
        would silently outrank the new root, so the setting would appear to do
        nothing. Nothing on disk is moved — this only changes where the server
        reads and writes from here on.
        """
        self.root = value
        self.library = ""
        self.db = ""
        self.log_dir = ""
        self.save()
        self.ensure_directories()

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
            "root": str(self.root_path) if self.root_path else None,
            "library": str(self.library_path),
            "database": str(self.db_path),
            "logs": str(self.log_path),
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


def validate_directory(value: str) -> tuple[Path | None, str | None]:
    """
    Check that a folder the user typed can actually hold the library, and return
    it resolved. Used before any setting is written, so a typo or a share that is
    not mounted is refused with a reason instead of silently accepted.
    """
    if not value or not value.strip():
        return None, "a folder path is required"

    text = value.strip()
    candidate = Path(os.path.expandvars(text)).expanduser()
    if not candidate.is_absolute():
        return None, f"{text} is not an absolute path"

    anchor = Path(candidate.anchor)
    if not anchor.exists():
        return None, (
            f"{anchor} is not available — is the drive or network share connected?"
        )

    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"cannot create {candidate}: {exc}"

    if not candidate.is_dir():
        return None, f"{candidate} exists but is not a directory"

    probe = candidate / ".invokr-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return None, f"{candidate} is not writable: {exc}"

    return candidate.resolve(), None


def get_config() -> Config:
    """Accessor used by the FastAPI dependency layer and tests."""
    return CONFIG
