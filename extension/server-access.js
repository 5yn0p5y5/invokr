/**
 * Host access for the configured server origin.
 *
 * A server on another machine is a cross-origin fetch. Firefox allows one
 * without CORS only after the user grants that origin at runtime, so
 * manifest.json declares an optional ceiling and the extension asks for the
 * exact origin behind `serverUrl`. Never request the wildcard itself: the
 * grant then covers one host, and about:addons → invokr → Permissions shows
 * exactly which host that is and lets it be revoked.
 *
 * The default `http://127.0.0.1:8765` needs none of this — it is in the
 * manifest's `host_permissions`, so every path stays prompt-free for the
 * common case.
 */

// Kept in step with host_permissions in manifest.json. These are granted at
// install and cannot be requested at runtime.
const InvokrAccess = (() => {
  const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost"]);

  /**
   * The narrowest match pattern that covers `serverUrl`.
   *
   * Ports are deliberately dropped: Firefox match patterns ignore them (and
   * reject a port in the host), so the grant covers the server on any port.
   */
  function originPattern(serverUrl) {
    let url;
    try {
      url = new URL(serverUrl);
    } catch (_) {
      return null;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return `${url.protocol}//${url.hostname}/*`;
  }

  function isLocal(serverUrl) {
    try {
      const url = new URL(serverUrl);
      // Only http is pre-authorized by host_permissions; https on localhost
      // would need the same runtime grant as any other host.
      return url.protocol === "http:" && LOCAL_HOSTS.has(url.hostname);
    } catch (_) {
      return false;
    }
  }

  /** Is the server origin already reachable without a prompt? */
  async function has(serverUrl) {
    if (isLocal(serverUrl)) return true;
    const origin = originPattern(serverUrl);
    if (!origin) return false;
    try {
      return await browser.permissions.contains({ origins: [origin] });
    } catch (_) {
      return false;
    }
  }

  /**
   * Ask for the configured origin.
   *
   * Call this synchronously from a click handler: Firefox only runs
   * `permissions.request` inside a user action. It resolves immediately for
   * localhost and for an origin that is already granted, so calling it on
   * every save costs nothing.
   */
  async function request(serverUrl) {
    if (isLocal(serverUrl)) return true;
    const origin = originPattern(serverUrl);
    if (!origin) return false;
    try {
      return await browser.permissions.request({ origins: [origin] });
    } catch (_) {
      // An origin that is already required, or a browser without the API, can
      // refuse the call even though access is in place. Ask the real question.
      return has(serverUrl);
    }
  }

  /** Plain-language explanation for a request that the browser blocked. */
  function denied(serverUrl) {
    let host = serverUrl;
    try {
      host = new URL(serverUrl).host;
    } catch (_) {
      /* keep the raw string */
    }
    return (
      `Firefox has not given invokr access to ${host}.\n\n` +
      `Open Settings, check the server URL, and press Save to grant it. ` +
      `The grant covers that one host and can be revoked from ` +
      `about:addons → invokr → Permissions.`
    );
  }

  return { originPattern, isLocal, has, request, denied };
})();
