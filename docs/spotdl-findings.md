# spotDL v4 — server-side research brief

Driving `spotdl` from a self-hosted HTTP server that downloads one Spotify track per request. Every claim is from spotDL's own source/docs or Spotify's docs (spotdl was not executed here, so runtime behaviour is *read from source*).

## 0. Version correction

Latest is **spotdl 4.5.2**, not a "4.2.x line": PyPI reports `4.5.2`, `requires_python <3.15,>=3.10`, classifiers 3.10–3.14 ([PyPI](https://pypi.org/project/spotdl/)); GitHub's latest release is `v4.5.2 - hotfix`, 2026-07-20 ([release](https://github.com/spotDL/spotify-downloader/releases/tag/v4.5.2)). 4.2.10 is from 2024-11 and predates heavy YouTube-side churn. **Pin `spotdl==4.5.2`.**

## 1. Invocation and credentials

```bash
spotdl download "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b"  # explicit
spotdl "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b"            # download is the default operation
spotdl download 'The Weeknd - Blinding Lights'                          # bare query, quoted
spotdl download URL_A URL_B 'query'                                      # several queries, one process
```

- **No Spotify developer credentials are needed by default.** spotDL ships built-in `client_id`/`client_secret` defaults with `"use_official_api": false`, using its own shared "SpotipyFree" client ([config.py](https://github.com/spotDL/spotify-downloader/blob/master/spotdl/utils/config.py)). Supply your own only if you pass `--client-id/--client-secret` or `--use-official-api`. Caveat: shared public creds can be revoked/rate-limited anytime, and the default-config block in [usage.md](https://github.com/spotDL/spotify-downloader/blob/master/docs/usage.md) shows a *different* pair than the code, so that block is stale — trust the code.
- Gotcha (read from `entry_point`): the operation word is matched exactly against `sys.argv` elements, so a bare query that *is* `web`/`save`/`sync`/`meta`/`url`/`download` triggers that operation. Sanitise user-supplied queries.

## 2. Output control

```bash
spotdl download "$URL" \
  --output "/music/{album-artist}/{album}/{track-number} - {title}.{track-id}.{output-ext}" \
  --format mp3 --bitrate 320k
```

- **`{ext}` does not exist — use `{output-ext}`.** `{artists}` = all artists, `{artist}` = primary. Full set: `{title} {artists} {artist} {album} {album-artist} {genre} {disc-number} {disc-count} {duration} {year} {original-date} {track-number} {tracks-count} {isrc} {track-id} {publisher} {list-length} {list-position} {list-name} {output-ext}`. `{track-id}` is the stable, dedupe-friendly choice.
- `--format` ∈ `{mp3,flac,ogg,opus,m4a,wav}` (default `mp3`); `--bitrate` ∈ `auto|disable|8k…320k|0…9`.
- Source is **YouTube Music only** by default (`"audio_providers": ["youtube-music"]`), searched via `ytmusicapi`, downloaded via `yt-dlp`. So the audio is 128 kbps (256 with a YT Music Premium `--cookie-file`); `--bitrate 320k` only re-encodes upward. For raw quality use `--format m4a --bitrate disable`. Fallbacks: `youtube`, `soundcloud`, `bandcamp`, `piped`. `--help` also lists `slider-kz`, but it is absent from the `AUDIO_PROVIDERS` registry in [downloader.py](https://github.com/spotDL/spotify-downloader/blob/master/spotdl/download/downloader.py) — **UNVERIFIED/unusable**.
- **ffmpeg is mandatory** (≥4.2, [installation](https://github.com/spotDL/spotify-downloader/blob/master/docs/installation.md)); missing → `FFmpegError` → exit 1. Install with `spotdl --download-ffmpeg`. **Deno is strongly recommended** (`spotdl --download-deno`): without it some videos, including "made for kids", fail with `AudioProviderError: YT-DLP download error` ([troubleshooting](https://github.com/spotDL/spotify-downloader/blob/master/docs/troubleshooting.md)).

## 3. Idempotency / dedup

- `--overwrite skip|metadata|force`, default `skip`: if the output file exists, spotDL logs "Skipping (file already exists)" and **returns the path as success without downloading** ([downloader.py](https://github.com/spotDL/spotify-downloader/blob/master/spotdl/download/downloader.py)). Free idempotency — but exit 0 + a path cannot distinguish "already had it" from "just downloaded".
- `--archive FILE` is spotDL's dedup store: a newline-separated set of Spotify **song URLs** ([archive.py](https://github.com/spotDL/spotify-downloader/blob/master/spotdl/utils/archive.py)). Archived URLs are filtered before download; successes are appended. URL-keyed — ideal here.
- `--scan-for-songs` + `--detect-formats` find existing files by embedded metadata (catches renamed files). `--create-skip-file`/`--respect-skip-file` use `<output>.skip` sentinels. There is **no `--skip-albums`** — use `--ignore-albums`. `.spotdl` files are the `save`/`sync` JSON metadata format, not a cache; `--use-cache-file` caches Spotify *metadata* only.
- **Recommendation:** own the truth in a DB keyed by Spotify track ID; `--archive` is only a second guard (unlocked read-modify-write file, unsafe with concurrent processes).

## 4. `spotdl web` — drivable, but a poor foundation

`spotdl web` serves FastAPI/uvicorn (default `--host localhost --port 8800`) and 4.5.2 **does** expose JSON ([api.py @ v4.5.2](https://github.com/spotDL/spotify-downloader/blob/v4.5.2/spotdl/web/api.py)):

| Endpoint | Notes |
|---|---|
| `GET /api/version`, `GET /api/url?url=`, `GET /api/songs/search?query=` | version / URL → `Song[]` / search → `Song[]` |
| `POST /api/download/url?url=&client_id=` | one track; returns file path, **HTTP 500** on failure |
| `GET /api/download/file?file=&client_id=` | serves a file (path-guarded) |
| `GET /api/settings`, `POST /api/settings/update` | per-client downloader settings |
| `GET /api/connect?client_id=` | must register a client first (400/404 otherwise) |

- So yes, it can be POSTed to — register `client_id`, then POST the track. But the UI routes are **Datastar SSE/HTML, not REST**, there is no queue/job/status API, and it is one track per call.
- **Security: no authentication or authorization at all.** CORS is limited to localhost:8800-style origins by default (`ALLOWED_ORIGINS` in [utils/web.py](https://github.com/spotDL/spotify-downloader/blob/master/spotdl/utils/web.py), extend with `--allowed-origins`), TLS is off unless `--enable-tls --cert-file --key-file`, and it calls `webbrowser.open()` on start.
- Output goes to a **per-session temp dir deleted on shutdown** unless `--web-use-output-dir`/`--keep-sessions` — hostile to an archive.
- `archive`, `ffmpeg`, `cookie_file`, `output` are in the API's `forbidden_actions`, so archive/cookie dedup is unreachable through it. Wrap the CLI instead.

## 5. Robustness — detecting real success

- **Per-song failures do not fail the process.** `async_search_and_download` catches everything, appends `f"{song.url} - {ExceptionClass}: {exception}"` to `downloader.errors`, and returns `(song, None)`. `download` then returns normally, so **exit code is 0 even if every song failed**. `sys.exit(1)` in [entry_point.py](https://github.com/spotDL/spotify-downloader/blob/master/spotdl/console/entry_point.py) covers only setup errors (missing ffmpeg, bad settings, unhandled exception).
- **No JSON output mode exists.** Machine-readable-ish: `--save-errors FILE` (timestamped error lines — the parseable one), `--print-errors`, `--log-level {CRITICAL…INFO,MATCH,DEBUG,NOTSET}`, `--log-format`, `--simple-tui`, and `--save-file x.spotdl` (song metadata JSON, not a status report).
- **Recipe:** exit ≠ 0 = hard failure; otherwise compute the expected path from your own template and `stat()` it — non-zero size + fresh mtime = downloaded, old mtime = already present; also tail `--save-errors`.
- Failure modes: no match (`LookupError: No results found for song`), yt-dlp metadata failure (`DownloaderError`), video unavailable/region-locked/DRM/age-gated (`AudioProviderError`), "made for kids" needing Deno, ffmpeg conversion failure (`FFmpegError`, dump written to `~/.spotdl/errors/`), YouTube/YT Music anti-bot blocks (startup warns "You might be blocked by YouTube Music"), Spotify metadata 404/429.

## 6. Packaging

- **Official image exists:** `spotdl/spotify-downloader` — `latest` currently is the **v4.5.2** image (pushed 2026-07-20), plus `nightly` and version tags ([Docker Hub](https://hub.docker.com/r/spotdl/spotify-downloader/tags)). The [Dockerfile](https://github.com/spotDL/spotify-downloader/blob/master/Dockerfile) is `python:3.14-slim-bookworm`, installs ffmpeg/aria2/deno, runs as **non-root** `spotdl` (UID/GID args, default 1000), `VOLUME /music`.
- **Python:** 3.10–3.14 supported (`<3.15,>=3.10`), official image is 3.14, release binaries built with 3.13 — 3.13/3.14 are fine.
- Known issue: **v4.5.1 crashed at startup** because yt-dlp's "Python 3.10 deprecated" notice was raised as fatal (hit pip installs on 3.10 too) — fixed in 4.5.2. ytmusicapi `KeyError: 'header'` needs `ytmusicapi>=1.11.1`, spotdl ≥4.4.3.

## 7. Spotify metadata without a full download

- Yes: `GET /v1/search?q=…&type=track` with a **client-credentials** token. That flow is "server-to-server… does not include authorization, only endpoints that do not access user information can be accessed" ([client credentials](https://developer.spotify.com/documentation/web-api/tutorials/client-credentials-flow)) — `/search` qualifies, so it is server-side only, no user login. `POST https://accounts.spotify.com/api/token`, `grant_type=client_credentials`, `Authorization: Basic base64(id:secret)` → `access_token`, `expires_in: 3600`.
- **Rate limits:** a **rolling 30-second window** whose value "varies depending on whether your app is in development mode or extended quota mode"; 429s normally carry `Retry-After` ([rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits)). Any specific number (e.g. "180/30 s") is **UNVERIFIED** — Spotify publishes no fixed figure. Dev-mode apps also have separate quota buckets returning 429 `"reason": "QUOTA_EXCEEDED"` ([quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)).
- **Feb 2026 changes affect you** ([migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)): dev-mode apps require the **owner to hold Spotify Premium** (app stops if it lapses), 1 client ID / 5 users max; `/search` `limit` max **50 → 10** (default 20 → 5, paginate via `offset`); **batch endpoints removed** — `GET /tracks?ids=` is gone, so metadata is **one request per track** (`GET /tracks/{id}`). spotDL's default path avoids the official API entirely (`use_official_api: false`), so only `--use-official-api` with your own app is affected. Whether the Premium rule applies to a client-credentials-only app is **UNVERIFIED** (the "All Development Mode apps" wording implies yes).

## Flag table (verified against the full `--help` listing in usage.md + config.py)

| Flag | Purpose | Notes |
|---|---|---|
| `download` (or omitted) | Operation; default operation | `spotdl [urls]` works |
| `--output TEMPLATE` | Output path/filename template | `{track-id}`, `{output-ext}` (**not** `{ext}`), `{artists}` vs `{artist}` |
| `--format {mp3,flac,ogg,opus,m4a,wav}` | Container | default `mp3` |
| `--bitrate {auto,disable,8k…320k,0…9}` | Bitrate / re-encode control | `disable` skips conversion (m4a/opus) |
| `--audio {youtube,youtube-music,soundcloud,bandcamp,piped}` | Source providers, fallback order | default `youtube-music` only |
| `--overwrite {skip,metadata,force}` | Existing-file handling | default `skip` → success on existing file |
| `--archive FILE` | Newline-separated downloaded-URL set | best built-in dedup |
| `--scan-for-songs` / `--detect-formats […]` | Find existing songs via embedded metadata | pair with `--overwrite` |
| `--create-skip-file` / `--respect-skip-file` | `<output>.skip` sentinels | |
| `--threads N` | Concurrent downloads in-process | default 4 |
| `--ffmpeg PATH` | ffmpeg binary | required ≥4.2 |
| `--download-ffmpeg` / `--download-deno` | Install ffmpeg / Deno into spotdl dir | Deno needed for some videos |
| `--cookie-file F` | cookies.txt | needed for YT Music Premium 256 kbps |
| `--print-errors` / `--save-errors FILE` | Report collected per-song errors | `--save-errors` is the parseable one |
| `--log-level` / `--log-format` / `--simple-tui` | Logging & UI control | no JSON mode exists |
| `--save-file X.spotdl` | Metadata JSON for the operation | also drives `save`/`sync` |
| `--ignore-albums […]` | Skip listed albums | no `--skip-albums` exists |
| `--max-retries N` | Retries for Spotify metadata | not for yt-dlp failures |
| `--restrict {strict,ascii,none}`, `--max-filename-length N` | Filename safety | |
| `--client-id` / `--client-secret` / `--use-official-api` | Use your own Spotify app | otherwise built-in shared creds |
| `--no-cache` / `--cache-path` / `--use-cache-file` | Spotify metadata cache | not download dedup |
| `--proxy`, `--yt-dlp-args`, `--sponsor-block`, `--preload` | Network / yt-dlp tuning | |
| `--host/--port/--allowed-origins/--enable-tls/--cert-file/--key-file/--web-use-output-dir/--keep-sessions/--keep-alive` | `spotdl web` only | no auth; sessions deleted on exit |

## Recommended server design

**FastAPI + SQLite + one background worker that shells out to the spotdl CLI.** The CLI is the documented, stable interface; `spotdl web` is unauthenticated, UI-first, session-dir based, single-track, and forbids `archive`/`ffmpeg`/`cookie_file` config. Do **not** import `spotdl.download.Downloader` in-process — undocumented internals that create their own event loops.

- Endpoints: `POST /download {track_id,url?}` → `202 {job_id}`; `GET /download/{job_id}` → status/path/error. Require a bearer token; bind to localhost or a private overlay network. This is single-user software, not a public service.
- Queue: one worker, batching drained items (e.g. ≤10 or a 5 s window) into **one** spotdl invocation to amortise startup and let `--threads` parallelise. **Never run two spotdl processes concurrently** — `--archive` is read-modify-write with no locking.
- Command: `spotdl download <urls…> --output "/music/{album-artist}/{album}/{track-number} - {title}.{track-id}.{output-ext}" --format mp3 --bitrate disable --archive /data/.spotdl-archive --overwrite skip --print-errors --save-errors /logs/spotdl-errors.log --log-level INFO --simple-tui --threads 4`
- Verify: exit code, then expected file non-zero size + fresh mtime, then parse `--save-errors`. Never trust exit 0 alone.
- Dedup: SQLite `tracks(spotify_id PK, status, path, attempts, last_error, updated_at)` is the truth; `--archive` second; `--overwrite skip` third.
- Ops: run as non-root with a UID matching the volume; per-batch subprocess timeout with kill + requeue; pin `spotdl==4.5.2` but allow independent `yt-dlp`/`ytmusicapi` upgrades and a monthly image rebuild (the `yt-dlp<2027` bound accepts new releases).

## Fragility and ToS

- YouTube's ToS prohibits downloading; spotDL's README says users are responsible and that it does not support unauthorised downloading of copyrighted material. Spotify's Developer Policy likewise does not sanction archiving copies via the API. This is a personal-archiving grey area that both sides actively fight.
- **Highest-risk assumptions:** (1) the built-in shared Spotify credentials can be rate-limited or pulled at any time; (2) `ytmusicapi`/`yt-dlp` search and extraction break routinely under YouTube counter-measures (`AudioProviderError: YT-DLP download error`, YT Music `KeyError`s), with fixes only in new releases; (3) region-locked/DRM/"made for kids" tracks may fail indefinitely; (4) Spotify's Feb 2026 dev-mode tightening (Premium owner, 1 client ID, 5 users, search `limit` 10, batch endpoints removed) invalidated older tutorials' assumptions. Design for *retryable failure*: persist error strings, expose them to the extension, keep the queue durable.
