/**
 * invokr — toolbar popup.
 *
 * The popup only appears when you click the icon, so it cannot be handed a
 * review. It asks the server what is waiting (`GET /api/reviews`) and renders
 * the newest one. Sessions live on the server with a TTL, so closing the popup
 * by accident loses nothing — reopen it and the review is still there.
 *
 * Every action is one-shot: buttons disable themselves on click, and a
 * completed action replaces the panel with a result state that has no repeat
 * button for the same session.
 */

(() => {
  "use strict";

  const DEFAULTS = {
    serverUrl: "http://127.0.0.1:8765",
    token: "",
    reviewOpen: "popup",
  };

  const $ = (id) => document.getElementById(id);
  let config = { ...DEFAULTS };
  let pendingCount = 0;

  // ------------------------------------------------------------------ setup

  async function loadSettings() {
    config = { ...DEFAULTS, ...(await browser.storage.local.get(DEFAULTS)) };
  }

  async function api(path, options = {}) {
    const response = await fetch(`${config.serverUrl}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${config.token}`,
        ...(options.headers || {}),
      },
    });

    if (response.status === 401) {
      throw new Error("the server rejected the token — check Settings");
    }
    if (!response.ok) {
      let detail = "";
      try {
        detail = (await response.json()).detail || "";
      } catch (_) {
        /* not JSON */
      }
      throw new Error(`HTTP ${response.status}${detail ? `: ${detail}` : ""}`);
    }
    return response.json();
  }

  async function clearBadge() {
    try {
      await browser.action.setBadgeText({ text: "" });
    } catch (_) {
      /* cosmetic */
    }
  }

  function setSub(text) {
    $("sub").textContent = text || "";
  }

  // ------------------------------------------------------------- dom helpers

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  function fmtDuration(seconds) {
    if (!seconds) return "?";
    const total = Math.round(seconds);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  function show(nodes) {
    const content = $("content");
    content.textContent = "";
    for (const node of [].concat(nodes)) content.appendChild(node);
  }

  function message(text, kind) {
    show(el("div", { class: kind === "error" ? "note err" : "note", text }));
  }

  /**
   * A button that fires at most once.
   *
   * The previous version re-rendered an identical "Download selected" button
   * after a successful confirm, so a second click hit an already-consumed
   * session and produced a 404.
   */
  function actionButton(label, className, handler) {
    const button = el("button", { class: className, text: label });
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      button.disabled = true;
      button.textContent = "Working…";
      try {
        await handler();
      } catch (error) {
        button.disabled = false;
        button.textContent = label;
        message(`Failed: ${error.message}`, "error");
      }
    });
    return button;
  }

  function watchJob(jobId, label) {
    try {
      browser.runtime.sendMessage({ type: "invokr/watchJob", jobId, label });
    } catch (_) {
      /* the badge just will not update; the download still runs */
    }
  }

  // ------------------------------------------------------------- result view

  function renderDone(text, remaining) {
    pendingCount = Math.max(0, remaining);

    const buttons = [];
    if (pendingCount > 0) {
      buttons.push(actionButton(`Next review (${pendingCount})`, "primary", init));
    } else {
      buttons.push(el("button", { class: "primary", text: "Close", onclick: () => window.close() }));
    }
    buttons.push(el("button", { text: "Refresh", onclick: init }));

    if (pendingCount > 0) {
      buttons.push(
        actionButton("Discard all", "", async () => {
          await api("/api/reviews", { method: "DELETE" });
          await clearBadge();
          await renderIdle();
        })
      );
    }

    setSub(pendingCount > 0 ? `${pendingCount} still pending` : "all caught up");
    show([el("div", { class: "note", text }), el("div", { class: "row" }, buttons)]);
  }

  // -------------------------------------------------------------- idle state

  async function renderIdle() {
    await clearBadge();
    setSub("nothing waiting");
    pendingCount = 0;

    const blocks = [
      el("div", {
        class: "sub",
        text: "No review is pending. Like a song on YouTube Music and it appears here.",
      }),
    ];

    try {
      const [queue, jobs] = await Promise.all([
        api("/api/queue"),
        api("/api/jobs?limit=8"),
      ]);

      const depth = Object.entries(queue.depth || {})
        .map(([status, count]) => `${status}: ${count}`)
        .join("  ·  ");
      blocks.push(el("div", { class: "sub", text: `Queue — ${depth || "empty"}` }));

      const active = (jobs.jobs || []).filter((job) =>
        ["queued", "downloading"].includes(job.status)
      );

      for (const job of active) {
        const row = el("div", { class: "card" }, [
          el("div", { class: "body" }, [
            el("div", { class: "t", text: job.title || job.video_id || job.job_id }),
            el("div", { class: "m", text: job.spotify_url || job.yt_url }),
            el("div", { class: "tags" }, [
              el("span", {
                class: `badge ${job.status === "downloading" ? "ok" : ""}`,
                text: job.status,
              }),
            ]),
          ]),
        ]);

        if (job.status === "queued") {
          row.appendChild(
            actionButton("Cancel", "", async () => {
              const result = await api(`/api/jobs/${job.job_id}/cancel`, { method: "POST" });
              if (!result.cancelled) throw new Error(result.detail || "could not cancel");
              await renderIdle();
            })
          );
        }

        blocks.push(row);
      }

      if (!active.length) {
        blocks.push(el("div", { class: "sub", text: "No downloads in flight." }));
      }
    } catch (error) {
      blocks.push(el("div", { class: "note err", text: `Server: ${error.message}` }));
    }

    show(blocks);
  }

  // ------------------------------------------------------------ review state

  function candidateCard(candidate, index, sourceDuration) {
    const badge = (text, cls) => el("span", { class: `badge ${cls || ""}`, text });

    let durationBadge = null;
    if (sourceDuration && typeof candidate.duration_delta_s === "number") {
      const delta = candidate.duration_delta_s;
      const shown = `${delta > 0 ? "+" : ""}${delta.toFixed(1)}s`;
      if (!candidate.duration_ok) durationBadge = badge(`duration ${shown}`, "warn");
      else if (Math.abs(delta) > 2) durationBadge = badge(`duration ${shown}`, "ok");
    }

    const input = el("input", { type: "radio", name: "choice", value: String(index) });
    if (index === 0) input.checked = true;

    const card = el("label", { class: `card${index === 0 ? " sel" : ""}` }, [
      input,
      candidate.cover_url
        ? el("img", { src: candidate.cover_url, alt: "", loading: "lazy" })
        : el("div", { class: "ph" }),
      el("div", { class: "body" }, [
        el("div", { class: "t", text: `${candidate.artist} — ${candidate.title}` }),
        el("div", {
          class: "m",
          text: [candidate.album, candidate.year, candidate.track_number ? `track ${candidate.track_number}` : ""]
            .filter(Boolean)
            .join("  ·  "),
        }),
        el("div", { class: "tags" }, [
          badge(candidate.source),
          durationBadge,
          badge(fmtDuration(candidate.duration_s)),
          badge(`score ${candidate.score}`),
        ]),
      ]),
    ]);

    input.addEventListener("change", () => {
      for (const other of $("content").querySelectorAll(".card")) other.classList.remove("sel");
      card.classList.add("sel");
    });

    return card;
  }

  function manualSection(session) {
    const coverInput = el("input", { type: "hidden", id: "coverUrl", value: "" });
    const artResults = el("div", { class: "artgrid", id: "artResults" });
    const chosen = el("div", { class: "chosen", id: "artChosen" });
    const query = el("input", {
      type: "text",
      id: "artQuery",
      value: session.artist || "",
      placeholder: "search artwork…",
    });

    const fields = [
      ["title", "Title", session.title || ""],
      ["artist", "Artist", session.artist || ""],
      ["album", "Album", session.album || ""],
      ["album_artist", "Album artist", session.artist || ""],
      ["year", "Year", ""],
      ["track_number", "Track no.", ""],
      ["genre", "Genre", ""],
    ].map(([name, label, value]) =>
      el("label", {}, [label, el("input", { type: "text", name, value })])
    );

    function select(url, label) {
      coverInput.value = url;
      chosen.textContent = "";
      chosen.appendChild(el("img", { src: url, alt: "" }));
      chosen.appendChild(el("span", { class: "sub", text: label || "artwork selected" }));
      for (const node of artResults.querySelectorAll(".artitem")) {
        node.classList.toggle("sel", node.dataset.url === url);
      }
    }

    async function searchArt() {
      const q = query.value.trim();
      if (!q) return;
      artResults.textContent = "searching…";
      try {
        const data = await api(`/api/review/${session.reviewId}/art?q=${encodeURIComponent(q)}`);
        artResults.textContent = "";
        for (const hit of data.results) {
          const button = el(
            "button",
            {
              type: "button",
              class: "artitem",
              title: `${hit.artist} - ${hit.album} (${hit.source})`,
              onclick: () => select(hit.url, `${hit.artist} - ${hit.album}`),
            },
            [el("img", { src: hit.thumb, loading: "lazy", alt: "" })]
          );
          button.dataset.url = hit.url;
          artResults.appendChild(button);
        }
        if (!data.results.length) artResults.textContent = "no artwork found";
      } catch (error) {
        artResults.textContent = `search failed: ${error.message}`;
      }
    }

    const form = el(
      "form",
      {
        onsubmit: async (event) => {
          event.preventDefault();
          const submit = form.querySelector("button[type=submit]");
          if (submit.disabled) return;
          submit.disabled = true;
          submit.textContent = "Working…";

          const payload = {
            title: "",
            artist: "",
            album: "",
            albumArtist: "",
            year: "",
            genre: "",
            coverUrl: coverInput.value,
          };
          for (const input of form.querySelectorAll("input[type=text]")) {
            if (input.name === "album_artist") payload.albumArtist = input.value;
            else if (input.name === "track_number") payload.trackNumber = parseInt(input.value, 10) || null;
            else if (input.name) payload[input.name] = input.value;
          }

          try {
            const result = await api(`/api/review/${session.reviewId}/manual`, {
              method: "POST",
              body: JSON.stringify(payload),
            });
            await clearBadge();
            watchJob(result.jobId, `${result.artist} — ${result.title}`);
            renderDone(`Queued with your metadata — ${result.artist} — ${result.title}`, pendingCount - 1);
          } catch (error) {
            submit.disabled = false;
            submit.textContent = "Download with this metadata";
            message(`Could not queue: ${error.message}`, "error");
          }
        },
      },
      [
        el("div", { class: "grid2" }, fields),
        el("div", { class: "sub", text: "Artwork — search by artist or album, or use the YouTube thumbnail." }),
        coverInput,
        el("div", { class: "row" }, [
          query,
          el("button", { type: "button", onclick: searchArt, text: "Search" }),
          el("button", {
            type: "button",
            text: "YT thumbnail",
            onclick: () =>
              session.videoId &&
              select(`https://i.ytimg.com/vi/${session.videoId}/maxresdefault.jpg`, "YouTube thumbnail"),
          }),
        ]),
        artResults,
        chosen,
        el("div", { class: "row" }, [
          el("button", { type: "submit", class: "primary", text: "Download with this metadata" }),
        ]),
      ]
    );

    return el("details", {}, [
      el("summary", { text: "Can't find it? Enter the metadata manually" }),
      form,
    ]);
  }

  function renderReview(session) {
    setSub(pendingCount > 1 ? `${pendingCount} pending` : "1 pending");

    const blocks = [
      el("div", { class: "src" }, [
        el("img", { src: `https://i.ytimg.com/vi/${session.videoId}/mqdefault.jpg`, alt: "" }),
        el("div", { class: "body" }, [
          el("div", { class: "t", text: session.title || "(unknown title)" }),
          el("div", { class: "m", text: session.artist || "" }),
          el("div", {
            class: "m",
            text: [session.album, fmtDuration(session.durationSec)].filter(Boolean).join("  ·  "),
          }),
        ]),
      ]),
    ];

    // Only present when auto-accept declined — this is the whole reason you are
    // being asked, so it goes at the top rather than in a tooltip.
    if (session.reason) {
      blocks.push(el("div", { class: "note", text: `Why: ${session.reason}` }));
    }
    if (session.already) {
      blocks.push(
        el("div", {
          class: "note",
          text: `Already archived at ${session.already.path} — confirming will download it again.`,
        })
      );
    }
    if (session.errors && session.errors.length) {
      blocks.push(el("div", { class: "note", text: session.errors.join("\n") }));
    }

    if (!session.candidates.length) {
      blocks.push(el("div", { class: "sub", text: "No catalogue match. Use the manual section below." }));
    } else {
      session.candidates.forEach((candidate, index) =>
        blocks.push(candidateCard(candidate, index, session.durationSec))
      );
    }

    const actions = el("div", { class: "row" }, [
      actionButton("Download selected", "primary", async () => {
        const chosen = $("content").querySelector('input[name="choice"]:checked');
        if (!chosen) throw new Error("pick a candidate first");
        const result = await api(`/api/review/${session.reviewId}/confirm`, {
          method: "POST",
          body: JSON.stringify({ choice: parseInt(chosen.value, 10) }),
        });
        await clearBadge();
        watchJob(result.jobId, `${result.artist} — ${result.title}`);
        renderDone(`Queued — ${result.artist} — ${result.title}`, pendingCount - 1);
      }),
      actionButton("Skip", "", async () => {
        await api(`/api/review/${session.reviewId}/skip`, { method: "POST" });
        await clearBadge();
        renderDone("Skipped — nothing was downloaded.", pendingCount - 1);
      }),
      actionButton("Discard", "", async () => {
        await api(`/api/review/${session.reviewId}`, { method: "DELETE" });
        await clearBadge();
        renderDone("Review discarded without a decision.", pendingCount - 1);
      }),
    ]);

    blocks.push(actions);

    if (pendingCount > 1) {
      blocks.push(
        el("div", { class: "row" }, [
          actionButton(`Discard all ${pendingCount}`, "", async () => {
            await api("/api/reviews", { method: "DELETE" });
            await clearBadge();
            await renderIdle();
          }),
          el("button", {
            text: "Open full page",
            onclick: () => browser.tabs.create({ url: `${config.serverUrl}/review/${session.reviewId}` }),
          }),
        ])
      );
    } else {
      blocks.push(
        el("div", { class: "row" }, [
          el("button", {
            text: "Open full page",
            onclick: () => browser.tabs.create({ url: `${config.serverUrl}/review/${session.reviewId}` }),
          }),
          el("button", { text: "Settings", onclick: () => browser.runtime.openOptionsPage() }),
        ])
      );
    }

    blocks.push(manualSection(session));
    show(blocks);
  }

  // ------------------------------------------------------------------- boot

  function renderSetup() {
    setSub("not configured");
    show([
      el("div", {
        class: "note",
        text: "No server token yet. Open Settings, paste the token from server/config.json, then press Test connection.",
      }),
      el("div", { class: "row" }, [
        el("button", { class: "primary", text: "Open settings", onclick: () => browser.runtime.openOptionsPage() }),
      ]),
    ]);
  }

  async function init() {
    await loadSettings();
    if (!config.token) {
      renderSetup();
      return;
    }

    $("content").textContent = "";
    $("content").appendChild(el("div", { class: "spin", text: "Loading…" }));

    try {
      const data = await api("/api/reviews");
      pendingCount = data.reviews.length;
      if (!pendingCount) {
        await renderIdle();
        return;
      }
      renderReview(await api(`/api/review/${data.reviews[0].reviewId}`));
    } catch (error) {
      await clearBadge();
      message(`Cannot reach ${config.serverUrl}\n\n${error.message}`, "error");
    }
  }

  $("settings").addEventListener("click", () => browser.runtime.openOptionsPage());
  $("refresh").addEventListener("click", init);
  init();
})();
