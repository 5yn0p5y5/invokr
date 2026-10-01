/**
 * invokr — background event page.
 *
 * Detection happens here, not in the page. `webRequest.onBeforeRequest` can
 * read the body of the `/youtubei/v1/like/*` POST that YouTube Music sends when
 * you like something, which gives us the video id directly from the network
 * layer. That works for the player bar, playlist rows and the "…" menu, needs
 * no page-world injection, and cannot fail silently the way a DOM observer can.
 *
 * The content script's `like-status` observer is kept as a second signal. Both
 * paths funnel through `dispatchLike`, which de-duplicates on
 * (action, videoId) so one like never posts twice.
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

// Only needs to cover the gap between the two observers of a single click:
// webRequest fires as the POST goes out, the like-status attribute flips a few
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
  likesDetected: 0,
  unlikesDetected: 0,
  duplicatesSuppressed: 0,
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

function videoIdFromRequestBody(requestBody) {
  try {
    const raw = requestBody && requestBody.raw;
    if (!raw || !raw.length || !raw[0] || !raw[0].bytes) return null;
    const text = new TextDecoder("utf-8").decode(new Uint8Array(raw[0].bytes));
    return findVideoId(JSON.parse(text));
  } catch (_) {
    return null;
  }
}

/**
 * Pull a video id out of an innertube request body.
 *
 * The shape we expect is `{target: {videoId}}`, but that is reverse-engineered
 * rather than documented, so fall back to scanning for any `*videoId` key. A
 * body-shape change then degrades to "still works" rather than "silently stops
 * detecting likes".
 */
function findVideoId(value, depth = 0) {
  if (!value || typeof value !== "object" || depth > 6) return null;
  for (const [key, item] of Object.entries(value)) {
    if (
      typeof item === "string" &&
      /videoId$/i.test(key) &&
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

          const videoId = videoIdFromRequestBody(details.requestBody);

          // Record every innertube POST, not just the ones we act on. If a like
          // is ever missed, the request log shows whether it was sent at all
          // and under which path, instead of leaving us to guess.
          stats.recentRequests.push({
            at: Date.now(),
            path: details.url.replace(/^https?:\/\/[^/]+/, "").split("?")[0],
            like: details.url.includes("/like/"),
            videoId: videoId || null,
          });
          if (stats.recentRequests.length > 25) stats.recentRequests.shift();

          if (!details.url.includes("/like/")) return;

          // /like/dislike is a thumbs-down: not our business.
          if (details.url.includes("/like/dislike")) return;

          if (!videoId) {
            stats.requestsUnparsed += 1;
            recordDispatch({
              action: "?",
              videoId: null,
              source: "webRequest",
              outcome: "no videoId in body",
            });
            return;
          }

          const action = details.url.includes("/like/remove") ? "unlike" : "like";
          if (action === "unlike") stats.unlikesDetected += 1;
          else stats.likesDetected += 1;

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
      if (job.status === "failed" || job.status === "cancelled") {
        await badge("!", "#b3261e");
        await notify(
          job.status === "cancelled" ? "Download cancelled" : "Download failed",
          `${label}${job.error ? ` — ${job.error}` : ""}`
        );
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
