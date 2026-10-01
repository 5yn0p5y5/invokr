"""
Environment checks surfaced through ``GET /api/health``.

These exist because every failure mode this project has is an environment
failure: a missing ffmpeg, an unwritable library, an unreachable metadata
catalogue, or a yt-dlp that suddenly needs a JavaScript runtime. Checking them
up front turns a mystery download failure into one clear line of JSON.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .catalog import check_providers
from .config import Config

REQUIRED = "required"
RECOMMENDED = "recommended"
OPTIONAL = "optional"


def _result(
    name: str, ok: bool, detail: str, severity: str = REQUIRED, **extra
) -> dict:
    return {
        "name": name,
        "ok": bool(ok),
        "severity": severity,
        "detail": detail,
        **extra,
    }


def _run(cmd: list[str], timeout: int = 25) -> tuple[int | None, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, f"{cmd[0]} not found on PATH"
    except Exception as exc:  # noqa: BLE001
        return None, f"{cmd[0]} failed to run: {exc}"
    output = (proc.stdout or proc.stderr or "").strip()
    first = output.splitlines()[0].strip() if output else ""
    return proc.returncode, first


def check_ffmpeg() -> dict:
    exe = shutil.which("ffmpeg")
    if not exe:
        return _result("ffmpeg", False, "not found on PATH (spotDL requires it)")
    rc, line = _run(["ffmpeg", "-version"])
    return _result("ffmpeg", rc == 0, f"{exe} — {line}" if line else exe)


def check_js_runtime() -> dict:
    """
    yt-dlp warns that YouTube extraction without a JS runtime is deprecated.

    Downloads currently succeed without one, so this is a warning, not a gate.
    """
    found = [name for name in ("deno", "node", "bun") if shutil.which(name)]
    if found:
        return _result(
            "js_runtime", True, f"found: {', '.join(found)}", severity=RECOMMENDED
        )
    return _result(
        "js_runtime",
        False,
        "no deno/node/bun on PATH; yt-dlp warns that some formats may be missing",
        severity=RECOMMENDED,
    )


def check_library(cfg: Config) -> dict:
    path = cfg.library_path
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".invokr-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return _result("library", False, f"{path} is not writable: {exc}")
    return _result("library", True, str(path))


def check_output_template(cfg: Config) -> dict:
    template = cfg.output_template
    if "{output-ext}" not in template:
        return _result(
            "output_template",
            False,
            "template is missing {output-ext} (the variable is {output-ext}, not {ext})",
        )
    unknown = []
    for token in ("{ext}", "{album_artist}", "{filename}"):
        if token in template:
            unknown.append(token)
    if unknown:
        return _result(
            "output_template",
            False,
            f"template uses unsupported variable(s): {', '.join(unknown)}",
        )
    return _result("output_template", True, cfg.output_template_full)


def check_database(cfg: Config) -> dict:
    path = cfg.db_path
    try:
        from . import db

        db.configure(path)
        depth = db.queue_depth()
    except Exception as exc:  # noqa: BLE001
        return _result("database", False, f"{path}: {exc}")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(depth.items())) or "empty"
    return _result("database", True, f"{path} ({summary})")


def check_provider_health(cfg: Config) -> list[dict]:
    """One check per metadata provider. A dead provider must not hide the rest."""
    try:
        return check_providers(cfg)
    except Exception as exc:  # noqa: BLE001
        return [_result("providers", False, f"{type(exc).__name__}: {exc}")]


def run(cfg: Config) -> dict:
    checks = [
        check_ffmpeg(),
        check_library(cfg),
        check_output_template(cfg),
        check_database(cfg),
        check_js_runtime(),
    ]
    checks.extend(check_provider_health(cfg))

    required_ok = all(c["ok"] for c in checks if c["severity"] == REQUIRED)
    return {
        "ok": required_ok,
        "checks": checks,
        "warnings": [
            f"{c['name']}: {c['detail']}"
            for c in checks
            if not c["ok"] and c["severity"] != REQUIRED
        ],
    }
