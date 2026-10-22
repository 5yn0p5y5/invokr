#!/usr/bin/env python3
"""Sign the invokr extension through addons.mozilla.org, with no Node.js.

This does what `web-ext sign --channel unlisted` did, using only the standard
library: it packages extension/ into an .xpi, creates the short-lived JWT AMO
expects, uploads the build, waits for Mozilla's validator, submits it as an
unlisted version, waits for the signature, and stores the signed file in
web-ext-artifacts/.

AMO runs the same manifest/package checks that `web-ext lint` ran locally and
refuses an invalid upload, so there is no separate lint pass to keep in sync.

Credentials come from WEB_EXT_API_KEY / WEB_EXT_API_SECRET, or from
${XDG_CONFIG_HOME:-$HOME/.config}/invokr/signing.env when they are not in the
environment:

mkdir -p ~/.config/invokr
umask 077
cat > ~/.config/invokr/signing.env <<'ENV'
export WEB_EXT_API_KEY='user:12345:67890'
export WEB_EXT_API_SECRET='<secret>'
ENV
chmod 600 ~/.config/invokr/signing.env

Generate the pair at https://addons.mozilla.org/developers/addon/api/key/

Usage:
    tools/sign-extension.py
    tools/sign-extension.py --dry-run
    tools/sign-extension.py --resume          # continue a submitted version
    tools/sign-extension.py --source-dir extension --artifacts-dir dist
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

AMO_BASE = "https://addons.mozilla.org/api/v5/"
USER_AGENT = "invokr-sign/1.0"
JWT_LIFETIME_SECONDS = 300
POLL_INTERVAL_SECONDS = 2.0
REPORT_INTERVAL_SECONDS = 15.0
VALIDATION_TIMEOUT_SECONDS = 300
APPROVAL_TIMEOUT_SECONDS = 900

# Files that must not end up inside the archive. Hidden files are skipped
# separately, which also keeps editor and VCS droppings out.
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = {".xpi", ".zip"}


class AmoError(RuntimeError):
    """A request to AMO failed, or the build did not pass validation."""


# ----------------------------------------------------------------- credentials


def _read_env_file(path: Path) -> dict[str, str]:
    """Read `export NAME='value'` lines without executing the file."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        if tokens and tokens[0] == "export":
            tokens = tokens[1:]
        for token in tokens:
            name, sep, value = token.partition("=")
            if sep:
                values[name] = value
    return values


def load_credentials() -> tuple[str | None, str | None]:
    key = os.environ.get("WEB_EXT_API_KEY", "")
    secret = os.environ.get("WEB_EXT_API_SECRET", "")

    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    env_file = base / "invokr" / "signing.env"
    if (not key or not secret) and env_file.is_file():
        values = _read_env_file(env_file)
        key = key or values.get("WEB_EXT_API_KEY", "")
        secret = secret or values.get("WEB_EXT_API_SECRET", "")

    return key or None, secret or None


# ------------------------------------------------------------------------- JWT


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def auth_header(key: str, secret: str) -> str:
    """A fresh HS256 JWT in the header format AMO expects."""
    issued_at = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "iss": key,
        # A nonce: AMO rejects a replayed jti.
        "jti": secrets.token_hex(16),
        "iat": issued_at,
        "exp": issued_at + JWT_LIFETIME_SECONDS,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in (header, claims)
    )
    signature = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"JWT {signing_input}.{_b64url(signature)}"


# ------------------------------------------------------------------------ HTTP


def _headers(key: str, secret: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {
        "Authorization": auth_header(key, secret),
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        **(extra or {}),
    }


def _http_error(method: str, url: str, error: urllib.error.HTTPError) -> AmoError:
    detail = error.read().decode("utf-8", "replace").strip()
    if len(detail) > 2000:
        detail = detail[:2000] + "…"
    return AmoError(f"{method} {url} → HTTP {error.code}\n{detail}")


def request_json(
    url: str,
    key: str,
    secret: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 60,
) -> dict:
    extra = {"Content-Type": content_type} if content_type else {}
    request = urllib.request.Request(
        url, data=body, method=method, headers=_headers(key, secret, extra)
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise _http_error(method, url, error) from None
    except urllib.error.URLError as error:
        raise AmoError(f"{method} {url} failed: {error.reason}") from None
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise AmoError(f"{method} {url} returned non-JSON:\n{raw[:500]!r}") from None


def download(url: str, destination: Path, key: str, secret: str) -> None:
    partial = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url, headers=_headers(key, secret))
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            with partial.open("wb") as handle:
                while chunk := response.read(1024 * 256):
                    handle.write(chunk)
        partial.replace(destination)
    except urllib.error.HTTPError as error:
        partial.unlink(missing_ok=True)
        raise _http_error("GET", url, error) from None
    except urllib.error.URLError as error:
        partial.unlink(missing_ok=True)
        raise AmoError(f"GET {url} failed: {error.reason}") from None


def _absolute(base: str, url: str) -> str:
    """AMO returns some URLs absolute and some site-relative."""
    return url if urllib.parse.urlparse(url).scheme else urllib.parse.urljoin(base, url)


def _multipart(
    fields: dict[str, str], file_field: str, filename: str, content: bytes
) -> tuple[bytes, str]:
    boundary = f"----invokr{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\n'
        "Content-Type: application/x-xpinstall\r\n\r\n"
    ).encode("utf-8")
    body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


