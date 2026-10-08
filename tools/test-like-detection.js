/**
 * Regression tests for the like-detection state machine in
 * extension/ytm-bridge.js.
 *
 *     node tools/test-like-detection.js
 *
 * No dependencies and no browser: the script is loaded against a small fake DOM
 * and driven through the scenarios that have actually gone wrong in the field.
 * The one that matters most is the first — a track whose `like-status`
 * attribute is never rewritten, which is what made roughly one like in five
 * vanish silently.
 *
 * This covers the state machine, not the browser: it cannot tell whether
 * YouTube Music really writes the attribute the way the fake DOM does, nor
 * whether `world: "MAIN"` was honoured. Run **Run diagnostics** in the
 * extension for that.
 */
const fs = require("fs");
const path = require("path");

const sent = [];
const intervals = [];
const documentHandlers = {};
const windowHandlers = {};
const observers = [];

class FakeObserver {
  constructor(cb) {
    this.cb = cb;
    observers.push(this);
  }
  observe(target) {
    this.target = target;
    if (target) target.__observer = this;
  }
  disconnect() {
    if (this.target) this.target.__observer = null;
  }
  fire() {
    this.cb([]);
  }
}

class FakeElement {
  constructor(tag, attrs = {}, opts = {}) {
    this.tagName = tag.toUpperCase();
    this.attrs = { ...attrs };
    this.inBar = !!opts.inBar;
    this.isConnected = true;
    this.parentElement = null;
    this.__observer = null;
  }
  getAttribute(name) {
    return name in this.attrs ? this.attrs[name] : null;
  }
  setAttribute(name, value) {
    this.attrs[name] = value;
    if (this.__observer) this.__observer.fire();
  }
  querySelector() {
    return null;
  }
  querySelectorAll() {
    return [];
  }
  closest(sel) {
    return sel === "ytmusic-player-bar" && this.inBar ? this : null;
  }
  get textContent() {
    return "";
  }
}

// ------------------------------------------------------------ mutable world

const world = {
  videoId: "AAAAAAAAAAA",
  status: "INDIFFERENT",
  // A browse page: the URL carries no ?v= and the player bar has no watch link,
  // which is the only state in which the page's player is the sole identifier.
  url: "https://music.youtube.com/watch?v=AAAAAAAAAAA",
  barLink: true,
};

const renderer = new FakeElement("ytmusic-like-button-renderer", { "like-status": "INDIFFERENT" }, { inBar: true });
const link = {
  getAttribute: () => `watch?v=${world.videoId}`,
};
const bar = new FakeElement("ytmusic-player-bar", {}, { inBar: true });
bar.querySelector = (sel) => (sel.includes("watch?v=") && world.barLink ? link : null);

const documentElement = new FakeElement("html");

global.document = {
  readyState: "complete",
  documentElement,
  querySelector(sel) {
    if (sel === "ytmusic-player-bar") return bar;
    if (sel.includes("#like-button-renderer")) return renderer;
    if (sel.includes("ytmusic-like-button-renderer")) return renderer;
    return null;
  },
  getElementById: () => null,
  addEventListener(name, fn) {
    (documentHandlers[name] = documentHandlers[name] || []).push(fn);
  },
};

global.window = {
  location: {
    get href() {
      return world.url;
    },
    origin: "https://music.youtube.com",
  },
  setInterval(fn) {
    intervals.push(fn);
    return intervals.length;
  },
  setTimeout: (...a) => setTimeout(...a),
  addEventListener(name, fn) {
    (windowHandlers[name] = windowHandlers[name] || []).push(fn);
  },
};
global.navigator = {};
global.Element = FakeElement;
global.MutationObserver = FakeObserver;
global.browser = {
  runtime: {
    sendMessage(message) {
      sent.push(message.payload);
      return Promise.resolve();
    },
    onMessage: { addListener() {} },
  },
};

// ------------------------------------------------------------------- load

const source = fs.readFileSync(path.join(__dirname, "..", "extension", "ytm-bridge.js"), "utf8");
eval(source); // eslint-disable-line no-eval

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function poll() {
  for (const fn of intervals) fn();
}

function fireClick() {
  for (const fn of (documentHandlers.click || [])) {
    fn({ target: { closest: () => renderer } });
  }
}

function setStatus(value) {
  world.status = value;
  renderer.setAttribute("like-status", value);
}

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
}

