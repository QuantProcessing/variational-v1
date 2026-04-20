// content_script.js — runs in variational.io page context.
// Only trusts messages from the extension's background worker.
// Executes fetch() and returns (status, body, latencyMs) verbatim.

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "EXECUTE_FETCH") {
    return false;
  }
  if (!sender || sender.id !== chrome.runtime.id) {
    sendResponse({ ok: false, error: "untrusted_sender" });
    return false;
  }

  const fetchSpec = msg.fetch || {};
  const timeoutMs = Math.max(500, Number(msg.timeoutMs) || 5000);

  (async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const t0 = performance.now();
    try {
      const response = await fetch(fetchSpec.url, {
        method: fetchSpec.method || "GET",
        headers: fetchSpec.headers || {},
        body: fetchSpec.body !== undefined ? fetchSpec.body : undefined,
        credentials: "include",
        signal: controller.signal,
      });
      const text = await response.text();
      sendResponse({
        ok: true,
        status: response.status,
        body: text,
        latencyMs: Math.round(performance.now() - t0),
      });
    } catch (err) {
      const isAbort = err && err.name === "AbortError";
      sendResponse({
        ok: false,
        error: isAbort ? "timeout" : String(err && err.message ? err.message : err),
        latencyMs: Math.round(performance.now() - t0),
      });
    } finally {
      clearTimeout(timer);
    }
  })();

  return true; // keep sendResponse channel open for async work
});

// Strong signal for debugging: show up in DevTools console once loaded.
console.log("[variational-forwarder] content_script loaded");
