/**
 * invokr — background event page.
 *
 * Detection is redundant on purpose. Three signals converge here and are
 * de-duplicated on (action, videoId) so one like never posts twice:
 *
 *   1. `webRequest.onBeforeRequest` on the `/youtubei/v1/like/*` POST, which
 *      carries the video id in the request body. Kept because it costs nothing
 *      and works whenever Firefox hands the upload stream over, but it is no
 *      longer the primary source: `WebRequestUpload.createRequestBody` returns
 *      form data with no raw bytes whenever the body parses as form encoded,
 *      and in practice this path reported "no videoId in body" for every like.
 *   2. The `like-status` observer in the content script, which is the source of
 *      truth for LIKE vs UNLIKE on the playing track.
 *   3. `ytm-net.js` in the page world, which reads the like POST as the page
 *      sends it. This is the only signal that sees a like of a track that is
 *      not the one playing.
 */

const DEFAULTS = {
  serverUrl: "http://127.0.0.1:8765",
  token: "",
  notify: true,
  // "popup" badges the toolbar icon and waits for a click; "tab" opens the
  // server-rendered review page straight away.
  reviewOpen: "popup",
  // Try to open the popup panel automatically when a human decision is needed.
  autoOpenPopup: true,
};

// Only needs to cover the gap between the signals for a single click: the page
// world sees the POST as it goes out, the like-status attribute flips a few
// hundred milliseconds later once the server responds. It must stay short, or
// a genuine second like gets swallowed.
const DEDUPE_WINDOW_MS = 4000;

const stats = {
  startedAt: Date.now(),
  // null = not attempted yet, false = the webRequest permission is missing.
  webRequestAvailable: null,
  popupApi: null,
  requestsSeen: 0,
  requestsUnparsed: 0,
  // Counted in `dispatchLike`, so they mean "an event was accepted from any
  // signal". They used to be incremented only in the webRequest branch, which
  // made them read 0 while likes were being detected and processed.
  likesDetected: 0,
  unlikesDetected: 0,
  duplicatesSuppressed: 0,
  bySource: {},
  serverCalls: [],
  recentRequests: [],
  recentDispatches: [],
  errors: [],
};

const recent = new Map(); // "action:videoId" -> timestamp

function note(kind, detail) {
  stats.errors.push({ at: Date.now(), kind, detail: String(detail).slice(0, 300) });
  if (stats.errors.length > 20) stats.errors.shift();
  console.warn("invokr:", kind, detail);
}

function recordServerCall(entry) {
  stats.serverCalls.push({ at: Date.now(), ...entry });
  if (stats.serverCalls.length > 20) stats.serverCalls.shift();
}

function recordDispatch(entry) {
  stats.recentDispatches.push({ at: Date.now(), ...entry });
  if (stats.recentDispatches.length > 25) stats.recentDispatches.shift();
}

function duplicate(key) {
  const now = Date.now();
  const last = recent.get(key);
  recent.set(key, now);
  if (recent.size > 60) {
    for (const [k, t] of recent) if (now - t > 60000) recent.delete(k);
  }
  return typeof last === "number" && now - last < DEDUPE_WINDOW_MS;
}

// ------------------------------------------------------------------ settings

async function settings() {
  return { ...DEFAULTS, ...(await browser.storage.local.get(DEFAULTS)) };
}

async function badge(text, colour) {
  try {
    await browser.action.setBadgeText({ text: text ? String(text) : "" });
    if (colour) await browser.action.setBadgeBackgroundColor({ color: colour });
  } catch (_) {
    /* cosmetic */
  }
}

async function notify(title, message) {
  const { notify: enabled } = await settings();
  if (!enabled) return;
  try {
    await browser.notifications.create({ type: "basic", title, message });
  } catch (_) {
    /* cosmetic */
  }
}

function describe(payload) {
  if (payload.title) {
    return payload.artists && payload.artists.length
      ? `${payload.artists.join(", ")} — ${payload.title}`
      : payload.title;
  }
  return payload.videoId || "track";
}

