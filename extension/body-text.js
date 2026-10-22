/**
 * Decode Innertube request bodies.
 *
 * YouTube Music started sending its JSON request bodies gzip-compressed. A
 * plain TextDecoder over those bytes produces mojibake with no `videoId` in
 * it — which is why the like path reported "no videoId in body (raw)" while
 * the UI itself was fine.
 *
 * Shared by background.js (the raw body from webRequest) and ytm-net.js (the
 * Blob/ArrayBuffer handed to fetch or XHR). Firefox's DecompressionStream has
 * been available since long before this extension's strict_min_version (128).
 */
(() => {
  "use strict";

  const GZIP_MAGIC = [0x1f, 0x8b];

  function isGzip(bytes) {
    return (
      bytes &&
      bytes.length >= GZIP_MAGIC.length &&
      bytes[0] === GZIP_MAGIC[0] &&
      bytes[1] === GZIP_MAGIC[1]
    );
  }

  /** Bytes → UTF-8 text, transparently inflating a gzip body. */
  async function text(bytes) {
    if (!bytes || !bytes.length) return "";

    if (isGzip(bytes)) {
      if (typeof DecompressionStream === "undefined") return "";
      try {
        const stream = new Blob([bytes])
          .stream()
          .pipeThrough(new DecompressionStream("gzip"));
        return await new Response(stream).text();
      } catch (_) {
        // A body that claims gzip but is not, or a stream the browser refuses
        // to inflate. The caller treats empty text as "no id readable".
        return "";
      }
    }

    try {
      return new TextDecoder("utf-8").decode(bytes);
    } catch (_) {
      return "";
    }
  }

  globalThis.InvokrBody = { isGzip, text };
})();
