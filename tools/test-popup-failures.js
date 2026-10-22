/**
 * Regression tests for the popup's failure panel in extension/popup.js.
 *
 *     node tools/test-popup-failures.js
 *
 * The extension and the server are installed separately, so an extension newer
 * than its server is a normal state rather than a fault. This drives the real
 * popup against a fake server that answers 404 for an endpoint it does not
 * have, and checks the panel still renders — a missing optional endpoint used
 * to replace the whole panel with "Server: HTTP 404: Not Found".
 *
 * Only the DOM and `fetch` are faked; the popup's own logic is the real thing.
 */
const fs = require("fs");
const path = require("path");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ------------------------------------------------------------------ fake DOM

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attrs = {};
    this.handlers = {};
    this._text = "";
    this.classList = {
      _set: new Set(),
      add: (name) => this.classList._set.add(name),
      remove: (name) => this.classList._set.delete(name),
      contains: (name) => this.classList._set.has(name),
    };
  }
  appendChild(node) {
    this.children.push(node);
    return node;
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join(" ");
  }
  set className(value) {
    this.attrs.class = value;
  }
  get className() {
    return this.attrs.class || "";
  }
  setAttribute(name, value) {
    this.attrs[name] = value;
  }
  getAttribute(name) {
    return this.attrs[name];
  }
  addEventListener(name, fn) {
    (this.handlers[name] = this.handlers[name] || []).push(fn);
  }
  querySelector() {
    return null;
  }
}

const nodes = {
  content: new FakeNode("div"),
  sub: new FakeNode("span"),
  libCount: new FakeNode("span"),
  settings: new FakeNode("button"),
  refresh: new FakeNode("button"),
};

global.document = {
  getElementById: (id) => nodes[id] || null,
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => {
    const node = new FakeNode("#text");
    node.textContent = text;
    return node;
  },
};

// ----------------------------------------------------------------- fake server

let server;
let stored = { token: "test-token" };
let permissionGranted = true;
function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

global.fetch = async (url, options = {}) => {
  const route = String(url).replace(/^https?:\/\/[^/]+/, "").split("?")[0];
  const handler = server[route];
  if (!handler) return response(404, { detail: "Not Found" });
  return handler({ method: options.method || "GET" });
};

global.browser = {
  storage: { local: { get: async () => ({ ...stored }) } },
  action: { setBadgeText: async () => {} },
  runtime: { sendMessage: async () => {}, openOptionsPage: () => {} },
  tabs: { create: () => {} },
  // The popup only ever asks whether the origin is granted; it cannot prompt.
  permissions: {
    contains: async () => permissionGranted,
    request: async () => permissionGranted,
    getAll: async () => ({ origins: [] }),
  },
};

// ---------------------------------------------------------------------- cases

const results = [];

function check(name, ok, detail = "") {
  results.push(ok);
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
}

function walk(node, out = []) {
  out.push(node);
  for (const child of node.children) walk(child, out);
  return out;
}

/** Buttons, not the word — "Retrying usually works" contains "Retry" too. */
function buttons(label) {
  return walk(nodes.content).filter(
    (node) => node.tagName === "BUTTON" && node.textContent.trim() === label
  );
}

function retryButtons() {
  return buttons("Retry");
}

const FAILED_JOB = {
  job_id: "abc123",
  status: "failed",
  title: "Artist — A Song",
  error: "DownloadError: bot check",
};

function newServer({ withFailuresEndpoint }) {
  server = {
    "/api/reviews": () => response(200, { reviews: [] }),
    "/api/queue": () => response(200, { depth: { done: 21, failed: 3 } }),
    "/api/jobs": () => response(200, { jobs: [FAILED_JOB] }),
    "/api/stats": () => response(200, { tracks: 21, playable: 21, by_status: {} }),
  };
  if (withFailuresEndpoint) {
    server["/api/failures"] = () => response(200, { failures: [FAILED_JOB] });
  }
}

async function loadPopup() {
  const read = (name) =>
    fs.readFileSync(path.join(__dirname, "..", "extension", name), "utf8");
  // server-access.js declares InvokrAccess at the top level of a classic script,
  // so evaluate it in the same scope and publish it where popup.js will look.
  eval(`${read("server-access.js")}\nglobalThis.InvokrAccess = InvokrAccess;`);
  eval(read("popup.js")); // eslint-disable-line no-eval
  await sleep(60); // init() is fired at the end of the file, not awaited
  return nodes.content.textContent;
}

(async () => {
  // 1. An older server: no /api/failures at all.
  newServer({ withFailuresEndpoint: false });
  let text = await loadPopup();

  check("older server: the panel does not blank out", !/Server:\s*HTTP 404/.test(text), text.slice(0, 60));
  check("older server: the queue is still shown", /Queue —/.test(text));
  check("older server: the failure is still listed", /A Song/.test(text));
  check("older server: it says the server is old", /older build/.test(text));
  check("older server: no Retry button that would 404", retryButtons().length === 0, `${retryButtons().length} button(s)`);
  check(
    "older server: the raw failed count is not shown as live",
    !/failed: 3/.test(text.replace(/older build[\s\S]*/, "")),
  );

  // 2. A matching server.
  newServer({ withFailuresEndpoint: true });
  text = await loadPopup();

  check("current server: the failure is reported", /Download failed/.test(text));
  check("current server: a Retry button is offered", retryButtons().length === 1, `${retryButtons().length} button(s)`);
  check("current server: no stale-build hint", !/older build/.test(text));

  // 3. A matching server with nothing outstanding.
  newServer({ withFailuresEndpoint: true });
  server["/api/failures"] = () => response(200, { failures: [] });
  text = await loadPopup();

  check("nothing outstanding: no complaint", !/Download failed/.test(text));
  check("nothing outstanding: no Retry button", retryButtons().length === 0, `${retryButtons().length} button(s)`);

  // 4. Clearing failures deletes them on the server and re-renders without the
  //    complaint.
  newServer({ withFailuresEndpoint: true });
  let clearedFailures = false;
  server["/api/failures"] = ({ method } = {}) => {
    if (method === "DELETE") {
      clearedFailures = true;
      return response(200, { cleared: 1, tracksRemoved: 1 });
    }
    return response(200, { failures: clearedFailures ? [] : [FAILED_JOB] });
  };
  text = await loadPopup();
  const clearButtons = buttons("Clear failed");
  check("current server: a Clear failed button is offered", clearButtons.length === 1, `${clearButtons.length} button(s)`);
  await clearButtons[0].handlers.click[0]();
  await sleep(60);
  text = nodes.content.textContent;
  check("clear failed: DELETE reaches the server", clearedFailures);
  check("clear failed: the complaint is gone", !/Download failed/.test(text));

  // 5. A server on another machine whose host was never granted. That is a
  //    permission problem, not a dead server, and the panel says which.
  stored = { serverUrl: "http://192.168.1.50:8765", token: "test-token" };
  permissionGranted = false;
  text = await loadPopup();

  check("denied host: names the host", /has not given invokr access to 192\.168\.1\.50:8765/.test(text), text.slice(0, 80));
  check("denied host: does not blame the server", !/Cannot reach/.test(text));

  const failed = results.filter((result) => !result).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
})();