function api(path, body, { serverUrl, token }) {
  return fetch(`${serverUrl}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
}

// -------------------------------------------------------------- detection

/**
 * Turn `details.requestBody` into text.
 *
 * Firefox does not hand back one uniform shape. Per
 * `WebRequestUpload.createRequestBody`, a body that parses as form data comes
 * back as `{formData}` with **no** `raw` bytes, while anything else — including
 * the JSON that YouTube Music sends — comes back as `{raw}` plus a leniently
 * parsed `lenientFormData`. The old code read `raw` only, so every like request
 * was reported as "no videoId in body" even though the body was right there.
 *
 * `kind` is carried into the diagnostics so a future body-shape change is
 * visible instead of guessable.
 */
function decodeRequestBody(requestBody) {
  if (!requestBody) return { kind: "absent", text: "" };
  if (requestBody.error) return { kind: "error", text: "" };

  const chunks = requestBody.raw;
  if (chunks && chunks.length) {
    let text = "";
    for (const chunk of chunks) {
      if (!chunk) continue;
      if (chunk.file) return { kind: "file", text: "" };
      if (chunk.bytes) {
        text += new TextDecoder("utf-8").decode(new Uint8Array(chunk.bytes));
      }
    }
    if (text) return { kind: "raw", text };
  }

  const form = requestBody.formData || requestBody.lenientFormData;
  if (form) {
    const pairs = [];
    for (const [key, value] of Object.entries(form)) {
      for (const item of [].concat(value)) {
        pairs.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(item))}`);
      }
    }
    if (pairs.length) return { kind: "formData", text: pairs.join("&") };
  }

  return { kind: "empty", text: "" };
}

function videoIdFromBodyText(text) {
  if (!text) return null;
  try {
    const found = findVideoId(JSON.parse(text));
    if (found) return found;
  } catch (_) {
    /* not JSON — the textual scan below still applies */
  }
  const match = /"video[_-]?id"\s*:\s*"([A-Za-z0-9_-]{11})"/i.exec(text);
  return match ? match[1] : null;
}

/**
 * Pull a video id out of an innertube request body.
 *
 * The shape we expect is `{target: {videoId}}`, but that is reverse-engineered
 * rather than documented, so fall back to scanning for any `*video_id` key. A
 * body-shape change then degrades to "still works" rather than "silently stops
 * detecting likes".
 */
function findVideoId(value, depth = 0) {
  if (!value || typeof value !== "object" || depth > 6) return null;
  for (const [key, item] of Object.entries(value)) {
    if (
      typeof item === "string" &&
      /video_?id$/i.test(key) &&
      /^[A-Za-z0-9_-]{11}$/.test(item)
    ) {
      return item;
    }
    const nested = findVideoId(item, depth + 1);
    if (nested) return nested;
  }
  return null;
}

/**
 * Ask the content script for the track it is showing. Returns null when the
 * liked video is not the one playing — a playlist-row like, for instance — in
 * which case the server resolves the metadata from YouTube itself.
 */
async function enrich(tabId, videoId) {
  if (typeof tabId !== "number" || tabId < 0) return null;
  try {
    const response = await browser.tabs.sendMessage(tabId, { type: "invokr/getTrack" });
    const track = response && response.track;
    if (track && track.videoId === videoId) return track;
  } catch (_) {
    /* content script not ready; the server will resolve it */
  }
  return null;
}

/**
 * Registered from the end of this file, and inside a try/catch on purpose.
 *
 * `browser.webRequest` is undefined unless the manifest grants the permission.
 * A bare reference at the top level would throw during script evaluation and
 * kill the whole background page — taking the message listener and the
 * like-status fallback down with it, which looks exactly like "nothing fires".
 */
