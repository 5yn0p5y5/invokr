/**
 * Regression tests for gzip-compressed Innertube request bodies.
 *
 *     node tools/test-body-text.js
 *
 * YouTube Music started gzipping its like request bodies. The decoders in
 * extension/body-text.js (shared), background.js (webRequest raw bytes) and
 * ytm-net.js (fetch/XHR Blob or typed array) must inflate them before looking
 * for a video id.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const zlib = require("zlib");

const extension = path.join(__dirname, "..", "extension");
const results = [];

function check(name, ok, detail = "") {
  results.push(ok);
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
}

const sample = JSON.stringify({ target: { videoId: "rOheV30BYtE" } });
const raw = new Uint8Array(Buffer.from(sample, "utf8"));
const gz = new Uint8Array(zlib.gzipSync(Buffer.from(sample, "utf8")));

const context = vm.createContext({
  Blob,
  Response,
  TextDecoder,
  DecompressionStream,
  console,
});
vm.runInContext(
  fs.readFileSync(path.join(extension, "body-text.js"), "utf8"),
  context,
  { filename: "body-text.js" }
);
const body = vm.runInContext("InvokrBody", context);

// Load the real background page against enough browser API to evaluate it.
const backgroundContext = vm.createContext({
  Blob,
  Response,
  TextDecoder,
  DecompressionStream,
  console,
  setTimeout,
  clearTimeout,
  InvokrBody: body,
  browser: {
    storage: { local: { get: async () => ({}) } },
    action: {
      setBadgeText: async () => {},
      setBadgeBackgroundColor: async () => {},
    },
    notifications: { create: async () => {} },
    runtime: { onMessage: { addListener: () => {} } },
    webRequest: { onBeforeRequest: { addListener: () => {} } },
    tabs: { sendMessage: async () => null },
    permissions: { contains: async () => true, request: async () => true },
  },
});
vm.runInContext(
  fs.readFileSync(path.join(extension, "background.js"), "utf8"),
  backgroundContext,
  { filename: "background.js" }
);
const decodeRequestBody = vm.runInContext("decodeRequestBody", backgroundContext);
const videoIdFromBodyText = vm.runInContext("videoIdFromBodyText", backgroundContext);

(async () => {
  check("detects the gzip magic", body.isGzip(gz));
  check("does not mistake UTF-8 for gzip", !body.isGzip(raw));
  check("decodes a plain body", (await body.text(raw)) === sample);
  check("inflates a gzip body", (await body.text(gz)) === sample);
  check("empty body stays empty", (await body.text(new Uint8Array())) === "");
  check(
    "truncated gzip fails closed",
    (await body.text(gz.slice(0, 8))) === ""
  );

  const chunk = gz.buffer.slice(gz.byteOffset, gz.byteOffset + gz.byteLength);
  const decoded = await decodeRequestBody({ raw: [{ bytes: chunk }] });
  check("webRequest body reports gzip", decoded.kind === "gzip", decoded.kind);
  check("webRequest body inflates", decoded.text === sample);
  check(
    "webRequest body yields the video id",
    videoIdFromBodyText(decoded.text) === "rOheV30BYtE",
    videoIdFromBodyText(decoded.text)
  );

  const failed = results.filter((ok) => !ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
})();
