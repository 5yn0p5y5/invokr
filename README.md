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
Firefox (music.youtube.com)                 Local server (127.0.0.1:8765)
┌────────────────────────────────┐           ┌────────────────────────────────┐
│ background.js                  │           │ POST /api/review               │
│   • three signals, deduped     │           │   → search iTunes + Deezer     │
│     on (action, videoId)       │ Bearer    │   → rank the candidates        │
│   • posts, badges the icon     │ ────────► │   → auto-download, or ask      │
│                                │ token     │                                │
│ ytm-bridge.js (content)        │           │ GET  /api/reviews              │
│   • like-status + clicks,      │           │ GET  /api/review/{id}          │
│     read with the videoId      │           │   → candidates as JSON         │
│                                │           │                                │
│ ytm-net.js (page world)        │           │ worker: yt-dlp + mutagen       │
│   • reads the like POST        │           │   → download the exact video   │
│     as the page sends it       │           │   → write tags + cover art     │
│                                │           │   → verify the file on disk    │
│ popup.js (toolbar panel)       │           │                                │
│   • renders candidates,        │           │ SQLite: tracks, jobs, events   │
│     manual metadata, art       │           │                                │
│   • confirms / skips           │           │                                │
└────────────────────────────────┘           └────────────────────────────────┘
```

The audio **always** comes from the video you liked. The catalogue is only used
for metadata — title, artist, album, year, track number, genre and cover art.

Two design choices are worth knowing up front:

- **Likes are detected three times over, and de-duplicated.** The player bar's
  `like-status` attribute is the source of truth for like versus un-like; a
  click on the control is captured before YouTube Music's own handler runs, so
  the first like of a freshly started track is never mistaken for the baseline;
  and [ytm-net.js](extension/ytm-net.js) reads the `/youtubei/v1/like/*` POST
  inside the page, which is the only way to see a like of a track that is not
  playing. `webRequest` is still registered as a fourth signal, but Firefox
  does not reliably hand over request bodies, so nothing depends on it.
  Which song a like belongs to comes from the page's own player rather than the
  address bar, so it also works while browsing — where the URL has no `?v=` —
  and with the player bar collapsed.
- **Obvious matches download without asking.** The server scores every
  candidate and only opens the review panel when something is genuinely
  ambiguous. See [tuning auto-accept](#tuning-auto-accept).

---

## Requirements

| | |
|---|---|
| **Python** | 3.10 or newer |
| **ffmpeg** | must be on `PATH` — `winget install Gyan.FFmpeg` / `brew install ffmpeg` / `apt install ffmpeg` |
| **Firefox** | 128 or newer — the page-world sniffer needs `world: "MAIN"`. **149+** to have the review panel open automatically |
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

### 3. Install the extension

Download the latest `.xpi` from
[Releases](https://github.com/5yn0p5y5/invokr/releases) and open it, or use
`about:addons` → gear icon → **Install Add-on From File…**. That build carries a
Mozilla signature, so Firefox keeps it across restarts and there is nothing to
change in `about:config`. Firefox 140 and newer also show the declared
data-collection state on the install prompt — this build declares `none`,
because nothing leaves your machine.

> Working on the code instead? `about:debugging#/runtime/this-firefox` →
> **Load Temporary Add-on…** → pick `extension/manifest.json` for
> edit-and-reload with no packaging. That path is disposable by design: Firefox
> drops temporary add-ons the moment it restarts.

### 4. Connect them

Open the extension's settings (click the toolbar icon → **Settings**) and paste:

- **Server URL** — `http://127.0.0.1:8765`
- **Bearer token** — the `token` value from `server/config.json`

Press **Test connection**. It should say *Server reachable and ready.*

> **Serving from another machine?** Set **Server URL** to that machine's address
> (for example `http://192.168.1.50:8765`) and press **Save**. Firefox asks once
> whether invokr may access that host; approve it, then press **Test
> connection**. The grant covers that one host and can be reviewed or revoked in
> `about:addons` → invokr → **Permissions**. Only `127.0.0.1` and `localhost`
> are pre-authorized. The server also has to listen on an address the other
> machine can reach — see `host` under [Configuration](#configuration).

Where the archive lives is a server setting rather than an extension one — the
same settings page has an **Archive location** section when you want to move it
off the default folder. See [the master folder](#the-master-folder).

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

**A download that fails is retried once, a second later.** Most failures are
transient — a YouTube bot check, a dropped connection — and the same job
usually works on the second run. If both attempts fail, the extension says so
and **opens the panel**, where the reason is shown with a **Retry** button. The
failure is not lost if the panel cannot be opened (an older Firefox, or another
panel already up): a notification is sent either way, and the failed job is
still listed in the panel the next time you open it.

**And it clears itself when you fix it the obvious way.** Liking the song again
is the natural response to a failed download, and it works — but it does not
retry the job, it runs the whole like path and queues a *new* one. The old job
row stays `failed` for ever, which used to leave the panel insisting three
downloads had failed when all three were sitting in the library. A failure is
now only reported while the track it belongs to has no file, so re-liking clears
it, and a `Retry` on something already archived is refused rather than
downloaded twice.


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
| `host` / `port` | `127.0.0.1` / `8765` | Where the server listens. Use the machine's LAN IP, or `0.0.0.0` for every interface, to serve other devices |
| `token` | generated | Shared secret; also goes in the extension settings |
| `root` | *(unset)* | **The master folder.** Everything below derives from it |
| `library` | `<root>/library` | Where finished files land; set it only to override `root` |
| `db` | `<root>/db/invokr.db` | The index; set it only to override `root` |
| `log_dir` | `<root>/logs` | Logs; set it only to override `root` |
| `output_template` | `{artist} - {title}.{output-ext}` | Filename template |
| `format` | `m4a` | `m4a` keeps the source untouched |
| `bitrate` | `disable` | `disable` = never re-encode |
| `cookie_file` | `null` | Optional `cookies.txt` for YT Music Premium quality |
| `provider_order` | `["itunes","deezer"]` | Metadata sources, in order |
| `download_timeout_seconds` | `900` | Per-download ceiling |
| `review_ttl_minutes` | `30` | How long a review stays valid |
| `backup_interval_minutes` | `360` | Index snapshot cadence; `0` turns it off |
| `backup_keep` | `7` | How many snapshots to keep |
| `auto_accept*` | see below | When to skip the review step |

Paths accept `~` and environment variables, so `~/Music/invokr` and
`%USERPROFILE%\Music\invokr` both work.

### The master folder

One folder holds everything invokr owns:

```
Z:\music\
├── library\     the audio
├── db\          invokr.db, plus backups\
└── logs\
```

Set `root` and those three follow it. Because nothing lives outside, the archive
becomes a folder you can copy to another drive or machine and keep using — the
only thing that changes is this one path.

**Set it from the extension:** Settings → **Archive location** → *Load from
server* to see the current layout, type the folder, **Apply on server**. The
server validates before changing anything: the path has to be absolute, the drive
or share has to be reachable, and the folder has to be creatable and writable —
so a typo, or a NAS that is not mounted, is refused with a reason instead of
being silently accepted. Editing `root` in `server/config.json` by hand works
too, and both routes retire the per-path overrides above, which would otherwise
quietly outrank `root`.

Pointing the server at a new folder never moves or deletes files. If the new
folder has no database yet and the old one does, the existing index is **copied**
across (never moved), so a mistake here cannot lose the original.

**Tracked paths are stored relative to the library**, so moving the archive
normally needs nothing else. The exception is an index written by an earlier
version, which holds absolute paths — after physically moving those files,
re-point them once:

```bash
python import_library.py --rebase-from "/old/path/to/library"
```

A row is rewritten only when its recorded path genuinely starts with that old
folder, so it never guesses which file was meant, and files that live outside the
library are left alone. Plain `python import_library.py` also repairs this, by
re-registering everything from its tags.

#### On a network share

Pointing `root` at a NAS works, with two things worth knowing:

- **The journal switches itself.** SQLite's WAL mode assumes locking and
  shared-memory behaviour that SMB and NFS do not provide, and a lock that
  silently does nothing can corrupt a database. When the index lands on a network
  path the server drops back to the rollback journal (`DELETE`) on its own.
  `/api/health`, `/api/config` and the settings page all report which mode is in
  use.
- **Snapshots run on a timer.** Every `backup_interval_minutes` — and once at
  startup when the index is on a share — the server writes a `VACUUM INTO`
  snapshot into `<root>/db/backups/`, keeping the newest `backup_keep`. The index
  is also rebuildable from the tags on disk, so it is never the only copy of
  anything.

Keep one server process as the only writer. Two servers sharing a database on a
share is the case SQLite is genuinely bad at.

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
| `GET` | `/api/config` | Where the archive lives, and the storage decisions in force |
| `PATCH` | `/api/config` | `{root}` — move the archive to a different master folder |
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
| `GET` | `/api/jobs` · `/api/jobs/{id}` | Recent jobs / one job, with `outstanding` on a failure |
| `GET` | `/api/failures` | Failed downloads whose track still has no file |
| `POST` | `/api/jobs/{id}/cancel` | Cancel a download that has not started |
| `POST` | `/api/jobs/{id}/retry` | Re-queue a failed or cancelled download |
| `GET` | `/api/tracks` · `/api/events` · `/api/queue` | Archive index, event log, queue depth |
| `GET` | `/api/stats` | Whole-database totals: `{tracks, playable, by_status}` |

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
| `page script present` is `false` | The page-world sniffer did not load. Firefox 128+ and a reloaded tab are required. |
| `clicks on like control` rises, `accepted by signal` does not | The click was seen but the state never changed — YouTube Music did not apply the rating. |
| `accepted by signal` shows only `page-network` | The DOM path is not seeing the player bar; check `like renderer found` / `in-player-bar`. |
| `dropped transitions` is non-empty | A track change raced a like-status rewrite; the event was discarded rather than charged to the wrong song. |
| `likes detected` rises, no server calls | The server was never reached — check the token and URL |
| A server call with a status | The failure is server-side; see `/api/events` |

The diagnostics also keep two logs: **recent innertube POSTs** (what YouTube
Music actually sent, with the body kind and a raw preview for like requests)
and **recent dispatch decisions** (what the extension did with it, and through
which of the signals). Between them, a missed like is no longer a mystery.

**The review panel does not open by itself.**
`action.openPopup()` needs Firefox 149+; earlier builds refuse the call and it
falls back to badging the icon. Diagnostics reports `popup API` accordingly.

**Nothing downloads, the server log says the job failed.**
Every download is attempted twice, a second apart, before it is reported — so a
job in this state has already had its second chance. The panel shows the reason
with a **Retry** button, which queues a fresh pair of attempts.
`http://127.0.0.1:8765/api/events` shows the same thing as `download_failed` and
`download_retry` events. Common causes: YouTube
bot checks, a region-locked video, or a video that has been removed.

**The panel still complains about a song you have since downloaded.**
It should not — the failure is dropped as soon as the track has a file, so
`GET /api/failures` is the authoritative list. If a failure lingers, its track
row is still marked `failed` with no path: check
`http://127.0.0.1:8765/api/tracks` for that song. Re-liking it queues a new job
and clears the old complaint.

**The archive looks empty after you moved it.**
The index is still pointing at the old paths. Set the new master folder
(Settings → **Archive location** → **Apply on server**), then re-point the rows
recorded before the move, once: `python import_library.py --rebase-from
"/old/path/to/library"`. See [the master folder](#the-master-folder).

**`js_runtime` warning.**
yt-dlp warns that YouTube extraction without a JavaScript runtime is deprecated.
Downloads work without one, but installing
[Deno](https://deno.land/) removes the risk of some formats going missing.

---

## Design notes

**Why three signals instead of one?** Every single-signal version of this has
failed in production, each for a different reason. The original watched
`like-status` from a `world: "MAIN"` content script; when `world: "MAIN"` was
not honoured the script ran isolated, the player API was invisible, `videoId`
was `null`, and every event was dropped **silently**. It was replaced by a
`webRequest` body read — which turned out to be worse: Firefox only fills in
`details.requestBody` when it can reach the upload stream, and
`WebRequestUpload.createRequestBody` returns form data with no raw bytes
whenever the body parses as form encoded. Diagnostics showed
`/youtubei/v1/like/like` arriving with `no videoId in body` for **every** like,
so detection had quietly fallen back to the DOM observer alone.

That observer then dropped roughly one like in five, for a subtler reason. It
took the first `like-status` value it ever saw for a video id as a "baseline"
and returned early, on the theory that the first value is whatever YouTube
Music rendered. But YouTube Music does not rewrite the attribute when the value
has not changed, so a track that started out `INDIFFERENT` — the common case —
never got a baseline at all, and the user's own like arrived as a first
observation. It was discarded as if it were pre-existing state.

The current design removes the guesswork instead of adding more of it: a poll
records a baseline for whatever is playing within a second, a capture-phase
click listener records the pre-click state before YouTube Music can change it,
and a flip is only reported once it has survived a short delay with the same
track still playing.

**Why keep `webRequest` at all?** It is registered, costs one listener, and is
a fourth independent signal on the rare occasion Firefox does hand over the
body. Nothing depends on it.

**The fiddly parts have regression tests.** `node tools/test-like-detection.js`
loads the content script against a fake DOM and drives it through the scenarios
that have actually broken in the field — a track whose `like-status` is never
rewritten, a click on a track with no baseline yet, and a track change that
races an attribute rewrite. `python tools/test-download-retry.py` does the same
for the download retry, against a temporary database with a scripted download.
Neither needs a browser, a network, or a real archive.

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
