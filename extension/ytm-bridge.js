/**
 * invokr — content script on music.youtube.com.
 *
 * Runs in the ordinary, isolated content-script world. Nothing here needs
 * page-world access; the one signal that does lives in `ytm-net.js`.
 *
 * Detection is deliberately redundant. Three independent signals funnel into
 * the same `{type: "invokr/like"}` message, and the background page
 * de-duplicates them on `(action, videoId)`:
 *
 *   1. `like-status` on the player-bar like renderer, read *together with* the
 *      video id. This is the source of truth for LIKE vs UNLIKE, but the
 *      attribute is rewritten during track changes, so a flip is only reported
 *      once it has survived `CONFIRM_MS` and the track has not moved.
 *   2. A click on a like control, seen in the capture phase before YouTube
 *      Music's own handler runs. That gives the pre-click state, which is what
 *      makes the first like of a freshly started track detectable at all.
 *   3. `ytm-net.js`, which reads the `/youtubei/v1/like/*` POST inside the page
 *      and is the only signal that sees a like of a track that is not playing.
 *
 * The history matters here. The first version of this file treated the first
 * `like-status` value it ever saw for a video id as a "baseline" and returned
 * early. Because YouTube Music does not rewrite the attribute when the value
 * does not change, a track that started out INDIFFERENT — the common case —
 * never got a baseline, so the user's own like arrived as a first observation
 * and was thrown away. That is the bug this file exists to not reintroduce:
 * the poll below records the baseline for the current track straight away, and
 * the click handler records it before the state has a chance to move.
 */
