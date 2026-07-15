/* ============================================================================
   SIGNAL — data.js loader + readiness tests (Stage 7, item 6B)

   Zero-build: plain `node --test`, Node built-ins only (node:test + node:vm).
   No package.json / npm / dependency / build artifact. Run from the repo root:

       node --test frontend/app/data-loader.test.js

   data.js is evaluated inside a vm sandbox with a deterministic fake
   window / document / fetch / CustomEvent, so no network and no real DOM are
   used. The real HTML base-assignment snippet is extracted from the page and
   run verbatim so the tests exercise the actual boot wiring, not a copy.

   Covers: HTML adapter-before-loader ordering + classic-script shape; the base
   snippet default assignment without overwriting a provided base; all three
   readiness contracts with identity equality; exactly one DATA assignment and
   one event; the fixtures are fetched; loadSnapshot receives fixtures/base/fetch
   and its snapshot is passed through unreshaped; the full API-unreachable demo
   snapshot (real adapter) still resolves and dispatches; a degraded/partial
   snapshot is dispatched unchanged; a fixture-fetch failure rejects and renders
   an HTML-safe fatal screen; a missing adapter is fatal; and the source-boundary
   guard that keeps wire knowledge out of data.js.
   ========================================================================== */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_DIR = __dirname;
const DATA_JS_PATH = path.join(APP_DIR, "data.js");
const ADAPTER_PATH = path.join(APP_DIR, "api-adapter.js");
const HTML_PATH = path.join(APP_DIR, "..", "SIGNAL - Intelligence Platform.html");

const DATA_JS_SRC = fs.readFileSync(DATA_JS_PATH, "utf8");
const HTML_SRC = fs.readFileSync(HTML_PATH, "utf8");
const REAL_ADAPTER = require(ADAPTER_PATH);

/* ---- Fake DOM primitives -------------------------------------------------- */
class FakeCustomEvent {
  constructor(type, init) { this.type = type; this.detail = init ? init.detail : undefined; }
}

function makeDocument(root) {
  return { getElementById: (id) => (id === "root" ? root : null) };
}

// A window whose DATA setter counts assignments and whose dispatchEvent records.
function makeWindow(cfg) {
  const events = [];
  const state = { value: undefined, assignments: 0 };
  const win = {
    SignalApiAdapter: cfg.adapter,
    fetch: cfg.fetchImpl,
    dispatchEvent(ev) { events.push(ev); return true; },
    addEventListener() {},
  };
  if ("presetApiBase" in cfg) win.SIGNAL_API_BASE = cfg.presetApiBase;
  Object.defineProperty(win, "DATA", {
    configurable: true, enumerable: true,
    get() { return state.value; },
    set(v) { state.value = v; state.assignments += 1; },
  });
  win.__events = events;
  win.__state = state;
  return win;
}

function okJson(body) {
  return { ok: true, status: 200, json: async () => body };
}

// The real HTML inline snippet that assigns window.SIGNAL_API_BASE.
function extractApiBaseSnippet(html) {
  const m = html.match(/<script>([\s\S]*?SIGNAL_API_BASE[\s\S]*?)<\/script>/);
  assert.ok(m, "SIGNAL_API_BASE inline snippet not found in HTML");
  return m[1];
}
const API_BASE_SNIPPET = extractApiBaseSnippet(HTML_SRC);

// Emulate the HTML boot: adapter already loaded, run the real base snippet, then
// evaluate data.js — all in one shared vm context with host-realm primitives.
function runLoader(cfg) {
  const root = { innerHTML: "" };
  const win = makeWindow(cfg);
  const sandbox = {
    window: win,
    document: makeDocument(root),
    console: { error() {}, warn() {}, log() {} },
    CustomEvent: FakeCustomEvent,
    Promise: Promise,
    Date: Date,
    URL: URL,
  };
  vm.createContext(sandbox);
  vm.runInContext(API_BASE_SNIPPET, sandbox, { filename: "html-api-base-snippet.js" });
  vm.runInContext(DATA_JS_SRC, sandbox, { filename: "data.js" });
  return { win, root, ready: win.__signalDataReady, events: win.__events, state: win.__state };
}

// A stub adapter that records the loadSnapshot options and returns a sentinel.
function stubAdapter(snapshot, capture) {
  return {
    DEFAULT_API_BASE: "http://localhost:8000",
    async loadSnapshot(options) {
      if (capture) capture.options = options;
      return snapshot;
    },
  };
}