function installWebRequestHook() {
  try {
    if (!browser.webRequest || !browser.webRequest.onBeforeRequest) {
      stats.webRequestAvailable = false;
      note(
        "webRequest",
        "browser.webRequest is unavailable — the 'webRequest' permission is not " +
          "granted. Reload the extension in about:debugging so the new manifest " +
          "takes effect. Falling back to the like-status observer."
      );
      return;
    }

    browser.webRequest.onBeforeRequest.addListener(
      (details) => {
        try {
          if (details.method !== "POST") return;
          stats.requestsSeen += 1;

          const isLike = details.url.includes("/like/");

          // Record every innertube POST, but only decode the body of a like
          // request. If a like is ever missed, the log shows whether the
          // request was sent at all, what the body looked like, and what we
          // managed to read out of it.
          const entry = {
            at: Date.now(),
            path: details.url.replace(/^https?:\/\/[^/]+/, "").split("?")[0],
            like: isLike,
            videoId: null,
          };

          if (!isLike) {
            stats.recentRequests.push(entry);
            if (stats.recentRequests.length > 25) stats.recentRequests.shift();
            return;
          }

          const body = decodeRequestBody(details.requestBody);
          const videoId = videoIdFromBodyText(body.text);
          entry.videoId = videoId || null;
          entry.body = body.kind;
          entry.preview = body.text.slice(0, 160);
          stats.recentRequests.push(entry);
          if (stats.recentRequests.length > 25) stats.recentRequests.shift();

          // /like/dislike is a thumbs-down: not our business.
          if (details.url.includes("/like/dislike")) return;

          if (!videoId) {
            stats.requestsUnparsed += 1;
            recordDispatch({
              action: "?",
              videoId: null,
              source: "webRequest",
              outcome: `no videoId in body (${body.kind})`,
            });
            return;
          }

          const action = details.url.includes("/like/remove") ? "unlike" : "like";
          dispatchLike({ action, videoId, tabId: details.tabId, source: "webRequest" });
        } catch (error) {
          note("webRequest", error);
        }
      },
      { urls: ["*://music.youtube.com/youtubei/v1/*"] },
      ["requestBody"]
    );

    stats.webRequestAvailable = true;
  } catch (error) {
    stats.webRequestAvailable = false;
    note("webRequest", error);
  }
}

// ------------------------------------------------------------------ actions

function dispatchLike({ action, videoId, tabId, source, title, artists, album, durationSec }) {
  if (!videoId) return;

  const key = `${action}:${videoId}`;
  if (duplicate(key)) {
    stats.duplicatesSuppressed += 1;
    recordDispatch({ action, videoId, source, outcome: "duplicate-suppressed" });
    return;
  }

  // An un-like resets the state, so re-liking straight afterwards must not be
  // swallowed by the dedupe window of the original like.
  if (action === "unlike") recent.delete(`like:${videoId}`);

  if (action === "unlike") stats.unlikesDetected += 1;
  else stats.likesDetected += 1;
  stats.bySource[source] = (stats.bySource[source] || 0) + 1;

  recordDispatch({ action, videoId, source, outcome: "dispatched" });

  const finish = (track) => {
    const payload = {
      action,
      source,
      videoId,
      ytUrl: `https://music.youtube.com/watch?v=${videoId}`,
      title: (track && track.title) || title || "",
      artists: (track && track.artists) || artists || [],
      album: (track && track.album) || album || "",
      durationSec: (track && track.durationSec) || durationSec || null,
    };
    return action === "unlike" ? handleUnlike(payload) : handleLike(payload);
  };

  if (action === "like" && !title) {
    enrich(tabId, videoId)
      .then(finish)
      .catch((error) => note("enrich", error));
  } else {
    finish(null);
  }
}

async function handleUnlike(payload) {
  const config = await settings();
  if (!config.token) return;
  if (!(await InvokrAccess.has(config.serverUrl))) {
    note("unlike", InvokrAccess.denied(config.serverUrl));
    return;
  }
  try {
    const response = await api(
      "/api/unlike",
      { videoId: payload.videoId, title: payload.title || "" },
      config
    );
    recordServerCall({ path: "/api/unlike", status: response.status, videoId: payload.videoId });
  } catch (error) {
    note("unlike", error);
  }
}