(async () => {
  await sleep(50);

  // 1. The reported bug: the track starts INDIFFERENT and the attribute is
  //    never rewritten, so the only way a baseline exists is the poll.
  poll();
  await sleep(50);
  check("poll records a baseline without emitting", sent.length === 0, `sent=${sent.length}`);

  setStatus("LIKE");
  await sleep(600);
  check(
    "like on a track whose attribute never changed is detected",
    sent.length === 1 && sent[0].action === "like" && sent[0].videoId === "AAAAAAAAAAA",
    JSON.stringify(sent.map((m) => `${m.action}:${m.videoId}:${m.source}`))
  );

  // 2. Repeated same-value writes (Polymer) must not emit again.
  setStatus("LIKE");
  await sleep(600);
  check("repeat rewrite of the same value does not re-emit", sent.length === 1, `sent=${sent.length}`);

  // 3. Un-like, then re-like.
  setStatus("INDIFFERENT");
  await sleep(600);
  setStatus("LIKE");
  await sleep(600);
  check(
    "un-like then re-like reports both",
    sent.length === 3 &&
      sent[1].action === "unlike" &&
      sent[2].action === "like" &&
      sent[2].videoId === "AAAAAAAAAAA",
    JSON.stringify(sent.map((m) => `${m.action}:${m.videoId}`))
  );

  // 4. Track change races the attribute rewrite: a liked track is replaced, the
  //    renderer is reset to INDIFFERENT while the bar still shows the old song.
  //    That must not be reported as an un-like of the old song.
  const before = sent.length;
  world.videoId = "BBBBBBBBBBB";
  setStatus("INDIFFERENT"); // bar still reads the old id at this instant
  await sleep(100);
  world.videoId = "CCCCCCCCCCC"; // bar catches up before the confirm fires
  await sleep(600);
  check("track change does not emit a spurious un-like", sent.length === before, `sent=${sent.length}`);

  // 5. The click path: a track the poll has not seen yet, liked immediately.
  const clicked = sent.length;
  world.videoId = "DDDDDDDDDDD";
  renderer.attrs["like-status"] = "INDIFFERENT"; // fresh track, no baseline
  fireClick();
  await sleep(30);
  setStatus("LIKE");
  await sleep(700);
  check(
    "click rescues a like on a track with no baseline yet",
    sent.length === clicked + 1 &&
      sent[clicked].action === "like" &&
      sent[clicked].videoId === "DDDDDDDDDDD",
    JSON.stringify(sent.slice(clicked).map((m) => `${m.action}:${m.videoId}:${m.source}`))
  );

  // 6. The page-world sniffer reports a like of a track that is NOT playing.
  const networked = sent.length;
  for (const fn of (windowHandlers.message || [])) {
    fn({
      source: global.window,
      origin: "https://music.youtube.com",
      data: { __invokr: "invokr/network-like", action: "like", videoId: "EEEEEEEEEEE" },
    });
  }
  await sleep(50);
  check(
    "page-network like of a non-playing track is forwarded",
    sent.length === networked + 1 &&
      sent[networked].source === "page-network" &&
      sent[networked].videoId === "EEEEEEEEEEE",
    JSON.stringify(sent.slice(networked).map((m) => `${m.action}:${m.videoId}:${m.source}`))
  );

  // 7. A malformed page message must be ignored.
  const malformed = sent.length;
  for (const fn of (windowHandlers.message || [])) {
    fn({ source: global.window, origin: "https://music.youtube.com", data: { __invokr: "network-like", action: "like", videoId: "x" } });
  }
  await sleep(50);
  check("foreign message shape is ignored", sent.length === malformed, `sent=${sent.length}`);

  // 8. The reported bug: a browse page, so the URL has no ?v= and the collapsed
  //    player bar has no watch link either. The only thing that knows which
  //    song is playing is the page's own player, which reports it over the
  //    bridge — with `source: null`, because across Firefox's Xray boundary the
  //    sender is not reliably the content script's own `window`.
  const browse = sent.length;
  world.url = "https://music.youtube.com/playlist?list=PL123";
  world.barLink = false;
  world.videoId = "SHOULDNOTBE"; // deliberately not a valid id: nothing may use it
  renderer.attrs["like-status"] = "INDIFFERENT";

  for (const fn of (windowHandlers.message || [])) {
    fn({
      source: null,
      origin: "https://music.youtube.com",
      data: {
        __invokr: "invokr/network-like",
        track: { videoId: "GGGGGGGGGGG", title: "Browsing Song", author: "Some Artist" },
      },
    });
  }
  await sleep(50);
  check("a page-reported track is accepted without event.source", sent.length === browse, `sent=${sent.length}`);

  poll(); // record the baseline for the track the player named
  await sleep(50);
  setStatus("LIKE");
  await sleep(600);

  check(
    "a like on a browse page is attributed to the playing track",
    sent.length === browse + 1 &&
      sent[browse].action === "like" &&
      sent[browse].videoId === "GGGGGGGGGGG",
    JSON.stringify(sent.slice(browse).map((m) => `${m.action}:${m.videoId}`))
  );
  check(
    "and it carries the metadata the player supplied",
    sent.length === browse + 1 &&
      sent[browse].title === "Browsing Song" &&
      (sent[browse].artists || []).includes("Some Artist"),
    JSON.stringify(sent.slice(browse).map((m) => `${m.title}/${(m.artists || []).join(",")}`))
  );

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
})();
