/** invokr — options page, including the diagnostics used to debug detection. */

const DEFAULTS = {
  serverUrl: "http://127.0.0.1:8765",
  token: "",
  notify: true,
  reviewOpen: "popup",
  autoOpenPopup: true,
};

const $ = (id) => document.getElementById(id);
const line = (label, value) => `${label.padEnd(24)} ${value}`;

// ------------------------------------------------------------------ settings

async function load() {
  const stored = { ...DEFAULTS, ...(await browser.storage.local.get(DEFAULTS)) };
  $("serverUrl").value = stored.serverUrl;
  $("token").value = stored.token;
  $("notify").checked = stored.notify;
  $("reviewOpen").value = stored.reviewOpen;
  $("autoOpenPopup").checked = stored.autoOpenPopup;
}

async function save() {
  const values = {
    serverUrl: $("serverUrl").value.trim().replace(/\/+$/, "") || DEFAULTS.serverUrl,
    token: $("token").value.trim(),
    notify: $("notify").checked,
    reviewOpen: $("reviewOpen").value,
    autoOpenPopup: $("autoOpenPopup").checked,
  };
  await browser.storage.local.set(values);
  $("status").textContent = "Saved.";
  return values;
}

// -------------------------------------------------------------------- health

function renderHealth(report) {
  const lines = report.checks.map((check) => {
    const mark = check.ok ? "OK  " : check.severity === "required" ? "FAIL" : "warn";
    return `[${mark}] ${check.name.padEnd(16)} ${check.detail}`;
  });
  const head = report.ok
    ? "Server reachable and ready."
    : "Server reachable, but a required check failed.";
  const warnings = report.warnings.length ? `\n\nWarnings:\n- ${report.warnings.join("\n- ")}` : "";

  $("status").textContent = `${head}\n\n${lines.join("\n")}${warnings}`;
  $("status").className = report.ok ? "ok" : "bad";
}

async function testConnection() {
  const values = await save();
  $("status").className = "";
  $("status").textContent = "Contacting the server…";

  let response;
  try {
    response = await fetch(`${values.serverUrl}/api/health`, {
      headers: { Authorization: `Bearer ${values.token}` },
    });
  } catch (error) {
    $("status").className = "bad";
    $("status").textContent =
      `Could not reach ${values.serverUrl}\n\n${error.message}\n\n` +
      `Is the server running?  cd server; python run.py`;
    return;
  }

  if (response.status === 401) {
    $("status").className = "bad";
    $("status").textContent =
      "The server rejected the token. Copy the value from server/config.json again.";
    return;
  }

  renderHealth(await response.json());
}

// --------------------------------------------------------------- diagnostics

function renderDiag(text) {
  $("diagout").textContent = text;
}

async function youtubeMusicTabs() {
  try {
    return await browser.tabs.query({ url: "*://music.youtube.com/*" });
  } catch (error) {
    return [];
  }
}

