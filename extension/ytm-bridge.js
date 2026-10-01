/**
 * invokr — content script on music.youtube.com.
 *
 * Runs in the ordinary, isolated content-script world. That is deliberate: the
 * previous version depended on `world: "MAIN"` to reach YouTube Music's player
 * API, and when that did not take effect the detector silently saw
 * `videoId: null` and dropped every event. Nothing here needs page-world access.
 *
 * Detection is layered, most reliable first:
 *
 *   1. `webRequest` in the background page sees the actual
 *      `/youtubei/v1/like/*` POST and reads the video id out of the request
 *      body. No DOM, no page injection, works for the player bar, playlist
 *      rows and the "…" menu alike.
 *   2. The `like-status` attribute on `#like-button-renderer`, observed here.
 *      It is a plain DOM attribute, so isolation is irrelevant. Covers the
 *      player bar if webRequest ever misses.
 *   3. The YouTube Music toast ("Added to liked music"). Reported for
 *      diagnostics rather than acted on, since 1 and 2 already fire.
 *
 * The background page de-duplicates 1 and 2, so a single like never posts twice.
 */
(() => {
  "use strict";

  const VIDEO_ID_RE = /[?&]v=([A-Za-z0-9_-]{11})/;
  const TOAST_HINT = /(liked|like|library|added to|removed from|saved to)/i;

  const state = {
    startedAt: Date.now(),
    likeStatusChanges: 0,
    likeStatusLast: null,
    toasts: [],
    errors: [],
  };

  const txt = (node) => (node && node.textContent ? node.textContent.trim() : "");

  function note(error) {
    state.errors.push({ at: Date.now(), message: String(error && error.message ? error.message : error) });
    if (state.errors.length > 10) state.errors.shift();
  }

  // ------------------------------------------------------------- metadata

  function videoIdFromDom() {
    const bar = document.querySelector("ytmusic-player-bar");
    const link = bar && bar.querySelector('a[href*="watch?v="]');
    const fromLink = link && VIDEO_ID_RE.exec(link.getAttribute("href") || "");
    if (fromLink) return fromLink[1];

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
  let lastScan = 0;
  const baseline = new Map(); // videoId -> last observed like-status
  const emitted = new Map(); // videoId -> last emitted status

  function send(payload) {
    try {
      return browser.runtime.sendMessage({ type: "invokr/like", payload });
    } catch (error) {
      note(error);
      return null;
    }
  }

  function reportStatus(reason) {
    if (!likeEl) return;
    state.likeStatusChanges += 1;
    const status = likeEl.getAttribute("like-status");
    state.likeStatusLast = { status, at: Date.now() };

    const track = readTrack();
    const videoId = track.videoId;
    if (!videoId || !status) return;

    // The first value seen for a track is whatever YouTube Music rendered —
    // often LIKE for a song liked months ago. Never report that as an event.
    if (reason === "baseline" || !baseline.has(videoId)) {
      baseline.set(videoId, status);
      return;
    }

    const previous = baseline.get(videoId);
    if (previous === status) return; // Polymer rewrite of the same value
    baseline.set(videoId, status);

    if (status === "LIKE") {
      if (emitted.get(videoId) === "LIKE") return;
      emitted.set(videoId, "LIKE");
      send({ action: "like", source: "like-status", ...track });
    } else if (status === "INDIFFERENT") {
      emitted.set(videoId, "INDIFFERENT");
      send({ action: "unlike", source: "like-status", videoId, title: track.title });
    }
    // DISLIKE is ignored on purpose.
  }

  function attachLikeObserver() {
    const el =
      document.getElementById("like-button-renderer") ||
      document.querySelector("ytmusic-like-button-renderer");
    if (!el) return;
    if (el === likeEl && likeObserver) return;

    if (likeObserver) likeObserver.disconnect();
    likeEl = el;
    likeObserver = new MutationObserver(() => reportStatus("change"));
    likeObserver.observe(el, { attributes: true, attributeFilter: ["like-status"] });
    reportStatus("baseline");
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
        }
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  // -------------------------------------------------------------- messages

  function diagnostics() {
    const renderer = likeEl || document.querySelector("ytmusic-like-button-renderer");
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
          }
        : null,
      playerBarFound: !!document.querySelector("ytmusic-player-bar"),
      currentTrack: readTrack(),
      mediaSession: mediaSessionTrack(),
      likeStatusChanges: state.likeStatusChanges,
      likeStatusLast: state.likeStatusLast,
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

  function scan() {
    const now = Date.now();
    if (now - lastScan < 500) return; // fires on every DOM mutation
    lastScan = now;
    if (!likeEl || !likeEl.isConnected) attachLikeObserver();
  }

  function boot() {
    try {
      attachLikeObserver();
      startToastObserver();
      if (document.documentElement) {
        new MutationObserver(scan).observe(document.documentElement, {
          childList: true,
          subtree: true,
        });
      }
      window.setInterval(scan, 3000);
    } catch (error) {
      note(error);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
