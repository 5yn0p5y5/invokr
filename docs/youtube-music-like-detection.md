# YouTube Music "Like" Detection — Technical Research Brief

Scope: Firefox WebExtension (MV3, content script) detecting **LIKE** on music.youtube.com and extracting song metadata.
Confidence tags: **[V]** = verified in a primary source, **[L]** = likely / single-source, **UNVERIFIED** = could not confirm.

---

## 1. DOM of the like button

The player-bar like control is a `ytmusic-like-button-renderer` carrying its state as an **attribute**, not only as ARIA. **[V]**

- `#like-button-renderer` with attribute `like-status` ∈ `LIKE | DISLIKE | INDIFFERENT` — the `LikeType` enum is literally `Dislike='DISLIKE'`, `Indifferent='INDIFFERENT'`, `Like='LIKE'` ([pear-desktop `song-info-front.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/providers/song-info-front.ts), [`datahost-get-state.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/types/datahost-get-state.ts)). A second file in the same project waits for `#like-button-renderer` and reads `like-status == 'DISLIKE'` ([`skip-disliked-songs/index.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/plugins/skip-disliked-songs/index.ts)).
- The project also drives the control programmatically via `#like-button-renderer.updateLikeStatus(status)` ([`renderer.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/renderer.ts)).
- Inner buttons are `yt-button-shape` wrappers around a native `<button aria-pressed>`: a working bulk-dislike script uses `yt-button-shape#button-shape-dislike button[aria-pressed="false"]` ([Qiita, 2024-01-04](https://qiita.com/christ1nu/items/68a41c0878aae4c3921c)). By symmetry `#button-shape-like` exists — **[V]** for the `#button-shape-dislike` string, **[L]** for `#button-shape-like`.
- `document.querySelector('ytmusic-like-button-renderer')` is the generic tag selector for the same element; it persists even when the mini/window-narrow layout prunes the visible Like buttons ([DEV: YTM Mini Player](https://dev.to/kakeroth/how-i-hijacked-youtube-musics-dom-to-build-a-custom-mini-player-3m23)).
- Player bar is `<ytmusic-player-bar>` with attributes `shuffle-on`, `player-fullscreened`, `is-mweb-player-bar-modernization-enabled` **[V]** ([`renderer.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/renderer.ts), [`song-info-front.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/providers/song-info-front.ts)).

Realistic snippet (**reconstructed** from the sources above; inner wrapper elements and exact class lists are **UNVERIFIED**):

```html
<ytmusic-like-button-renderer id="like-button-renderer" like-status="LIKE">
  <yt-button-shape id="button-shape-like" class="style-scope ytmusic-like-button-renderer">
    <button class="yt-spec-button-shape-next yt-spec-button-shape-next--tonal"
            title="Unlike" aria-label="Unlike" aria-pressed="true">
      <div class="yt-spec-button-shape-next__icon"><yt-icon>…filled thumb…</yt-icon></div>
    </button>
  </yt-button-shape>
  <yt-button-shape id="button-shape-dislike" class="style-scope ytmusic-like-button-renderer">
    <button class="yt-spec-button-shape-next yt-spec-button-shape-next--tonal"
            title="Dislike" aria-label="Dislike" aria-pressed="false">
      <div class="yt-spec-button-shape-next__icon"><yt-icon>…outline thumb…</yt-icon></div>
    </button>
  </yt-button-shape>
</ytmusic-like-button-renderer>
```

`title`/`aria-label` flipping to “Unlike” when liked: **UNVERIFIED** — I found no source confirming the label strings for YTM. Use `like-status` / `aria-pressed`, not label text. Note `aria-pressed` is on the **inner `<button>`** while `like-status` is on the **renderer** — reading the wrong one yields `null`.

---

## 2. Detection strategy comparison

| Approach | Player bar | Playlist/album row | "…" context menu | Async delay | Like vs unlike | Double-fire risk |
|---|---|---|---|---|---|---|
| (a) click capture | yes | only row's own control | **misses** (menu item) | fires *before* state settles | must read state separately | low, but `click` can be synth-replayed |
| (b) MutationObserver on `like-status` | **yes** | **no** if the liked track isn't the playing one | **no** (same reason) | fires when UI updates | **yes** (`LIKE`/`DISLIKE`/`INDIFFERENT`) | high — Polymer rewrites the attribute repeatedly |
| (c) `yt-navigate-finish` / `yt-page-type-changed` | n/a | n/a | n/a | n/a | n/a | n/a — it's a *lifecycle* signal |

- `yt-navigate-start` / `yt-navigate-finish` are real site events, discovered via `getEventListeners(document)`; `yt-navigate-finish` is recommended "for other cases" ([SO](https://stackoverflow.com/revisions/2ba8f0ba-a42d-4e01-8b23-d74f5c3ce738/view-source)). `yt-page-type-changed` fires on page-type switches — **UNVERIFIED** (no primary source found).
- (b) is the approach the most mature third-party client uses in production ([`song-info-front.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/providers/song-info-front.ts)). It is the only one that distinguishes LIKE from UNLIKE and naturally absorbs the async round-trip.
- **Blind spot of (b):** liking a track from a playlist/album row or the "…" menu does **not** mutate the player-bar `like-status` unless that row *is* the current track. Catching those requires either (i) the row's `toggleMenuServiceItemRenderer.likeEndpoint.status` (`LIKE`/`DISLIKE`, typed in [`datahost-get-state.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/types/datahost-get-state.ts)) or (ii) intercepting the innertube request itself (§4).
- Best coverage: **(c) to (re)arm + (b) as the state source + request interception for cross-track likes.** Never infer a like from a click alone; never fire on `INDIFFERENT`→`LIKE` re-assertions of the same `(videoId, status)` pair.

---

## 3. Player-state API — how to read the current track

The player API object is the element `#movie_player`, typed `Element & MusicPlayer` and looked up exactly that way by pear-desktop **[V]** ([`renderer.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/renderer.ts)):

```js
const api = document.querySelector('#movie_player');      // player API object
api.getPlayerResponse().videoDetails  // { videoId, title, author, lengthSeconds, musicVideoType, ... }
api.getVideoData()                    // { title, author, video_id, list }  ← video_id, not videoId
api.getWatchNextResponse()
api.addEventListener('videodatachange', (name, data) => {}) // name: 'dataloaded' | 'dataupdated'
api.getCurrentTime(); api.getDuration(); api.getPlayerState(); // 1 playing, 2 paused
```

- `getPlayerResponse().videoDetails` includes `videoId`, `title`, `author`, `lengthSeconds`, `musicVideoType` — the full shape is typed in [`datahost-get-state.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/types/datahost-get-state.ts). **Album is not native**: pear-desktop injects it from `playerOverlays.playerOverlayRenderer.browserMediaSession.browserMediaSessionRenderer.album.runs[0].text` ([`song-info-front.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/providers/song-info-front.ts)).
- `ytmusic-player-bar` is a *different* object exposing UI helpers, not track data: `getState()` (→ `queue.repeatMode`), `queue.shuffle()`, `updateVolume()`, `onRepeatButtonClick()`, `onVolumeClick()` **[V]** ([`renderer.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/renderer.ts), [`song-info-front.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/providers/song-info-front.ts)). `ytmusic-player-bar.playerApi` as such: **UNVERIFIED**. `video-id` attribute on `ytmusic-player-bar`: **UNVERIFIED**.
- `dataloaded` → `dataupdated` can be delayed or missing, so pear-desktop waits for `dataupdated` with a **1500 ms fallback** on `dataloaded` **[V]** ([`song-info-front.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/providers/song-info-front.ts)). Copy this pattern.
- **DOM-only fallback** (works in an ISOLATED content script, no MAIN world needed): `.title.ytmusic-player-bar`, `.byline.ytmusic-player-bar` with `a[href^="channel/"]` → artists and `a[href^="browse/"]` → album, `.time-info.ytmusic-player-bar` → `"cur / total"`, plus `<video>.duration`/`currentTime` **[V]** for the selectors ([Tuna userscript](https://raw.githubusercontent.com/univrsal/tuna/master/deps/tuna_browser.user.js), [YouTube Music Metadata Fix](https://greasyfork.org/zh-TW/scripts/568349-youtube-music-metadata-fix/code)). `navigator.mediaSession.metadata` also carries title/artist/album/artwork **[V]** (same two sources).

**Firefox gotcha [V]:** `#movie_player` is a *page* object; an ISOLATED content script cannot see it (Xray vision — see the SO question ["Firefox extension: can't use variables or functions from element movie_player on ytmusic"](https://stackoverflow.com/questions/73972796/firefox-extension-cant-use-variables-or-functions-from-element-movie-player-on)). You must inject into `world: "MAIN"`.

---

## 4. Liked Music via innertube, and the SAPISIDHASH question

**Yes, it is possible — the premise in the question is wrong.**

- The Liked Music playlist is `POST /youtubei/v1/browse` with `{"browseId": "FEmusic_liked_videos"}`, and the endpoint requires authentication (`_check_auth()`); liked *playlists* use `FEmusic_liked_playlists` **[V]** ([ytmusicapi `mixins/library.py`](https://github.com/sigma67/ytmusicapi/blob/master/ytmusicapi/mixins/library.py)). Note ytmusicapi distinguishes *liked songs* (`FEmusic_liked_videos`) from library songs — different things.
- **SAPISIDHASH is computable.** Algorithm: `"SAPISIDHASH {ts}_{sha1hex(ts + ' ' + sapisid + ' ' + origin)}"`, origin `https://music.youtube.com`, sapisid read from `__Secure-3PAPISID` (preferred) or `SAPISID` **[V]** ([`ytmusic-api` `auth.rs`, mirroring ytmusicapi](https://docs.rs/ytmusic-api/latest/src/ytmusic_api/auth.rs.html)).
- **HttpOnly is not a blocker for an extension.** HttpOnly only hides the cookie from page JavaScript (`document.cookie`). The WebExtensions `cookies` API requires the `"cookies"` permission plus host permissions, and with `*://*.example.com/` the extension may "read or write a secure or non-secure cookie"; the returned `Cookie` type exposes both `value` and an `httpOnly` flag **[V]** ([MDN `cookies`](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/cookies), [MDN `cookies.Cookie`](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/cookies/Cookie)). So `browser.cookies.get({url:"https://music.youtube.com", name:"__Secure-3PAPISID"})` → value → SHA-1 in an offscreen/background worker. Confidence: **high** on capability, **medium** on sufficiency — ytmusicapi's `browser.json` also ships `X-Goog-AuthUser`, `x-origin`, `Cookie`, and `Authorization` together, so the hash alone may not be enough **[V]** ([ytmusicapi browser auth](https://ytmusicapi.readthedocs.io/en/latest/setup/browser.html)).
- **Much simpler route [L→V]:** the page exposes its own authenticated innertube client — `document.querySelector('ytmusic-app').networkManager.fetch(url, data)`, typed generically in pear-desktop [`music-player-app-element.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/types/music-player-app-element.ts) and used in the wild as `networkManager.fetch('/search', {...})` and `networkManager.fetch('/music/get_queue', {...})` ([`renderer.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/renderer.ts)). Calling `networkManager.fetch('/browse', {browseId:'FEmusic_liked_videos'})` from a MAIN-world script delegates all auth to the page. **UNVERIFIED** for `/browse` + that exact body (only `/search` and `/music/get_queue` are observed).
- **Recommendation:** do **not** poll. Use the DOM/observer as the source of truth for "user just liked"; use the API only for reconciliation/backfill on demand.

---

## 5. Firefox MV3 specifics

- **MutationObserver and click capture: fully available** in content scripts; nothing site- or Firefox-specific blocks them. Content scripts are ordinary scripts with DOM access ([MDN `content_scripts`](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/content_scripts)).
- **`content_scripts.matches`**: use `*://music.youtube.com/*`. `*://*.youtube.com/*` also matches www + music — convenient because YouTube Music SPA links stay on-host, but it over-injects on youtube.com. `matches` is the only mandatory key; add `all_frames: true` only if you need iframe coverage (it also injects into ad/tracker frames). **[V]**
- **SPA gotcha [V]:** on YouTube the URL changes via `history.pushState` and **content scripts are not re-injected** — inject once at `document_start` and handle the whole session yourself ([SO](https://stackoverflow.com/revisions/2ba8f0ba-a42d-4e01-8b23-d74f5c3ce738/view-source)).
- **`world: "MAIN"` [V]:** supported for MV3 content scripts and `scripting.executeScript` from **Firefox 128**; MAIN-world scripts get **no WebExtension APIs**, and unlike `window.eval` they are **not blocked by the page CSP** ([Mozilla Add-ons blog](https://blog.mozilla.org/addons/2024/07/10/manifest-v3-updates-landed-in-firefox-128/), [MDN `world`](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/content_scripts#world)). Firefox historically refused `world` and enforced Xray vision ([DEV: Firefox and content_scripts.world](https://dev.to/costinmanda/firefox-and-contentscriptsworld-29h9), [Xray vision docs](https://firefox-source-docs.mozilla.org/dom/scriptSecurity/xray_vision.html)) — so if you must support pre-128 Firefox, fall back to injecting a `<script>` tag from the ISOLATED world. Bridge MAIN→ISOLATED with `window.postMessage` or a `CustomEvent`; **never** pass the `#movie_player` object across the boundary — pass primitives.
- **`browser.storage`**: promise-based (`await browser.storage.local.get(...)`), no callback form needed; available in both worlds in the background/ISOLATED side. Not usable from MAIN. **[V]** (MDN WebExtensions baseline.)

---

## 6. Failure modes to design around

1. **Song changes while observing.** Read `videoId` at the *same instant* you read `like-status`; if `videodatachange` fired in between, drop the event. Reset per-track state on every `dataupdated`/`dataloaded`.
2. **Like button not yet rendered / pruned.** YTM strips Like/Dislike from narrow window layouts, and the renderer is re-created on navigation. Use a `waitForElement` helper (as in [`skip-disliked-songs`](https://github.com/pear-devs/pear-desktop/blob/master/src/plugins/skip-disliked-songs/index.ts)) plus re-attach on `yt-navigate-finish` and on `childList` mutation of the player bar. **A disconnected observer silently stops reporting.**
3. **Ads.** During ad playback `getPlayerResponse().videoDetails` describes the *ad*; pear-desktop's player state type carries `adPlaying` **[V]** ([`datahost-get-state.ts`](https://github.com/pear-devs/pear-desktop/blob/master/src/types/datahost-get-state.ts)). Suppress events while `adPlaying` is true.
4. **"Save to library" ≠ "Like".** Distinct operations: `rate_song(videoId, LIKE|DISLIKE|INDIFFERENT)` hits `like/*`, whereas library membership uses `edit_song_library_status(feedbackTokens)` → `feedback`; for playlists/albums "Add to library" *is* `rate_playlist` **[V]** ([`library.py`](https://github.com/sigma67/ytmusicapi/blob/master/ytmusicapi/mixins/library.py)). Filter on LIKE semantics only, and never treat a `toggleMenuServiceItemRenderer` labelled "Save to library" as a like.
5. **Duplicate events from SPA re-renders.** Polymer rewrites `like-status` repeatedly with the same value. Dedupe on `(videoId, status)` with a last-emitted guard, and additionally ignore any transition into `INDIFFERENT` (that's an un-like, not a like — but do report it if you want un-like).
6. **State change ≠ server acceptance.** YTM sometimes fails to apply a rating even on manual clicks, especially when clicks are fired rapidly ([Qiita](https://qiita.com/christ1nu/items/68a41c0878aae4c3921c)). If you need certainty, confirm via the API rather than trusting the optimistic UI flip.
7. **Non-local state changes.** The like status is persistent across launches (pear-desktop emits the *initial* value on setup **[V]**), and another device/tab can change it — the observer will fire without any local user action. Gate on "an interaction happened in this tab recently" if you only want user-initiated likes.
8. **INDIFFERENT ↔ LIKE cycles** are legitimate (unlike then re-like). Key your dedupe on transitions, not absolute values.

---

## Recommended design

**Two content scripts, three concerns.**

```
MANIFEST (MV3)
  content_scripts:
    - matches: ["*://music.youtube.com/*"]
      js: ["ytm-main.js"]          # world: "MAIN"
      world: "MAIN"
      run_at: "document_start"     # SPA: inject once, run for the whole session
    - matches: ["*://music.youtube.com/*"]
      js: ["ytm-bridge.js"]        # world: "ISOLATED"  (default)
      run_at: "document_start"
  permissions: ["storage"]         # add "cookies" + host perms ONLY if you poll the API
```

```js
/* ---------- ytm-main.js  (world: MAIN) — event source, no extension APIs ---------- */
const post = (type, detail) => window.postMessage({ __ytm: true, type, detail }, location.origin);

let api = null, currentVideoId = null, adPlaying = false;

function songFromDom() {                       // ISOLATED-safe fallback also exists
  const t = document.querySelector('.title.ytmusic-player-bar');
  const b = document.querySelector('.byline.ytmusic-player-bar');
  const time = document.querySelector('.time-info.ytmusic-player-bar')?.textContent.split('/');
  const artists = [...(b?.querySelectorAll('a[href^="channel/"]') ?? [])].map(a => a.textContent.trim());
  const album   = b?.querySelector('a[href^="browse/"]')?.textContent.trim() ?? '';
  return { title: t?.textContent.trim() ?? '', artists, album,
           durationText: time?.[1]?.trim() ?? null };
}

function attachPlayer() {
  api = document.querySelector('#movie_player');
  if (!api) return false;
  const video = document.querySelector('video');
  if (video && !Number.isNaN(video.duration)) {
    const vd = api.getVideoData();
    currentVideoId = vd.video_id;
    post('track', { videoId: currentVideoId, title: vd.title, author: vd.author,
                    playlistId: vd.list, lengthSeconds: api.getDuration() });
  }
  api.addEventListener('videodatachange', (name, data) => {
    if (name !== 'dataupdated' && name !== 'dataloaded') return;
    currentVideoId = data?.videoId ?? api.getVideoData?.().video_id ?? null;
    // ad detection: getAdState() on the player API, or player.adPlaying via ytmusic-app state
    adPlaying = typeof api.getAdState === 'function' ? !!api.getAdState() : false;
    if (adPlaying) return;                       // suppress metadata/like attribution during ads
    post('track', { videoId: currentVideoId, title: data?.title });
  });
  return true;
}

/* like-status observer: single source of truth for LIKE / DISLIKE of the playing track */
let observer = null, likeEl = null;
function attachLike() {
  const el = document.getElementById('like-button-renderer');   // = ytmusic-like-button-renderer
  if (!el || el === likeEl) return;
  observer?.disconnect();
  likeEl = el;
  observer = new MutationObserver(() => {
    if (adPlaying) return;                                      // never attribute an ad to a song
    const status = el.getAttribute('like-status');              // 'LIKE' | 'DISLIKE' | 'INDIFFERENT'
    // read the videoId in the SAME tick to avoid attributing a like to the next song
    const videoId = api?.getVideoData?.().video_id ?? currentVideoId;
    post('like-status', { status, videoId, song: songFromDom(), at: Date.now() });
  });
  observer.observe(el, { attributes: true, attributeFilter: ['like-status'] });
}

/* SPA lifecycle + late-render handling */
document.addEventListener('yt-navigate-finish', () => { attachPlayer(); attachLike(); });
new MutationObserver(() => { if (!api) attachPlayer(); attachLike(); })
  .observe(document.documentElement, { childList: true, subtree: true });
```

```js
/* ---------- ytm-bridge.js  (world: ISOLATED) — dedupe, enrich, persist ---------- */
let lastEmitted = null;      // `${videoId}:${status}`
let lastTrack = null;

window.addEventListener('message', (e) => {
  if (e.source !== window || e.origin !== location.origin || !e.data?.__ytm) return;
  const { type, detail } = e.data;

  if (type === 'track') {
    lastTrack = detail;
    // a new track invalidates the dedupe key: the same "LIKE" on a new song is a new event
    if (lastEmitted && !lastEmitted.startsWith(`${detail.videoId}:`)) lastEmitted = null;
    return;
  }

  if (type !== 'like-status') return;
  const { status, videoId, song } = detail;

  if (status === 'INDIFFERENT') {                 // un-like / cleared
    if (lastEmitted === `${videoId}:INDIFFERENT`) return;
    lastEmitted = `${videoId}:INDIFFERENT`;
    emit({ action: 'UNLIKE', videoId, song });    // or drop entirely if you only care about likes
    return;
  }
  if (status !== 'LIKE') return;                  // NEVER fire on DISLIKE

  const key = `${videoId}:LIKE`;
  if (key === lastEmitted) return;                // duplicate Polymer re-render
  if (videoId !== lastTrack?.videoId) return;     // track changed mid-flight → not our like
  lastEmitted = key;

  emit({ action: 'LIKE', videoId, song: merge(song, lastTrack) });
});

const emit = (e) => browser.runtime.sendMessage(e);   // background → browser.storage.local / user config
```

**Coverage notes.** This design reliably covers liking **from the player bar** (main case). For **playlist/album row likes** and the **"…" context menu** on a non-playing track, add one of:
- *optional, highest coverage:* in the MAIN script, wrap `XMLHttpRequest.prototype.open/send` and `window.fetch` to sniff `/youtubei/v1/like/like` and `/like/remove` and read `JSON.parse(body).target.videoId` — the request body shape is `{"target": {"videoId": ...}}` **[V]** ([ytmusicapi `rate_song`](https://github.com/sigma67/ytmusicapi/blob/master/ytmusicapi/mixins/library.py)). This catches every entry point and needs no DOM at all; it also removes the need for `yt-navigate-finish`, though you still want the lifecycle event for track identity.
- *optional, API side:* a MAIN-world call to `document.querySelector('ytmusic-app').networkManager.fetch('/browse', { browseId: 'FEmusic_liked_videos' })` for reconciliation, **without polling**.

### Confidence summary

| Claim | Confidence |
|---|---|
| `#like-button-renderer` + `like-status` ∈ LIKE/DISLIKE/INDIFFERENT | **High** — two files in a shipped project |
| `yt-button-shape#button-shape-dislike > button[aria-pressed]` | **High** — working userscript |
| `#button-shape-like` | Medium — inferred by symmetry; not directly observed |
| `aria-label`/`title` = "Unlike" when liked | **UNVERIFIED** — do not build on it |
| MutationObserver on `like-status` is the right primary signal | **High** — production practice, but blind to non-playing-row likes |
| `#movie_player` = player API; `getPlayerResponse()`/`getVideoData()`/`videodatachange` | **High** — typed + used in production |
| Requires `world: "MAIN"` in Firefox (Xray vision) | **High** |
| `yt-navigate-finish` / `yt-navigate-start` exist; content scripts aren't re-injected | **High** |
| `yt-page-type-changed` exists | **UNVERIFIED** |
| `ytmusic-player-bar.playerApi` / `video-id` attribute | **UNVERIFIED** |
| `FEmusic_liked_videos` on `POST /browse`, auth required | **High** — ytmusicapi source |
| SAPISIDHASH formula + origin + `__Secure-3PAPISID` | **High** — mirrored reference implementation with test vectors |
| Extension can read HttpOnly cookies via `browser.cookies` | **High** for the API contract; the *sufficiency* of hash-only auth is Medium |
| `ytmusic-app.networkManager.fetch()` exists and is authenticated | **High** for existence; **UNVERIFIED** for `/browse` arg shape |
| Firefox 128+ supports `world: "MAIN"` in MV3 | **High** — Mozilla blog + MDN |
| LIKE vs "Save to library" are different operations | **High** — distinct ytmusicapi endpoints |
| `ytmusic-player-bar` class also on `.title`/`.byline`/`.time-info` nodes | Medium — two independent userscripts agree, no official doc |