async function runDiagnostics() {
  const out = [];
  const manifest = browser.runtime.getManifest();

  out.push("=== extension ===");
  out.push(line("version", manifest.version));
  out.push(line("permissions", (manifest.permissions || []).join(", ")));
  out.push(line("host permissions", (manifest.host_permissions || []).join(", ")));

  out.push("");
  out.push("=== background page ===");
  let stats = null;
  try {
    stats = await browser.runtime.sendMessage({ type: "invokr/stats" });
  } catch (error) {
    out.push(line("error", error.message));
  }

  if (stats) {
    out.push(line("uptime (s)", Math.round((Date.now() - stats.startedAt) / 1000)));
    out.push(
      line(
        "webRequest hook",
        stats.webRequestAvailable === true
          ? "active"
          : stats.webRequestAvailable === false
            ? "UNAVAILABLE — permission not granted (reload the extension)"
            : "not attempted"
      )
    );
    out.push(line("popup API", stats.popupApi || "not attempted"));
    out.push(line("like requests seen", stats.requestsSeen));
    out.push(line("  ...unparseable", stats.requestsUnparsed));
    out.push(line("likes detected", stats.likesDetected));
    out.push(line("unlikes detected", stats.unlikesDetected));
    out.push(line("duplicates suppressed", stats.duplicatesSuppressed));

    out.push("");
    out.push("  recent server calls:");
    if (!stats.serverCalls.length) {
      out.push("    (none — the server has never been called)");
    }
    for (const call of stats.serverCalls.slice(-8)) {
      const when = new Date(call.at).toLocaleTimeString();
      out.push(
        `    ${when}  ${call.path}  status=${call.status}  ${call.title || call.videoId || ""}`
      );
    }

    if (stats.errors.length) {
      out.push("");
      out.push("  errors:");
      for (const error of stats.errors.slice(-8)) {
        out.push(`    ${error.kind}: ${error.detail}`);
      }
    }

    // The two logs below are what to read when a like seems to vanish: the
    // dispatches show what the extension decided, the requests show what
    // YouTube Music actually sent.
    out.push("");
    out.push("  recent dispatch decisions:");
    const dispatches = (stats.recentDispatches || []).slice(-10);
    if (!dispatches.length) out.push("    (none yet)");
    for (const item of dispatches) {
      out.push(
        `    ${new Date(item.at).toLocaleTimeString()}  ${item.action} ` +
          `${item.videoId || "?"}  via ${item.source}  ->  ${item.outcome}`
      );
    }

    out.push("");
    out.push("  recent innertube POSTs:");
    const requests = (stats.recentRequests || []).slice(-10);
    if (!requests.length) out.push("    (none yet)");
    for (const item of requests) {
      out.push(
        `    ${new Date(item.at).toLocaleTimeString()}  ${item.path}` +
          `${item.like ? "  [like]" : ""}  videoId=${item.videoId || "-"}`
      );
    }
  }

  out.push("");
  out.push("=== content script (music.youtube.com) ===");
  const tabs = await youtubeMusicTabs();
  if (!tabs.length) {
    out.push("  No YouTube Music tab is open. Open one, then run this again.");
  }

  for (const tab of tabs) {
    out.push("");
    out.push(`  tab ${tab.id}  ${tab.active ? "(active)" : ""}  ${tab.url}`);
    let diag;
    try {
      diag = await browser.tabs.sendMessage(tab.id, { type: "invokr/diag" });
    } catch (error) {
      out.push(`    FAILED to reach the content script: ${error.message}`);
      out.push("    → reload the YouTube Music tab (the extension was probably loaded after it)");
      continue;
    }

    out.push(`    content script uptime  ${Math.round(diag.uptimeMs / 1000)} s`);
    out.push(`    like renderer found    ${diag.likeRendererFound}`);
    if (diag.likeRenderer) {
      out.push(
        `      tag=${diag.likeRenderer.tag} id=${diag.likeRenderer.id} ` +
          `like-status=${diag.likeRenderer.likeStatus} connected=${diag.likeRenderer.connected}`
      );
    }
    out.push(`    player bar found       ${diag.playerBarFound}`);
    out.push(`    like-status changes    ${diag.likeStatusChanges}`);
    out.push(`    current track          ${JSON.stringify(diag.currentTrack)}`);
    out.push(`    media session          ${JSON.stringify(diag.mediaSession)}`);

    out.push("    recent toasts:");
    if (!diag.recentToasts.length) {
      out.push("      (none captured yet — like something to populate this)");
    }
    for (const toast of diag.recentToasts) {
      out.push(`      <${toast.tag}${toast.id ? "#" + toast.id : ""}> "${toast.text}"`);
    }

    if (diag.errors.length) {
      out.push("    content-script errors:");
      for (const error of diag.errors) out.push(`      ${error.message}`);
    }
  }

  out.push("");
  out.push(
    "Reading it: if 'like requests seen' stays 0 when you like a song, the webRequest " +
      "hook is not firing. If it rises but 'likes detected' does not, the request body " +
      "shape has changed. If 'recent server calls' stays empty, the server was never called."
  );

  renderDiag(out.join("\n"));
}

async function sendTestLike() {
  const tabs = await youtubeMusicTabs();
  if (!tabs.length) {
    renderDiag("No YouTube Music tab is open. Open one and start playing a song first.");
    return;
  }

  const tab = tabs.find((candidate) => candidate.active) || tabs[0];
  let response;
  try {
    response = await browser.tabs.sendMessage(tab.id, { type: "invokr/testLike" });
  } catch (error) {
    renderDiag(
      `Could not reach the content script in tab ${tab.id}.\n\n${error.message}\n\n` +
        `Reload the YouTube Music tab — the content script is only injected on page load.`
    );
    return;
  }

  renderDiag(
    `Test like dispatched from tab ${tab.id}.\n\n` +
      `track = ${JSON.stringify(response && response.track, null, 2)}\n\n` +
      `sent  = ${response && response.sent}\n` +
      (response && response.reason ? `reason = ${response.reason}\n` : "") +
      `\nIf the server path works you should get a review tab (or an "already archived" ` +
      `notification). Run diagnostics to see the server call.`
  );
}

$("save").addEventListener("click", save);
$("test").addEventListener("click", testConnection);
$("diag").addEventListener("click", runDiagnostics);
$("testlike").addEventListener("click", sendTestLike);
load();