// A fetch that serves the two fixtures and throws for anything else.
function fixtureOnlyFetch(core, eventsFixture) {
  const calls = [];
  const impl = async (url) => {
    calls.push(url);
    if (url.includes("signal-mock-data.json")) return okJson(core);
    if (url.includes("events.json")) return okJson(eventsFixture);
    throw new Error("ECONNREFUSED " + url);
  };
  impl.calls = calls;
  return impl;
}

/* ======================================================================== */
/*  HTML wiring: ordering + classic-script shape                             */
/* ======================================================================== */
test("HTML loads api-adapter.js (classic) before data.js, base snippet in between", () => {
  const adapterIdx = HTML_SRC.indexOf('src="app/api-adapter.js');
  const snippetIdx = HTML_SRC.search(/<script>[\s\S]*?SIGNAL_API_BASE/);
  const dataIdx = HTML_SRC.indexOf('src="app/data.js');
  assert.ok(adapterIdx > -1, "api-adapter.js script tag present");
  assert.ok(dataIdx > -1, "data.js script tag present");
  assert.ok(adapterIdx < snippetIdx, "adapter loads before the base snippet");
  assert.ok(snippetIdx < dataIdx, "base snippet runs before the data loader");

  const adapterTag = HTML_SRC.match(/<script[^>]*app\/api-adapter\.js[^>]*>\s*<\/script>/);
  assert.ok(adapterTag, "adapter script tag is a self-contained src include");
  assert.ok(!/type=/.test(adapterTag[0]), "adapter is a classic script (no type=)");
  const dataTag = HTML_SRC.match(/<script[^>]*app\/data\.js[^>]*>\s*<\/script>/);
  assert.ok(dataTag && !/type=/.test(dataTag[0]), "data.js is a classic script (no type=)");
});

test("base snippet adopts the adapter default when unset and never overwrites a provided base", () => {
  // unset -> default
  const s1 = { window: { SignalApiAdapter: { DEFAULT_API_BASE: "http://localhost:8000" } } };
  vm.createContext(s1);
  vm.runInContext(API_BASE_SNIPPET, s1);
  assert.equal(s1.window.SIGNAL_API_BASE, "http://localhost:8000");

  // caller-provided (with a trailing slash) -> preserved verbatim; the adapter,
  // not the page, is responsible for trailing-slash normalization.
  const s2 = { window: { SIGNAL_API_BASE: "http://api.example.com:9000/", SignalApiAdapter: { DEFAULT_API_BASE: "http://localhost:8000" } } };
  vm.createContext(s2);
  vm.runInContext(API_BASE_SNIPPET, s2);
  assert.equal(s2.window.SIGNAL_API_BASE, "http://api.example.com:9000/");
});

/* ======================================================================== */
/*  Readiness contracts: identity, one assignment, one event, fixtures       */
/* ======================================================================== */
test("publishes all three readiness contracts to the identical snapshot, exactly once", async () => {
  const snapshot = { runtime: { mode: "api" }, marker: "SENTINEL" };
  const fetchImpl = fixtureOnlyFetch({ core: true }, [{ id: "e1" }]);
  const { win, ready, events, state } = runLoader({ adapter: stubAdapter(snapshot), fetchImpl });

  const resolved = await ready;
  // identity across all three contracts (same object reference, not a copy)
  assert.equal(resolved, snapshot);
  assert.equal(win.DATA, snapshot);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, "signal:data-ready");
  assert.equal(events[0].detail, snapshot);
  // exactly one DATA assignment through the ready path
  assert.equal(state.assignments, 1);
  // both fixtures were fetched
  assert.ok(fetchImpl.calls.some((u) => u.includes("signal-mock-data.json")));
  assert.ok(fetchImpl.calls.some((u) => u.includes("events.json")));
});

test("loadSnapshot receives parsed fixtures, api base and a bound fetch; result is passed through unreshaped", async () => {
  const core = { core: true };
  const eventsFixture = [{ id: "e1" }];
  const snapshot = { runtime: { mode: "api" } };
  const capture = {};
  const fetchImpl = fixtureOnlyFetch(core, eventsFixture);
  const { win, ready } = runLoader({ adapter: stubAdapter(snapshot, capture), fetchImpl });

  const resolved = await ready;
  const opts = capture.options;
  assert.equal(opts.fixtures.core, core);          // exact parsed-JSON references
  assert.equal(opts.fixtures.events, eventsFixture);
  assert.equal(opts.apiBase, "http://localhost:8000"); // from the base snippet default
  assert.equal(typeof opts.fetch, "function");     // window.fetch bound
  // pass-through: DATA is the exact object the adapter returned, never reshaped
  assert.equal(resolved, snapshot);
  assert.equal(win.DATA, snapshot);
});

