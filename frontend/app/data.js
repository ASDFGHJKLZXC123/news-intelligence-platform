/* ============================================================================
   SIGNAL — fixture loader + readiness coordinator (Stage 7, item 6B)

   This file is NOT the wire layer. It knows only the two page-facing fixture
   files (camelCase, matching app/types.ts) and the public SignalApiAdapter
   surface. It fetches the fixtures, hands them to SignalApiAdapter — which owns
   every endpoint, backend wire key, pagination/error envelope, alert lifecycle
   map, company enrichment and synthetic-series decision — then publishes the
   single readiness contract the React app mounts on:

     • window.DATA               — the assembled snapshot, assigned exactly once
     • window.__signalDataReady  — a Promise that resolves to that same object
     • "signal:data-ready" event — detail === that same object, dispatched once

   No polling. When the API is wholly unreachable the adapter returns a complete
   fixture demo snapshot, so DATA is still assigned, the event still fires, and
   the app mounts normally. The fatal boot screen appears ONLY when a required
   fixture file — or the adapter script itself — cannot load or initialize.
   A boundary test keeps any wire knowledge from leaking back into this file.
   ========================================================================== */
(function () {
  "use strict";

  // The two page-facing fixture files (their camelCase shape is the app schema).
  var coreFixtureUrl = "data/signal-mock-data.json";
  var eventsFixtureUrl = "data/events.json";

  // Escape untrusted text before it ever reaches innerHTML (fatal screen only).
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Fetch one fixture file as JSON; a non-ok response is a true boot failure.
  function fetchFixture(url) {
    var bust = "?v=" + Date.now();
    return window.fetch(url + bust).then(function (response) {
      if (!response.ok) throw new Error("Failed to load " + url + " (" + response.status + ")");
      return response.json();
    });
  }

  // Publish exactly once: assign DATA first (so a late main.jsx already sees it),
  // then dispatch the ready event with the SAME object. Returns it so the
  // readiness Promise resolves to the identical reference — never a copy.
  function publish(snapshot) {
    window.DATA = snapshot;
    window.dispatchEvent(new CustomEvent("signal:data-ready", { detail: snapshot }));
    return snapshot;
  }

  // The fatal boot screen — a required fixture or the adapter itself failed to
  // load. API failures never reach here (the adapter returns a demo snapshot).
  function fatal(err) {
    console.error("[SIGNAL] data load failed:", err);
    var root = document.getElementById("root");
    if (root) {
      var safeMessage = escapeHtml(err && err.message ? err.message : "Unknown error");
      root.innerHTML =
        '<div style="display:grid;place-items:center;height:100vh;font-family:system-ui;color:#aab4c5;text-align:center;padding:24px">' +
        '<div><div style="font-size:16px;font-weight:600;color:#e8edf6;margin-bottom:8px">Unable to load intelligence data</div>' +
        '<div style="font-size:13px">' + safeMessage +
        '<br/>Serve this file over http (not file://) so the data fixtures can be fetched.</div></div></div>';
    }
    throw err;
  }

  window.__signalDataReady = (function load() {
    var adapter = window.SignalApiAdapter;
    if (!adapter || typeof adapter.loadSnapshot !== "function") {
      // The adapter classic script must load before this loader (see the HTML).
      return Promise.reject(new Error("SignalApiAdapter is unavailable (app/api-adapter.js failed to load)"));
    }
    return Promise.all([fetchFixture(coreFixtureUrl), fetchFixture(eventsFixtureUrl)])
      .then(function (fixtures) {
        // All API loading + snapshot assembly (including the total-unreachable
        // demo fallback) is the adapter's job; this loader never sees wire shapes.
        return adapter.loadSnapshot({
          fixtures: { core: fixtures[0], events: fixtures[1] },
          apiBase: window.SIGNAL_API_BASE,
          fetch: window.fetch.bind(window),
        });
      })
      .then(publish);
  })().catch(fatal);
})();