# -------------------------------------------------------------------- package


def load_manifest(source_dir: Path) -> dict:
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.is_file():
        raise AmoError(f"No manifest.json in {source_dir} — check --source-dir.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise AmoError(f"{manifest_path} is not valid JSON: {error}") from None
    if manifest.get("manifest_version") != 3:
        raise AmoError(f"{manifest_path} must declare manifest_version 3.")
    if not manifest.get("version"):
        raise AmoError(f"{manifest_path} has no version.")
    return manifest


def addon_id(manifest: dict) -> str:
    gecko = manifest.get("browser_specific_settings", {}).get("gecko", {})
    guid = gecko.get("id")
    if not guid:
        raise AmoError(
            "manifest.json has no browser_specific_settings.gecko.id; "
            "a Manifest V3 add-on needs one to be signed."
        )
    return guid


def build_xpi(source_dir: Path, version: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source_dir)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.name in SKIP_NAMES or path.suffix.lower() in SKIP_SUFFIXES:
                continue
            archive.write(path, relative.as_posix())

    content = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if "manifest.json" not in archive.namelist():
            raise AmoError("Built archive has no manifest.json at its root.")
    return content


# ---------------------------------------------------------------------- steps


def upload(
    base: str,
    content: bytes,
    filename: str,
    channel: str,
    key: str,
    secret: str,
    timeout: float,
) -> str:
    body, content_type = _multipart(
        {"channel": channel}, "upload", filename, content
    )
    url = urllib.parse.urljoin(base, "addons/upload/")
    uploaded = request_json(
        url, key, secret, method="POST", body=body, content_type=content_type
    )
    upload_uuid = uploaded.get("uuid")
    if not upload_uuid:
        raise AmoError(f"Upload response had no uuid:\n{json.dumps(uploaded, indent=2)}")

    print("Waiting for validation…", flush=True)
    detail_url = urllib.parse.urljoin(base, f"addons/upload/{upload_uuid}/")
    deadline = time.monotonic() + timeout
    while True:
        detail = request_json(detail_url, key, secret)
        if detail.get("processed"):
            if not detail.get("valid"):
                raise AmoError(
                    "Validation failed:\n"
                    + json.dumps(detail.get("validation"), indent=2)
                )
            return upload_uuid
        if time.monotonic() > deadline:
            raise AmoError(
                f"Validation: timeout after {timeout:.0f}s. "
                "The upload may still finish; check AMO before retrying."
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def submit(
    base: str, guid: str, upload_uuid: str, key: str, secret: str
) -> tuple[str, str | None]:
    url = urllib.parse.urljoin(
        base, f"addons/addon/{urllib.parse.quote(guid, safe='')}/"
    )
    payload = json.dumps({"version": {"upload": upload_uuid}}).encode("utf-8")
    submitted = request_json(
        url, key, secret, method="PUT", body=payload, content_type="application/json"
    )
    version = submitted.get("version") or {}
    version_id = version.get("id")
    if version_id is None:
        raise AmoError(
            "Submission response had no version id:\n"
            + json.dumps(submitted, indent=2)
        )
    edit_url = version.get("edit_url")
    return str(version_id), (_absolute(base, edit_url) if edit_url else None)


def _version_url(base: str, guid: str, reference: str) -> str:
    return urllib.parse.urljoin(
        base,
        f"addons/addon/{urllib.parse.quote(guid, safe='')}/versions/"
        f"{urllib.parse.quote(str(reference), safe='')}/",
    )


def wait_for_signature(
    base: str,
    guid: str,
    reference: str,
    key: str,
    secret: str,
    timeout: float,
    edit_url: str | None = None,
) -> str | None:
    """
    Poll until AMO has signed the version.

    A timeout of 0 means "submit but do not wait": return None after saying where
    the signed file will appear. That keeps a slow first submission from
    blocking, and --resume can pick the version up later without re-uploading.
    """
    url = _version_url(base, guid, reference)
    if timeout <= 0:
        print("Not waiting for the signature (--approval-timeout 0).")
        if edit_url:
            print(f"Watch this version at: {edit_url}")
        return None

    print(f"Waiting for the signature (up to {timeout:.0f}s)…", flush=True)
    started = time.monotonic()
    deadline = started + timeout
    last_report = -REPORT_INTERVAL_SECONDS
    while True:
        detail = request_json(url, key, secret)
        file_info = detail.get("file") or {}
        status = file_info.get("status") or "pending"
        signed = (
            status == "public"
            or file_info.get("is_mozilla_signed_extension") is True
        )
        if signed and file_info.get("url"):
            return _absolute(base, file_info["url"])
        if status == "disabled":
            raise AmoError(
                "AMO reports this version's file as disabled, so it will not be "
                "signed:\n"
                + json.dumps(detail, indent=2)
                + (f"\n\nOpen {edit_url}" if edit_url else "")
            )

        elapsed = time.monotonic() - started
        if elapsed - last_report >= REPORT_INTERVAL_SECONDS:
            print(f"  {elapsed:5.0f}s  status: {status}", flush=True)
            last_report = elapsed
        if time.monotonic() > deadline:
            raise AmoError(
                f"Signing: timeout after {timeout:.0f}s (last status: {status}).\n"
                "The version exists; the signed file appears on AMO once the "
                "signer finishes.\n"
                + (f"Watch it at: {edit_url}\n" if edit_url else "")
                + "Resume without re-uploading: "
                f"{Path(__file__).name} --resume"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


# ----------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Package and sign the invokr extension through addons.mozilla.org.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Credentials: WEB_EXT_API_KEY and WEB_EXT_API_SECRET, or\n"
            "${XDG_CONFIG_HOME:-$HOME/.config}/invokr/signing.env."
        ),
    )
    parser.add_argument(
        "-s", "--source-dir", type=Path, default=repo_root / "extension",
        help="extension source (default: <repo>/extension)",
    )
    parser.add_argument(
        "-a", "--artifacts-dir", type=Path, default=repo_root / "web-ext-artifacts",
        help="where the signed .xpi lands (default: <repo>/web-ext-artifacts)",
    )
    parser.add_argument(
        "-c", "--channel", default="unlisted",
        help="AMO channel: unlisted (default), listed, or enterprise",
    )
    parser.add_argument(
        "--amo-base", default=AMO_BASE,
        help=f"AMO API base URL (default: {AMO_BASE})",
    )
    parser.add_argument(
        "--validation-timeout", type=float, default=VALIDATION_TIMEOUT_SECONDS,
        help=f"seconds to wait for validation (default: {VALIDATION_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--approval-timeout", type=float, default=APPROVAL_TIMEOUT_SECONDS,
        help=f"seconds to wait for signing (default: {APPROVAL_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="package the extension and stop before contacting AMO",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help=(
            "do not upload: poll the manifest version that is already on AMO "
            "and download it when signed"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    source_dir = args.source_dir.resolve()
    manifest = load_manifest(source_dir)
    version = str(manifest["version"])
    guid = addon_id(manifest)
    content = build_xpi(source_dir, version)
    filename = f"invokr-{version}.xpi"

    print(f"Built {filename} ({len(content) / 1024:.1f} KiB, id {guid})")
    if args.dry_run:
        print("Dry run: not uploading.")
        return 0

    key, secret = load_credentials()
    if not key or not secret:
        print(
            "Missing WEB_EXT_API_KEY and/or WEB_EXT_API_SECRET.\n\n"
            "Generate API credentials at "
            "https://addons.mozilla.org/developers/addon/api/key/\n"
            "then store them once, in a 0600 file this script reads automatically:\n\n"
            "mkdir -p ~/.config/invokr\n"
            "umask 077\n"
            "cat > ~/.config/invokr/signing.env <<'ENV'\n"
            "export WEB_EXT_API_KEY='user:12345:67890'\n"
            "export WEB_EXT_API_SECRET='<secret>'\n"
            "ENV\n"
            "chmod 600 ~/.config/invokr/signing.env\n\n"
            "The JWT issuer is the API key; the JWT secret is the secret. A plain\n"
            "`export WEB_EXT_API_KEY=...` in the current shell works too.",
            file=sys.stderr,
        )
        return 1

    try:
        if args.resume:
            print(f"Resuming {filename} on {args.amo_base}; not uploading.")
            # `v` forces lookup by version number even for a numeric version.
            signed_url = wait_for_signature(
                args.amo_base,
                guid,
                f"v{version}",
                key,
                secret,
                args.approval_timeout,
            )
        else:
            print(f"Uploading to {args.amo_base} ({args.channel})")
            upload_uuid = upload(
                args.amo_base, content, filename, args.channel,
                key, secret, args.validation_timeout,
            )
            reference, edit_url = submit(args.amo_base, guid, upload_uuid, key, secret)
            print(f"Submitted version {version}.")
            if edit_url:
                print(f"Watch it at: {edit_url}")
            signed_url = wait_for_signature(
                args.amo_base, guid, reference, key, secret,
                args.approval_timeout, edit_url=edit_url,
            )

        if signed_url is None:
            return 0

        artifacts_dir = args.artifacts_dir.resolve()
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        download_name = Path(urllib.parse.urlparse(signed_url).path).name or filename
        downloaded = artifacts_dir / download_name
        download(signed_url, downloaded, key, secret)
        release = artifacts_dir / filename
        if downloaded != release:
            release.write_bytes(downloaded.read_bytes())
    except AmoError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "\nInterrupted. The upload or version may still be processed by AMO.\n"
            f"Resume without re-uploading: {Path(__file__).name} --resume\n"
            "Do not run the normal command again for the same version — AMO "
            "rejects duplicates.",
            file=sys.stderr,
        )
        return 130

    print()
    print(f"Signed by AMO : {downloaded}")
    print(f"Release asset : {release}")
    print()
    print("Attach the release asset to a GitHub release, or install it directly:")
    print("about:addons > gear icon > Install Add-on From File...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