async function handleLike(payload) {
  const config = await settings();

  if (!config.token) {
    await badge("!", "#b3261e");
    await notify("invokr is not configured", "Open the extension's options and paste the server token.");
    recordServerCall({ path: "/api/review", status: null, detail: "no token configured" });
    return;
  }

  // Likes arrive from a page event, not a click on the extension, so the
  // prompt cannot happen here. Report the missing grant instead of a timeout.
  if (!(await InvokrAccess.has(config.serverUrl))) {
    await badge("!", "#b3261e");
    await notify("invokr: server access not granted", InvokrAccess.denied(config.serverUrl));
    recordServerCall({ path: "/api/review", status: null, detail: "host access not granted" });
    return;
  }

  let response;
  try {
    response = await api("/api/review", payload, config);
  } catch (error) {
    await badge("!", "#b3261e");
    await notify("invokr: cannot reach the server", `${config.serverUrl} — ${error.message}`);
    recordServerCall({ path: "/api/review", status: null, detail: String(error.message) });
    return;
  }

  recordServerCall({
    path: "/api/review",
    status: response.status,
    videoId: payload.videoId,
    title: payload.title,
  });

  if (response.status === 401) {
    await badge("!", "#b3261e");
    await notify("invokr: token rejected", "The token in the extension options does not match the server.");
    return;
  }

  if (!response.ok) {
    await badge("!", "#b3261e");
    const detail = await response.text().catch(() => "");
    await notify("invokr: server error", `HTTP ${response.status} ${detail.slice(0, 180)}`);
    note("handleLike", `HTTP ${response.status} ${detail.slice(0, 180)}`);
    return;
  }

  const data = await response.json();

  if (data.skipped) {
    recordDispatch({
      action: "like",
      videoId: payload.videoId,
      source: payload.source,
      outcome: "already archived",
    });
    await badge("✓", "#2f6b3a");
    await notify("Already in your archive", (data.already && data.already.title) || describe(payload));
    return;
  }

  // Obviously-correct match: the server already queued it, so there is nothing
  // to ask about. Watch it through to completion instead.
  if (data.auto) {
    recordDispatch({
      action: "like",
      videoId: payload.videoId,
      source: payload.source,
      outcome: `auto-accepted (${data.decision ? data.decision.reason : ""})`,
    });
    await notify(
      `Saving — ${data.artist} — ${data.title}`,
      `Matched ${data.album ? `“${data.album}” ` : ""}on ${data.source}.`
    );
    watchJob(data.jobId, `${data.artist} — ${data.title}`);
    return;
  }

  const count = data.candidateCount || 0;
  const reason = data.decision && data.decision.reason ? data.decision.reason : "";

  if (config.reviewOpen === "tab") {
    await browser.tabs.create({ url: data.reviewUrl, active: false });
    await notify(
      "Confirm the metadata",
      `${reason || `${count} match(es)`} for ${describe(payload)} — a tab has been opened.`
    );
    await badge(count || "?", "#7c4dff");
    recordDispatch({
      action: "like",
      videoId: payload.videoId,
      source: payload.source,
      outcome: "review tab opened",
    });
    return;
  }

  await badge(count || "?", "#7c4dff");

  // Firefox 149 removed the user-gesture restriction on action.openPopup
  // (bug 1799344), though MDN still documents it. Try, and fall back to the
  // badge if the build is older or the call is refused.
  if (config.autoOpenPopup && (await tryOpenPopup())) {
    recordDispatch({
      action: "like",
      videoId: payload.videoId,
      source: payload.source,
      outcome: "popup opened",
    });
    return;
  }

  recordDispatch({
    action: "like",
    videoId: payload.videoId,
    source: payload.source,
    outcome: "badge only",
  });
  await notify(
    "Review needed",
    `${reason || `${count} match(es)`} for ${describe(payload)} — click the invokr icon.`
  );
}

// --------------------------------------------------------------- watching

const JOB_POLL_MS = 4000;
const JOB_POLL_ATTEMPTS = 60; // ~4 minutes

/**
 * Follow a queued download and reflect its outcome on the icon.
 *
 * Note: a Firefox event page can be suspended between timers, in which case the
 * badge simply stops updating. The notification is the durable signal.
 */
