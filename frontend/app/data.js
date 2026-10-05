/* SIGNAL — personal display-mode loader and readiness coordinator.

   Real mode is the unconditional default. It requests the API without reading
   fixture files. The two fixture files are loaded only after an explicit switch
   to the read-only demonstration. Every request belongs to a generation so a
   late response from an earlier mode can never publish into the current view.
*/
(function () {
  "use strict";

  var coreFixtureUrl = "data/signal-mock-data.json";
  var eventsFixtureUrl = "data/events.json";

  function createController(host, adapter) {
    var mode = "real";
    var generation = 0;
    var activeAbort = null;
    var initialPublished = false;

    function dispatch(type, snapshot) {
      host.dispatchEvent(new CustomEvent(type, { detail: snapshot }));
    }

    function publish(snapshot) {
      host.DATA = snapshot;
      if (!initialPublished) {
        initialPublished = true;
        dispatch("signal:data-ready", snapshot);
      } else {
        dispatch("signal:data-changed", snapshot);
      }
      return snapshot;
    }

    function nowIso() { return new Date().toISOString(); }

    function emptyFor(targetMode, loading) {
      return adapter.buildEmptySnapshot({
        mode: targetMode,
        apiBase: host.SIGNAL_API_BASE,
        now: nowIso(),
        loading: !!loading,
        reason: loading ? (targetMode === "demo" ? "Loading sample data" : "Loading stored data") : undefined,
      });
    }

    function abortPrevious() {
      if (activeAbort && typeof activeAbort.abort === "function") activeAbort.abort();
      activeAbort = null;
    }

    function requestFetch() {
      var Controller = host.AbortController;
      activeAbort = Controller ? new Controller() : null;
      var signal = activeAbort && activeAbort.signal;
      return function (url, options) {
        var init = Object.assign({}, options || {});
        if (signal) init.signal = signal;
        return host.fetch(url, init);
      };
    }

    function fetchFixture(fetchImpl, url) {
      return fetchImpl(url + "?v=" + Date.now()).then(function (response) {
        if (!response.ok) throw new Error("Failed to load " + url + " (" + response.status + ")");
        return response.json();
      });
    }

    function staleSnapshot(previous, failure) {
      var failedRuntime = failure.runtime || {};
      var priorRuntime = previous.runtime || {};
      return Object.assign({}, previous, {
        apiErrors: failure.apiErrors || [],
        runtime: Object.assign({}, priorRuntime, {
          mode: "real",
          degraded: true,
          stale: true,
          loading: false,
          requestFailed: true,
          reason: "Unable to refresh stored data",
          fetchAttemptedAt: failedRuntime.fetchAttemptedAt || failedRuntime.now || nowIso(),
          lastSuccessfulFetchAt: priorRuntime.lastSuccessfulFetchAt || null,
        }),
      });
    }

    async function loadReal(token, previous) {
      var snapshot = await adapter.loadSnapshot({
        mode: "real",
        apiBase: host.SIGNAL_API_BASE,
        fetch: requestFetch(),
      });
      if (token !== generation || mode !== "real") return null;
      activeAbort = null;
      if (snapshot.runtime && snapshot.runtime.degraded && previous &&
          previous.runtime && previous.runtime.mode === "real" && previous.runtime.lastSuccessfulFetchAt) {
        return publish(staleSnapshot(previous, snapshot));
      }
      return publish(snapshot);
    }

    async function loadDemo(token) {
      var fetchImpl = requestFetch();
      var fixtures = await Promise.all([
        fetchFixture(fetchImpl, coreFixtureUrl),
        fetchFixture(fetchImpl, eventsFixtureUrl),
      ]);
      if (token !== generation || mode !== "demo") return null;
      activeAbort = null;
      return publish(adapter.buildFixtureSnapshot(fixtures[0], fixtures[1], {
        apiBase: host.SIGNAL_API_BASE,
        reason: "explicit read-only demonstration",
      }));
    }

    function load(targetMode, options) {
      options = options || {};
      abortPrevious();
      mode = targetMode === "demo" ? "demo" : "real";
      generation += 1;
      var token = generation;
      var previous = mode === "real" && !options.clear ? host.DATA : null;
      if (options.clear && initialPublished) publish(emptyFor(mode, true));

      var work = mode === "demo" ? loadDemo(token) : loadReal(token, previous);
      return work.catch(function (err) {
        if (token !== generation || mode !== targetMode) return null;
        activeAbort = null;
        var failure = adapter.buildEmptySnapshot({
          mode: mode,
          apiBase: host.SIGNAL_API_BASE,
          now: nowIso(),
          requestFailed: true,
          fetchAttemptedAt: nowIso(),
          reason: mode === "demo" ? "Sample data could not be loaded" : "The API is unavailable",
          error: {
            endpoint: "display-load",
            code: "load-error",
            message: err && err.message ? err.message : "request failed",
            requestId: null,
          },
        });
        if (mode === "real" && previous && previous.runtime && previous.runtime.lastSuccessfulFetchAt) {
          return publish(staleSnapshot(previous, failure));
        }
        return publish(failure);
      });
    }

    return {
      start: function () { return load("real", { clear: false }); },
      refresh: function () { return load(mode, { clear: false }); },
      setMode: function (nextMode) {
        var normalized = nextMode === "demo" ? "demo" : "real";
        if (normalized === mode && host.DATA && !host.DATA.runtime.loading) return Promise.resolve(host.DATA);
        return load(normalized, { clear: true });
      },
      getMode: function () { return mode; },
      getGeneration: function () { return generation; },
    };
  }

  var adapter = window.SignalApiAdapter;
  if (!adapter || typeof adapter.loadSnapshot !== "function" ||
      typeof adapter.buildFixtureSnapshot !== "function" || typeof adapter.buildEmptySnapshot !== "function") {
    var missing = new Error("SignalApiAdapter is unavailable (app/api-adapter.js failed to load)");
    window.__signalDataReady = Promise.reject(missing);
    return;
  }

  window.createSignalDataController = createController;
  window.SignalDataController = createController(window, adapter);
  window.__signalDataReady = window.SignalDataController.start();
})();
