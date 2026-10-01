<img src="assets/icon.png" alt="" width="96" align="right">

# invokr

**Like a song on YouTube Music. It shows up in your own music folder, properly
tagged, with cover art.**

No Spotify account, no API keys, no cloud service. A Firefox extension watches
YouTube Music, a small local server does the work, and the audio comes from the
exact video you liked.

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Using it](#using-it)
- [Bringing your existing music](#bringing-your-existing-music)
- [Configuration](#configuration)
- [HTTP API](#http-api)
- [Troubleshooting](#troubleshooting)
- [Design notes](#design-notes)
- [Responsible use](#responsible-use)

---

## How it works

```
Firefox (music.youtube.com)                Local server (127.0.0.1:8765)
┌──────────────────────────────┐           ┌────────────────────────────────┐
│ background.js                │           │ POST /api/review               │
│   • webRequest sees the      │           │   → search iTunes + Deezer     │
│     /youtubei/v1/like POST   │  Bearer   │   → rank the candidates        │
│     and reads target.videoId │  token    │   → auto-download, or ask      │
│   • dedupes, posts, badges   │ ────────► │                                │
│     the toolbar icon         │           │ GET  /api/reviews              │
│                              │           │ GET  /api/review/{id}          │
│ popup.js (toolbar panel)     │           │   → candidates as JSON         │
│   • renders candidates,      │           │                                │
│     manual metadata, art     │           │ worker: yt-dlp + mutagen       │
│   • confirms / skips         │           │   → download the exact video   │
│                              │           │   → write tags + cover art     │
│ ytm-bridge.js (content)      │           │   → verify the file on disk    │
│   • reads the player bar for │           │                                │
│     title/artist/duration    │           │ SQLite: tracks, jobs, events   │
└──────────────────────────────┘           └────────────────────────────────┘
```

The audio **always** comes from the video you liked. The catalogue is only used
for metadata — title, artist, album, year, track number, genre and cover art.

Two design choices are worth knowing up front:

- **Likes are detected from the network, not the DOM.** `webRequest` reads
  `target.videoId` out of the `/youtubei/v1/like/*` POST that YouTube Music
  sends. That works identically for the player bar, playlist rows and the "…"
  menu, and it cannot fail silently. A DOM observer would only ever see the
  currently playing track.
- **Obvious matches download without asking.** The server scores every
  candidate and only opens the review panel when something is genuinely
  ambiguous. See [tuning auto-accept](#tuning-auto-accept).

---

## Requirements

| | |
|---|---|
| **Python** | 3.10 or newer |
| **ffmpeg** | must be on `PATH` — `winget install Gyan.FFmpeg` / `brew install ffmpeg` / `apt install ffmpeg` |
| **Firefox** | 115 or newer. **149+** to have the review panel open automatically |
| **OS** | developed on Windows; the server is plain Python and should run anywhere |

There is nothing else to sign up for.

---

## Install

### 1. Get the code

```bash
git clone https://github.com/5yn0p5y5/invokr.git
cd invokr
python -m pip install -r requirements.txt
```

### 2. Start the server

```bash
cd server
python run.py
```

First run creates `server/config.json` containing a freshly generated bearer
token. Check everything is in place:

```powershell
# PowerShell
$token = (Get-Content .\config.json -Raw | ConvertFrom-Json).token
Invoke-RestMethod http://127.0.0.1:8765/api/health -Headers @{Authorization="Bearer $token"} |
  Select-Object -ExpandProperty checks | Format-Table name, ok, detail -AutoSize
```

```bash
# bash
curl -s -H "Authorization: Bearer $(python -c "import json;print(json.load(open('config.json'))['token'])")" \
  http://127.0.0.1:8765/api/health | python -m json.tool
```

You want every required check to pass.

### 3. Load the extension

1. Open `about:debugging#/runtime/this-firefox`
2. **Load Temporary Add-on…**
3. Pick `extension/manifest.json`

> A temporary add-on disappears when Firefox restarts. To keep it permanently,
> sign it with [`web-ext sign`](https://extensionworkshop.com/documentation/develop/getting-started-with-web-ext/)
> and install the resulting `.xpi`.

### 4. Connect them

Open the extension's settings (click the toolbar icon → **Settings**) and paste:

- **Server URL** — `http://127.0.0.1:8765`
- **Bearer token** — the `token` value from `server/config.json`

Press **Test connection**. It should say *Server reachable and ready.*

---

## Using it

Like a song on YouTube Music. That's the whole workflow most of the time.

**Obvious matches download themselves.** You get a *"Saving — artist — title"*
notification, then *"Saved"* when the file lands. Nothing to click.

**When the match is ambiguous**, the review panel opens by itself showing the
candidates with artwork, album, duration and score, plus a line explaining what
was uncertain:

> Why: too close to call — 86.1 vs 80.1 for "Take On Me (MTV Unplugged)"

The toolbar badge tells you the state:

| Badge | Meaning |
|---|---|
| *n* (purple) | *n* reviews are waiting for a decision |
| ↓ (blue) | A download is running |
| ✓ (green) | Saved, or already in your archive |
| ! (red) | Token rejected, server unreachable, or the download failed |

Three ways out of a review:

- **Download selected** — queue the highlighted candidate
- **Skip** — record a decision and move on
- **Discard** — forget the review entirely, as if you never liked it

With several reviews pending you also get **Discard all**, and downloads still
queued can be cancelled. Review sessions live on the server for 30 minutes, so
closing the panel by accident loses nothing — reopen it and the review is there.

### When no catalogue has the song

Unreleased tracks, leaks, DJ edits — nothing will match. Open
**"Can't find it? Enter the metadata manually"** in the review panel, type the
title, artist, album and year, then either search for artwork by artist name or
just use the YouTube thumbnail. Press **Download with this metadata**.

The artwork search ranks the artist's own releases above "feat." credits, so
searching an artist name gives you their albums rather than a wall of features.

---

## Bringing your existing music

Already have a folder of downloads? Register them so invokr stops fetching them
again.

```bash
cd server
python import_library.py --path "/path/to/your/music" --dry-run   # report only
python import_library.py --path "/path/to/your/music"             # write
```

Without `--path` it scans the configured `library` folder. Re-running is safe.

Identity is read from the tags rather than guessed from filenames:

| File written by | Recovered from | Result |
|---|---|---|
| invokr | comment tag `<yt url> \| <source>:<id>` | exact key + video id |
| spotDL | comment tag (YouTube URL) + `WOAS` atom (Spotify URL) | video id + Spotify id |
| anything else | `Artist - Title` filename | keyed by a title/artist hash |

Files with no video id are still caught, because a like also checks the archive
by **title and artist**.

---

## Configuration

`server/config.json`, created on first run:

| Key | Default | Meaning |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8765` | Where the server listens |
| `token` | generated | Shared secret; also goes in the extension settings |
| `library` | `~/Music/invokr` | Where finished files land |
| `output_template` | `{artist} - {title}.{output-ext}` | Filename template |
| `format` | `m4a` | `m4a` keeps the source untouched |
| `bitrate` | `disable` | `disable` = never re-encode |
| `cookie_file` | `null` | Optional `cookies.txt` for YT Music Premium quality |
| `provider_order` | `["itunes","deezer"]` | Metadata sources, in order |
| `download_timeout_seconds` | `900` | Per-download ceiling |
| `review_ttl_minutes` | `30` | How long a review stays valid |
| `auto_accept*` | see below | When to skip the review step |

`library` and `log_dir` accept `~` and environment variables, so
`~/Music/invokr` and `%USERPROFILE%\Music\invokr` both work.

### Filename template variables

`{artist}`, `{artists}`, `{album-artist}`, `{album}`, `{title}`,
`{track-number}`, `{track-count}`, `{disc-number}`, `{year}`, `{isrc}`,
`{source-id}`, `{video-id}`, `{output-ext}`

Path separators create folders, e.g.
`{album-artist}\{album}\{track-number} - {title}.{output-ext}`.

### Tuning auto-accept

All four thresholds must pass:

| Key | Default | Meaning |
|---|---|---|
| `auto_accept` | `true` | Master switch |
| `auto_accept_min_score` | `85.0` | Overall score floor |
| `auto_accept_min_title` | `88.0` | Title similarity floor |
| `auto_accept_min_artist` | `85.0` | Artist similarity floor |
| `auto_accept_min_margin` | `8.0` | Minimum lead over the runner-up |

The margin matters most. `Take On Me` and `Take On Me (MTV Unplugged)` both
score ~100 on title and have compatible durations — only the gap between them
reveals that a human should choose. Raise the margin for fewer interruptions and
more wrong-version picks; lower it for the opposite.

### Audio quality

YouTube's free tier caps audio at roughly 130 kbps (formats `140` m4a and `251`
opus). Nothing here transcodes, so a higher `bitrate` setting would only add a
lossy generation. A YT Music Premium `cookies.txt` raises the source to 256 kbps
— point `cookie_file` at it.

---

## HTTP API

Everything under `/api/*` needs `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Liveness; no auth, leaks nothing |
| `GET` | `/api/health` | Preflight: ffmpeg, library, database, providers |
| `POST` | `/api/review` | `{videoId, ytUrl?, title?, artists?, album?, durationSec?, force?}` |
| `POST` | `/api/unlike` | `{videoId, title?}` — logged only, never deletes |
| `GET` | `/api/reviews` | Reviews waiting for a decision |
| `GET` | `/api/review/{id}` | Full review: candidates, errors, current state |
| `GET` | `/api/review/{id}/art` | `?q=` — cover-art candidates |
| `POST` | `/api/review/{id}/confirm` | `{choice: <index>}` → `{jobId}` |
| `POST` | `/api/review/{id}/manual` | Hand-entered metadata → `{jobId}` |
| `POST` | `/api/review/{id}/skip` | Record a skip |
| `DELETE` | `/api/review/{id}` | Discard without a decision |
| `DELETE` | `/api/reviews` | Discard every pending review |
| `POST` | `/api/library/import` | `{path?, dryRun?, recursive?}` — register existing files |
| `GET` | `/api/jobs` · `/api/jobs/{id}` | Recent jobs / one job |
| `POST` | `/api/jobs/{id}/cancel` | Cancel a download that has not started |
| `GET` | `/api/tracks` · `/api/events` · `/api/queue` | Archive index, event log, queue depth |

`POST /api/review` returns `{"skipped": true}` when the video is already
archived, or `{"auto": true, "jobId": …}` when the match was confident enough to
skip the review. Pass `"force": true` to override the archive check.

---

## Troubleshooting

Start with **Run diagnostics** in the extension settings. It reports every stage
so you can see exactly where something stopped.

| Reading | Meaning |
|---|---|
| `FAILED to reach the content script` | The tab predates the extension. Reload the YouTube Music tab. |
| `like requests seen` stays `0` | The `webRequest` hook is not firing |
| `like requests seen` rises, `likes detected` does not | The request body shape changed |
| `likes detected` rises, no server calls | The server was never reached — check the token and URL |
| A server call with a status | The failure is server-side; see `/api/events` |

The diagnostics also keep two logs: **recent innertube POSTs** (what YouTube
Music actually sent) and **recent dispatch decisions** (what the extension did
with it). Between them, a missed like is no longer a mystery.

**The review panel does not open by itself.**
`action.openPopup()` needs Firefox 149+; earlier builds refuse the call and it
falls back to badging the icon. Diagnostics reports `popup API` accordingly.

**Nothing downloads, the server log says the job failed.**
`http://127.0.0.1:8765/api/events` shows recent errors. Common causes: YouTube
bot checks, a region-locked video, or a video that has been removed.

**`js_runtime` warning.**
yt-dlp warns that YouTube extraction without a JavaScript runtime is deprecated.
Downloads work without one, but installing
[Deno](https://deno.land/) removes the risk of some formats going missing.

---

## Design notes

**Why `webRequest` instead of a DOM observer?** The first version watched the
`like-status` attribute from a `world: "MAIN"` content script. When `world:
"MAIN"` is not honoured, that script runs isolated, `getVideoData()` returns
nothing, `videoId` is null, and every event is dropped **silently** — which is
exactly what happened. Reading the video id off the network request has no such
failure mode and covers every like surface.

**Why not spotDL?** spotDL's `YouTubeURL|SpotifyURL` syntax is elegant, and this
project started there. But `reinit_song()` calls `Song.from_url()` for every
track, so its download path hard-requires the Spotify API — and its bundled
shared Spotify application returns `HTTP 429, Retry-After: 86400`. It also exits
`0` when a download fails, so success has to be inferred from the filesystem
anyway. Doing it directly means real exceptions and no hidden dependency.

**Why iTunes and Deezer, and no Spotify?** Both need no account, and iTunes
returns track number, track count, disc numbers, release date, genre and artwork
in a single call. Spotify was removed: its shared application is rate limited,
and since February 2026 a replacement development-mode app requires the owner to
hold Spotify Premium — for no field the other two lack.

**Success is verified on disk.** The worker computes the expected output path
itself and requires a non-empty file with a fresh mtime. Return codes are never
trusted.

**Un-likes are logged, never acted on.** The archive is append-only, so a
mis-click can never destroy a file.

More background in [`docs/`](docs/):
[YouTube Music like detection](docs/youtube-music-like-detection.md) ·
[spotDL findings](docs/spotdl-findings.md)

---

## Responsible use

Downloading audio from YouTube is contrary to
[YouTube's Terms of Service](https://www.youtube.com/t/terms). This is a
personal archiving tool for music you already have access to; it is not for
building a library you then distribute, and it is not a way to avoid paying for
music.

Metadata comes from the public iTunes and Deezer search APIs. Please be gentle
with them — the default configuration makes one or two requests per like.

You are responsible for how you use this.

---

## License

[MIT](LICENSE) © 2026 Bryce