(() => {
  "use strict";

  const VIDEO_ID_RE = /[?&]v=([A-Za-z0-9_-]{11})/;
  const VIDEO_ID_ONLY_RE = /^[A-Za-z0-9_-]{11}$/;
  const TOAST_HINT = /(liked|like|library|added to|removed from|saved to)/i;
  const NET_MARKER = "invokr/network-like";

  // How long a like-status flip has to survive before it is reported. During a
  // track change the renderer is rewritten while the player bar still shows the
  // previous song; waiting is what keeps that rewrite from being charged to a
  // song that is no longer playing.
  const CONFIRM_MS = 300;

  // How long after a click to look at the renderer again. YouTube Music flips
  // the attribute optimistically, then may rewrite it once the server answers.
  const CLICK_SETTLE_MS = 450;

  // The safety net. Every tick records a baseline for whatever is playing, so a
  // track that was never observed changing still has one before the user acts.
  const POLL_MS = 1000;

  // Bound the per-video maps. A long session can visit hundreds of tracks.
  const MAX_TRACKS = 200;

  const state = {
    startedAt: Date.now(),
    syncs: 0,
    likeStatusChanges: 0,
    likeStatusLast: null,
    lastSync: null,
    clicks: 0,
    toastSignals: 0,
    networkSignals: 0,
    networkUnreadable: 0,
    snifferPresent: false,
    playerTrackId: null,
    discarded: [],
    toasts: [],
    errors: [],
  };

  const known = new Map(); // videoId -> last like-status YouTube Music was seen in
  const emitted = new Map(); // videoId -> last status we reported

  const txt = (node) => (node && node.textContent ? node.textContent.trim() : "");

  function note(error) {
    state.errors.push({
      at: Date.now(),
      message: String(error && error.message ? error.message : error),
    });
    if (state.errors.length > 10) state.errors.shift();
  }

  function remember(map, key, value) {
    map.set(key, value);
    if (map.size > MAX_TRACKS) {
      const oldest = map.keys().next().value;
      if (oldest !== undefined) map.delete(oldest);
    }
  }

  // ------------------------------------------------------------- metadata

  // The playing track as reported by the page's own player (ytm-net.js). It is
  // the only identifier available when the URL has no ?v= and the player bar is
  // collapsed, and it is more trustworthy than either on a browse page.
  let playerTrack = null;

  function videoIdFromDom() {
    const bar = document.querySelector("ytmusic-player-bar");
    const link = bar && bar.querySelector('a[href*="watch?v="]');
    const fromLink = link && VIDEO_ID_RE.exec(link.getAttribute("href") || "");
    if (fromLink) return fromLink[1];

    // Before the URL: on a browse page the URL has no video id at all, so
    // falling through to it silently produced a like with no track.
    if (playerTrack && playerTrack.videoId) return playerTrack.videoId;

    const fromUrl = VIDEO_ID_RE.exec(window.location.href);
    return fromUrl ? fromUrl[1] : null;
  }

  function mediaSessionTrack() {
    try {
      const meta = navigator.mediaSession && navigator.mediaSession.metadata;
      if (!meta) return null;
      return { title: meta.title || "", artist: meta.artist || "", album: meta.album || "" };
    } catch (_) {
      return null;
    }
  }

  function readTrack() {
    const bar = document.querySelector("ytmusic-player-bar");
    const byline = bar && bar.querySelector(".byline");

    const artists = byline
      ? Array.from(byline.querySelectorAll('a[href^="channel/"]')).map(txt).filter(Boolean)
      : [];
    const album = txt(byline && byline.querySelector('a[href^="browse/"]'));

    let title = txt(bar && bar.querySelector(".title"));

    const timeInfo = txt(bar && bar.querySelector(".time-info"));
    let seconds = 0;
    if (timeInfo.includes("/")) {
      const parts = timeInfo.split("/").pop().trim().split(":").map((n) => parseInt(n, 10));
      if (parts.length && !parts.some(Number.isNaN)) {
        seconds = parts.reduce((total, n) => total * 60 + n, 0);
      }
    }

    // The media session is a useful backstop when the player bar is in a
    // compact layout and its .title node is absent.
    if (!title || !artists.length) {
      const media = mediaSessionTrack();
      if (media) {
        title = title || media.title;
        if (!artists.length && media.artist) artists.push(media.artist);
      }
    }

    const videoId = videoIdFromDom();

    // A collapsed player bar carries little or nothing the metadata reader can
    // use, so fall back to what the player says when it is the same video.
    if (playerTrack && playerTrack.videoId === videoId) {
      title = title || playerTrack.title;
      if (!artists.length && playerTrack.author) artists.push(playerTrack.author);
    }

    return {
      videoId,
      ytUrl: videoId ? `https://music.youtube.com/watch?v=${videoId}` : window.location.href,
      title,
      artists,
      album,
      durationSec: seconds || null,
    };
  }

  // ------------------------------------------------------- like status DOM

  let likeEl = null;
  let likeObserver = null;

  /**
   * Scoped to the player bar first. A bare
   * `document.querySelector("ytmusic-like-button-renderer")` returns whichever
   * renderer comes first in the document, which is not necessarily the one the
   * player bar is showing.
   */
  function likeRenderer() {
    return (
      document.querySelector("ytmusic-player-bar #like-button-renderer") ||
      document.querySelector("ytmusic-player-bar ytmusic-like-button-renderer") ||
      document.getElementById("like-button-renderer") ||
      document.querySelector("ytmusic-like-button-renderer")
    );
  }

  function readLikeStatus() {
    const el = likeRenderer();
    return el ? el.getAttribute("like-status") : null;
  }

  function send(payload) {
    try {
      const result = browser.runtime.sendMessage({ type: "invokr/like", payload });
      if (result && typeof result.catch === "function") result.catch(note);
      return result;
    } catch (error) {
      note(error);
      return null;
    }
  }

  /**
   * Make sure we are observing the renderer the player bar is currently using.
   *
   * Deliberately does not call `sync`: `sync` calls this, and the mutual
   * recursion would spin forever on a renderer that has been detached but not
   * yet replaced.
   */
  function ensureObserver() {
    const el = likeRenderer();
    if (!el) return null;
    if (el === likeEl && likeObserver && el.isConnected) return el;

    if (likeObserver) likeObserver.disconnect();
    likeEl = el;
    likeObserver = new MutationObserver(() => {
      state.likeStatusChanges += 1;
      sync("like-status");
    });
    likeObserver.observe(el, { attributes: true, attributeFilter: ["like-status"] });
    return el;
  }

  function attachLike() {
    if (!ensureObserver()) return;
    sync("attached");
  }

  /**
   * Read the player state and decide whether anything changed.
   *
   * The first value seen for a track is the baseline — whatever YouTube Music
   * rendered — and is never reported. The poll in `boot` makes sure that
   * baseline is recorded long before the user clicks anything.
   */
  function sync(reason) {
    state.syncs += 1;
    ensureObserver();

    const track = readTrack();
    const status = readLikeStatus();
    state.lastSync = { reason, videoId: track.videoId, status, at: Date.now() };
    state.likeStatusLast = { status, at: Date.now() };

    if (!track.videoId || !status) return;

    const previous = known.get(track.videoId);
    if (previous === undefined) {
      remember(known, track.videoId, status);
      return;
    }
    if (previous === status) return;

    scheduleConfirm(track.videoId, status, reason);
  }

  let pending = null;

  function scheduleConfirm(videoId, status, reason) {
    if (pending && pending.videoId === videoId && pending.status === status) return;
    if (pending) clearTimeout(pending.timer);
    pending = {
      videoId,
      status,
      reason,
      timer: setTimeout(() => {
        pending = null;
        confirm(videoId, status, reason);
      }, CONFIRM_MS),
    };
  }

  /** Run a scheduled confirmation now instead of waiting out `CONFIRM_MS`. */
  function flushPending(reason) {
    if (!pending) return;
    const { videoId, status } = pending;
    clearTimeout(pending.timer);
    pending = null;
    confirm(videoId, status, reason);
  }

  function confirm(videoId, status, reason) {
    const track = readTrack();
    if (track.videoId !== videoId) {
      // The player moved while we waited, so the attribute we saw belonged to a
      // renderer being repurposed for the next track.
      state.discarded.push({
        at: Date.now(),
        videoId,
        status,
        nowPlaying: track.videoId,
        reason,
      });
      if (state.discarded.length > 10) state.discarded.shift();
      return;
    }

    // It flipped again while we waited; that flip has its own confirmation.
    if (readLikeStatus() !== status) return;
    if (known.get(videoId) === status) return;

    remember(known, videoId, status);

    if (status === "LIKE") {
      if (emitted.get(videoId) === "LIKE") return;
      remember(emitted, videoId, "LIKE");
      send({ action: "like", source: `like-status:${reason}`, ...track });
    } else if (status === "INDIFFERENT") {
      if (emitted.get(videoId) === "INDIFFERENT") return;
      remember(emitted, videoId, "INDIFFERENT");
      send({
        action: "unlike",
        source: `like-status:${reason}`,
        videoId,
        title: track.title,
      });
    }
    // DISLIKE is ignored on purpose: a thumbs-down is not an un-like.
  }

  // --------------------------------------------------------- click capture

  /**
   * Which video a like control belongs to. In the player bar it is whatever is
   * playing; elsewhere (a row in a playlist or album) it is the row's own link,
   * found by walking up to the nearest ancestor holding exactly one watch link.
   */
  function videoIdForRenderer(renderer) {
    try {
      if (renderer.closest("ytmusic-player-bar")) return readTrack().videoId;
    } catch (_) {
      /* not an Element */
    }

    let node = renderer.parentElement;
    for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
      const links = node.querySelectorAll('a[href*="watch?v="]');
      if (links.length !== 1) continue;
      const match = VIDEO_ID_RE.exec(links[0].getAttribute("href") || "");
      if (match) return match[1];
    }
    return null;
  }

  function onClickCapture(event) {
    const target = event.target;
    const renderer =
      target && target.closest ? target.closest("ytmusic-like-button-renderer") : null;
    if (!renderer) return;

    state.clicks += 1;

    // Capture phase, so this runs before YouTube Music's own handler and the
    // attribute still holds the state the user was looking at.
    const before = renderer.getAttribute("like-status");
    const videoId = videoIdForRenderer(renderer);

    if (videoId && before && !known.has(videoId)) {
      remember(known, videoId, before);
    }

    window.setTimeout(() => sync("click"), 0);
    window.setTimeout(() => {
      sync("click");
      flushPending("click");
    }, CLICK_SETTLE_MS);
  }

  // ------------------------------------------------------------- toast log

  function startToastObserver() {
    if (!document.documentElement) return;
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes) {
          if (!(node instanceof Element)) continue;
          const text = (node.textContent || "").trim();
          if (!text || text.length > 160 || !TOAST_HINT.test(text)) continue;
          state.toasts.push({
            at: Date.now(),
            tag: node.tagName.toLowerCase(),
            id: node.id || null,
            text,
          });
          if (state.toasts.length > 20) state.toasts.shift();

          // "Saved to liked music" is the server confirming. Re-read the state
          // in case the attribute mutation was missed entirely; nothing is
          // reported unless the like-status actually changed.
          if (/liked music/i.test(text)) {
            state.toastSignals += 1;
            window.setTimeout(() => {
              sync("toast");
              flushPending("toast");
            }, 150);
          }
        }
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  // ------------------------------------------------------- network sniffer

  function onPageMessage(event) {
    // Deliberately no `event.source !== window` check. Across Firefox's Xray
    // boundary the sender is not reliably the same object as the content
    // script's own `window`, and a failed identity comparison fails *silently*
    // — the bridge simply goes dead while everything else looks healthy. The
    // origin check plus the strict shape check below is what actually keeps
    // this honest.
    if (event.origin !== window.location.origin) return;

    const data = event.data;
    if (!data || data.__invokr !== NET_MARKER) return;

    if (data.hello) {
      state.snifferPresent = true;
      return;
    }

    if (data.track) {
      const track = data.track;
      if (!track || typeof track.videoId !== "string" || !VIDEO_ID_ONLY_RE.test(track.videoId)) {
        return;
      }
      state.snifferPresent = true;
      playerTrack = {
        videoId: track.videoId,
        title: String(track.title || ""),
        author: String(track.author || ""),
        at: Date.now(),
      };
      state.playerTrackId = track.videoId;

      // Record the baseline for the new track straight away rather than waiting
      // for the next poll.
      scheduleSync("page-track");
      return;
    }

    if (data.action !== "like" && data.action !== "unlike") return;
    if (typeof data.videoId !== "string" || !VIDEO_ID_ONLY_RE.test(data.videoId)) {
      // The page saw the request but could not read an id out of it. Counted
      // rather than guessed at: attributing it to the playing track would
      // download the wrong song for every playlist-row like.
      state.networkUnreadable += 1;
      return;
    }

    state.networkSignals += 1;

    // Attach the player bar's metadata when the liked track is the one playing;
    // otherwise the server resolves it from YouTube.
    const track = readTrack();
    const sameTrack = track.videoId === data.videoId;
    const status = data.action === "like" ? "LIKE" : "INDIFFERENT";

    // `emitted` only. `known` is left to the DOM state machine: the network
    // signal arrives as the request goes out, which can be before the renderer
    // has been flipped, and recording it here would make the still-unflipped
    // attribute look like a change back to INDIFFERENT.
    remember(emitted, data.videoId, status);

    send({
      action: data.action,
      source: "page-network",
      videoId: data.videoId,
      ytUrl: `https://music.youtube.com/watch?v=${data.videoId}`,
      title: sameTrack ? track.title : "",
      artists: sameTrack ? track.artists : [],
      album: sameTrack ? track.album : "",
      durationSec: sameTrack ? track.durationSec : null,
    });
  }

  // -------------------------------------------------------------- messages

  function diagnostics() {
    const renderer = likeEl && likeEl.isConnected ? likeEl : likeRenderer();
    return {
      url: window.location.href,
      readyState: document.readyState,
      uptimeMs: Date.now() - state.startedAt,
      likeRendererFound: !!renderer,
      likeRenderer: renderer
        ? {
            tag: renderer.tagName.toLowerCase(),
            id: renderer.id || null,
            likeStatus: renderer.getAttribute("like-status"),
            connected: renderer.isConnected,
            inPlayerBar: !!renderer.closest("ytmusic-player-bar"),
          }
        : null,
      playerBarFound: !!document.querySelector("ytmusic-player-bar"),
      currentTrack: readTrack(),
      mediaSession: mediaSessionTrack(),
      syncs: state.syncs,
      likeStatusChanges: state.likeStatusChanges,
      likeStatusLast: state.likeStatusLast,
      lastSync: state.lastSync,
      clicks: state.clicks,
      toastSignals: state.toastSignals,
      networkSignals: state.networkSignals,
      networkUnreadable: state.networkUnreadable,
      snifferPresent: state.snifferPresent,
      playerTrack: playerTrack
        ? {
            videoId: playerTrack.videoId,
            title: playerTrack.title,
            author: playerTrack.author,
            ageMs: Date.now() - playerTrack.at,
          }
        : null,
      trackedVideos: known.size,
      reportedVideos: emitted.size,
      discardedTransitions: state.discarded.slice(-5),
      recentToasts: state.toasts.slice(-6),
      errors: state.errors.slice(-6),
    };
  }

  browser.runtime.onMessage.addListener((message) => {
    if (!message || typeof message.type !== "string") return undefined;

    if (message.type === "invokr/getTrack") {
      return Promise.resolve({ track: readTrack() });
    }

    // Bypasses detection entirely: posts the current track straight away, so we
    // can tell "detection is broken" apart from "the server path is broken".
    if (message.type === "invokr/testLike") {
      const track = readTrack();
      if (!track.videoId) {
        return Promise.resolve({ sent: false, reason: "no videoId on the page", track });
      }
      send({ action: "like", source: "manual", ...track });
      return Promise.resolve({ sent: true, track });
    }

    if (message.type === "invokr/diag") {
      return Promise.resolve(diagnostics());
    }

    return undefined;
  });

  // ------------------------------------------------------------- lifecycle

  let scheduled = null;
  function scheduleSync(reason) {
    if (scheduled) return;
    scheduled = window.setTimeout(() => {
      scheduled = null;
      sync(reason);
    }, 200);
  }

  function boot() {
    try {
      // Chrome (the repair loop), then a couple of signals:
      //   - a slow poll, which is what guarantees every track gets a baseline;
      //   - the document observer, so the player bar appearing or being
      //     replaced is noticed;
      //   - the site's own navigation events.
      window.setInterval(() => sync("poll"), POLL_MS);

      if (document.documentElement) {
        new MutationObserver(() => scheduleSync("dom")).observe(document.documentElement, {
          childList: true,
          subtree: true,
        });
      }

      for (const name of ["yt-navigate-start", "yt-navigate-finish", "yt-page-type-changed"]) {
        document.addEventListener(
          name,
          () => {
            attachLike();
            window.setTimeout(() => sync(name), 500);
          },
          true
        );
      }

      attachLike();
      startToastObserver();
    } catch (error) {
      note(error);
    }
  }

  // Registered before the DOM is ready on purpose: the page-world sniffer
  // announces itself at `document_start`, and the click listener has to be in
  // place from the first interaction.
  window.addEventListener("message", onPageMessage);
  document.addEventListener("click", onClickCapture, true);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
