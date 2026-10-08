/**
 * invokr — page-world network sniffer for music.youtube.com.
 *
 * This is the one signal that does not depend on the DOM at all. It wraps the
 * page's own `fetch` and `XMLHttpRequest` and reads the body of every
 * `/youtubei/v1/like/*` request as the page sends it, which is the only way to
 * see a like of a track that is *not* the one playing — a playlist row, an
 * album row, the "…" menu.
 *
 * Why not `webRequest`? The background page still listens there, but Firefox
 * only fills in `details.requestBody` when it can get at the upload stream, and
 * `WebRequestUpload.createRequestBody` returns `{ formData }` with no raw bytes
 * whenever the body parses as form data. In practice that path reported
 * "no videoId in body" for every single like, so it cannot be the primary
 * source. Reading the body inside the page has no such failure mode.
 *
 * The extension APIs are not reachable from here, so findings are posted to the
 * isolated content script through `window.postMessage`, which validates them
 * before forwarding.
 *
 * MAIN world is required (the page's `window` is not visible from an isolated
 * script) and is supported for content scripts from Firefox 128.
 */
(() => {
  "use strict";

  const MARKER = "invokr/network-like";
  const LIKE_PATH_RE = /\/youtubei\/v1\/like\/(like|dislike|remove|removelike)(?:[/?#]|$)/i;
  const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
  const VIDEO_ID_IN_TEXT_RE = /"video[_-]?id"\s*:\s*"([A-Za-z0-9_-]{11})"/i;
  const MAX_BODY_TEXT = 20000;

  // A like is re-sent several times over a second or two; only the first of an
  // identical (action, videoId) pair is worth reporting.
  const REPEAT_WINDOW_MS = 3000;
  const recent = new Map();

  // How often to ask the player what is playing, and when to re-announce
  // ourselves to the content script.
  const TRACK_POLL_MS = 1000;
  const HELLO_DELAYS_MS = [0, 300, 1000, 3000];

  /**
   * `null` for a dislike — a thumbs-down is not a like and never removes one.
   */
  function actionFor(url) {
    const match = LIKE_PATH_RE.exec(String(url || ""));
    if (!match) return null;
    const verb = match[1].toLowerCase();
    if (verb === "dislike") return null;
    return verb === "like" ? "like" : "unlike";
  }

  function videoIdFromValue(value, depth = 0) {
    if (!value || typeof value !== "object" || depth > 6) return null;
    for (const [key, item] of Object.entries(value)) {
      if (typeof item === "string" && /video_?id$/i.test(key) && VIDEO_ID_RE.test(item)) {
        return item;
      }
      const nested = videoIdFromValue(item, depth + 1);
      if (nested) return nested;
    }
    return null;
  }

  /**
   * The documented shape is `{"target": {"videoId": "..."}}`, but that is
   * reverse engineered. Parse it when it is JSON, then fall back to a textual
   * scan so a body-shape change degrades to "still works" rather than
   * "silently stops detecting likes".
   */
  function videoIdFromBody(text) {
    if (!text) return null;
    if (text.length <= MAX_BODY_TEXT) {
      try {
        const found = videoIdFromValue(JSON.parse(text));
        if (found) return found;
      } catch (_) {
        /* not JSON — the textual scan below still applies */
      }
    }
    const match = VIDEO_ID_IN_TEXT_RE.exec(text);
    return match ? match[1] : null;
  }

  /**
   * Bodies arrive as strings, `URLSearchParams`, `FormData`, `Blob` or
   * `ArrayBuffer`. Only the first three can be read synchronously; the rest are
   * reported with no id, and the DOM signals cover them.
   */
  function bodyText(body) {
    if (body === null || body === undefined) return "";
    if (typeof body === "string") return body;
    try {
      if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) {
        return body.toString();
      }
      if (typeof FormData !== "undefined" && body instanceof FormData) {
        const parts = [];
        for (const pair of body.entries()) {
          if (typeof pair[1] === "string") parts.push(`${pair[0]}=${pair[1]}`);
        }
        return parts.join("&");
      }
    } catch (_) {
      /* cross-realm object; nothing readable */
    }
    return "";
  }

  function report(url, body) {
    let action;
    try {
      action = actionFor(url);
    } catch (_) {
      return;
    }
    if (!action) return;

    let videoId = null;
    try {
      videoId = videoIdFromBody(bodyText(body));
    } catch (_) {
      videoId = null;
    }

    // YouTube Music re-sends a like several times. Report each distinct
    // (action, videoId) once; the retries add nothing but noise to the
    // diagnostics and wake the background page for nothing.
    const key = `${action}:${videoId || "?"}`;
    const now = Date.now();
    if (now - (recent.get(key) || 0) < REPEAT_WINDOW_MS) return;
    recent.set(key, now);
    if (recent.size > 40) {
      for (const [k, t] of recent) if (now - t > REPEAT_WINDOW_MS) recent.delete(k);
    }

    try {
      window.postMessage(
        { __invokr: MARKER, action, videoId, at: now },
        window.location.origin
      );
    } catch (_) {
      /* an unwritable postMessage target is not worth breaking the page over */
    }
  }

  /**
   * The video that is actually playing, straight from the page's own player.
   *
   * This is what makes a like attributable when the URL has no `?v=` — which is
   * every browse page — and when the player bar is collapsed and its title is
   * not a link. The id lives on the player element, so reading it needs the page
   * world, which is the other reason this file exists.
   */
  function currentTrack() {
    try {
      const api = document.querySelector("#movie_player");
      if (!api || typeof api.getVideoData !== "function") return null;
      const data = api.getVideoData();
      if (!data) return null;
      const videoId = data.video_id || data.videoId || "";
      if (!VIDEO_ID_RE.test(videoId)) return null;
      return { videoId, title: data.title || "", author: data.author || "" };
    } catch (_) {
      return null;
    }
  }

  function announceTrack() {
    const track = currentTrack();
    if (!track || track.videoId === lastAnnouncedId) return;
    lastAnnouncedId = track.videoId;
    try {
      window.postMessage({ __invokr: MARKER, track, at: Date.now() }, window.location.origin);
    } catch (_) {
      /* nothing to report */
    }
  }

  let lastAnnouncedId = null;

  // ------------------------------------------------------------------ fetch

  try {
    const nativeFetch = window.fetch;
    if (typeof nativeFetch === "function") {
      window.fetch = function (input, init) {
        try {
          const url = typeof input === "string" ? input : input && input.url;
          if (actionFor(url)) {
            let body = init && init.body;
            if (body === undefined && input && typeof input === "object" && "body" in input) {
              body = input.body;
            }
            report(url, body);
          }
        } catch (_) {
          /* never let inspection change the request */
        }
        return nativeFetch.apply(this, arguments);
      };
    }
  } catch (_) {
    /* leave fetch alone */
  }

  // -------------------------------------------------------------------- XHR

  try {
    const nativeOpen = XMLHttpRequest.prototype.open;
    const nativeSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function (method, url) {
      try {
        this.__invokrUrl = url;
      } catch (_) {
        /* some XHR-like objects are not extensible */
      }
      return nativeOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function (body) {
      try {
        if (actionFor(this.__invokrUrl)) report(this.__invokrUrl, body);
      } catch (_) {
        /* never let inspection change the request */
      }
      return nativeSend.apply(this, arguments);
    };
  } catch (_) {
    /* leave XMLHttpRequest alone */
  }

  // The current track can change at any time, and the DOM says nothing about it
  // on a browse page. A one-second poll is cheap — this is one property read —
  // and it also covers the player element being created after us.
  window.setInterval(announceTrack, TRACK_POLL_MS);
  window.setTimeout(announceTrack, 0);

  // The content script may not have registered its listener yet, so say hello a
  // few times: it reports this as "page script present", and a single message
  // at document_start would be a coin toss.
  for (const delay of HELLO_DELAYS_MS) {
    window.setTimeout(() => {
      try {
        window.postMessage({ __invokr: MARKER, hello: true }, window.location.origin);
      } catch (_) {
        /* nothing to report */
      }
    }, delay);
  }
})();