/* ======================================================================== */
/*  Full API-unreachable demo snapshot (real adapter) still resolves         */
/* ======================================================================== */
test("total API-unreachable: the real adapter demo snapshot still resolves and dispatches", async () => {
  const core = { meta: { now: "2026-06-05T08:30:00-07:00" }, dashboard: {}, risk: { riskRadar: [] }, admin: {}, ask: { suggestions: [] } };
  const fetchImpl = fixtureOnlyFetch(core, []); // fixtures served; every API URL throws
  const { win, ready, events, root, state } = runLoader({ adapter: REAL_ADAPTER, fetchImpl });

  const resolved = await ready;
  assert.equal(resolved.runtime.mode, "demo");
  assert.equal(resolved.runtime.degraded, true);
  assert.equal(win.DATA, resolved);
  assert.equal(events.length, 1);
  assert.equal(events[0].detail, resolved);
  assert.equal(state.assignments, 1);
  assert.equal(root.innerHTML, ""); // never hit the fatal boot screen on an API failure
});

test("a degraded (partial) snapshot is dispatched unchanged", async () => {
  const snapshot = { runtime: { mode: "api", degraded: true }, apiErrors: [{ endpoint: "/x" }] };
  const fetchImpl = fixtureOnlyFetch({}, []);
  const { win, ready, events, root, state } = runLoader({ adapter: stubAdapter(snapshot), fetchImpl });

  const resolved = await ready;
  assert.equal(resolved, snapshot);      // identical object, no special-casing
  assert.equal(win.DATA, snapshot);
  assert.equal(events[0].detail, snapshot);
  assert.equal(state.assignments, 1);
  assert.equal(root.innerHTML, "");      // degraded is not fatal
});

/* ======================================================================== */
/*  Fatal boot failures: fixture fetch failure + missing adapter             */
/* ======================================================================== */
test("a fixture fetch failure rejects readiness and renders an HTML-safe fatal screen", async () => {
  const fetchImpl = async (url) => {
    if (url.includes("signal-mock-data.json")) throw new Error("<img src=x onerror=alert(1)>");
    if (url.includes("events.json")) return okJson([]);
    throw new Error("unexpected " + url);
  };
  const { ready, root, state, events } = runLoader({ adapter: stubAdapter({}), fetchImpl });

  await assert.rejects(ready);
  assert.match(root.innerHTML, /Unable to load intelligence data/);
  // the raw exception text is escaped, never injected as live markup
  assert.ok(!root.innerHTML.includes("<img"), "raw error markup must not be injected");
  assert.match(root.innerHTML, /&lt;img/);
  assert.equal(state.assignments, 0); // DATA never assigned on a fatal boot
  assert.equal(events.length, 0);     // and no ready event fires
});

test("a missing adapter is a fatal boot failure (rejects + fatal screen), not a silent mount", async () => {
  const fetchImpl = fixtureOnlyFetch({}, []);
  const { ready, root, state, events } = runLoader({ adapter: undefined, fetchImpl });

  await assert.rejects(ready, /SignalApiAdapter is unavailable/);
  assert.match(root.innerHTML, /Unable to load intelligence data/);
  assert.equal(state.assignments, 0);
  assert.equal(events.length, 0);
});

/* ======================================================================== */
/*  Source boundary: data.js carries no backend/wire knowledge               */
/* ======================================================================== */
test("data.js contains no backend endpoints or wire keys (that boundary belongs to the adapter)", () => {
  const src = DATA_JS_SRC;
  assert.ok(!/\/api\//.test(src), "no /api/ endpoint paths");
  assert.ok(!/api\/v1/.test(src), "no api/v1 prefix");
  assert.ok(!/[a-z]{2,}_[a-z]{2,}/.test(src), "no snake_case wire keys");
  assert.ok(!/company-research|provider-data|risk-radar|daily-brief/.test(src), "no endpoint segments");
  assert.ok(!/X-Request-ID|[?&]limit=|[?&]offset=/.test(src), "no pagination/envelope wire tokens");
  // page-facing fixture filenames and the public adapter surface ARE allowed
  assert.ok(/signal-mock-data\.json/.test(src) && /events\.json/.test(src), "fixture filenames present");
  assert.ok(/window\.SignalApiAdapter/.test(src), "reads the adapter global");
  assert.ok(/loadSnapshot/.test(src), "delegates snapshot assembly to the adapter");
});