function watchJob(jobId, label) {
  let attempts = 0;
  let missed = 0;

  badge("↓", "#1f6feb");

  const tick = async () => {
    attempts += 1;
    if (!jobId || attempts > JOB_POLL_ATTEMPTS) {
      await badge("", "");
      return;
    }

    try {
      const config = await settings();
      if (!(await InvokrAccess.has(config.serverUrl))) {
        await badge("!", "#b3261e");
        await notify("invokr: server access not granted", InvokrAccess.denied(config.serverUrl));
        note("watchJob", "host access not granted");
        return;
      }
      const response = await fetch(`${config.serverUrl}/api/jobs/${jobId}`, {
        headers: { Authorization: `Bearer ${config.token}` },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const job = await response.json();
      missed = 0;

      if (job.status === "done") {
        await badge("✓", "#2f6b3a");
        await notify("Saved", `${label} → ${job.path || ""}`);
        setTimeout(() => badge("", ""), 10000);
        return;
      }
      // A failure the user has already fixed is not news. Liking the song again
      // runs the whole like path and creates a new job, which leaves this one
      // sitting at "failed" — so ask the server whether it still matters
      // instead of trusting the status alone.
      if (job.status === "failed" && job.outstanding === false) {
        await badge("✓", "#2f6b3a");
        setTimeout(() => badge("", ""), 10000);
        return;
      }

      if (job.status === "failed" || job.status === "cancelled") {
        const cancelled = job.status === "cancelled";
        await badge("!", "#b3261e");
        await notify(
          cancelled ? "Download cancelled" : "Download failed — retry?",
          cancelled
            ? label
            : `${label}${job.error ? ` — ${job.error}` : ""} — open the extension to retry.`
        );

        // The worker retries once by itself, so a job that reports "failed"
        // has already had a second chance. Put it in front of the user, where
        // the popup shows the reason and a Retry button.
        //
        // Deliberately not gated on `autoOpenPopup`: that setting is about
        // interrupting for a *review*, and this is a failure the user asked to
        // be told about. If the panel cannot be opened (an older Firefox, or
        // another panel already up) the notification above is the durable
        // signal, and the failure is still there on the next click.
        if (!cancelled) await tryOpenPopup();

        setTimeout(() => badge("", ""), 10000);
        return;
      }
    } catch (error) {
      missed += 1;
      if (missed > 5) return; // server went away; stop shouting about it
    }

    setTimeout(tick, JOB_POLL_MS);
  };

  setTimeout(tick, 1500);
}

/**
 * Try to open the toolbar popup.
 *
 * Firefox removed the user-gesture requirement for action.openPopup in 149
 * (bug 1799344); older builds reject the call, so this always has a fallback.
 */
async function tryOpenPopup() {
  try {
    if (!browser.action || typeof browser.action.openPopup !== "function") {
      stats.popupApi = "unavailable in this build";
      return false;
    }
    await browser.action.openPopup();
    stats.popupApi = "opened";
    return true;
  } catch (error) {
    stats.popupApi = `refused: ${error.message}`;
    return false;
  }
}

// ---------------------------------------------------------------- messages

browser.runtime.onMessage.addListener((message) => {
  if (!message || typeof message.type !== "string") return undefined;

  if (message.type === "invokr/like") {
    const payload = message.payload || {};
    dispatchLike({
      action: payload.action === "unlike" ? "unlike" : "like",
      videoId: payload.videoId,
      tabId: -1,
      source: payload.source || "content-script",
      title: payload.title,
      artists: payload.artists,
      album: payload.album,
      durationSec: payload.durationSec,
    });
    return undefined;
  }

  if (message.type === "invokr/watchJob") {
    watchJob(message.jobId, message.label || "");
    return Promise.resolve({ watching: true });
  }

  if (message.type === "invokr/stats") {
    return Promise.resolve({
      ...stats,
      pendingReviewTabs: stats.serverCalls.length,
    });
  }

  return undefined;
});

// No action.onClicked listener: the manifest sets `default_popup`, so a click
// opens the popup instead. The popup has the Settings button.

// Last, so that nothing here can prevent the listeners above from registering.
installWebRequestHook();
