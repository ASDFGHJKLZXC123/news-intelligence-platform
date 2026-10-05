"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_DIR = __dirname;
const DATA_JS_SRC = fs.readFileSync(path.join(APP_DIR, "data.js"), "utf8");
const HTML_SRC = fs.readFileSync(path.join(APP_DIR, "..", "SIGNAL - Intelligence Platform.html"), "utf8");
const REAL_ADAPTER = require(path.join(APP_DIR, "api-adapter.js"));

class FakeCustomEvent {
  constructor(type, init) { this.type = type; this.detail = init && init.detail; }
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function emptySnapshot(mode, extra) {
  return REAL_ADAPTER.buildEmptySnapshot(Object.assign({ mode, apiBase: "http://localhost:8000", now: "2026-06-05T16:00:00Z" }, extra || {}));
}

function realSnapshot(id, extraRuntime) {
  const snapshot = emptySnapshot("real");
  snapshot.events = [{ id, title: id }];
  snapshot.eventsById = { [id]: snapshot.events[0] };
  snapshot.runtime = Object.assign({}, snapshot.runtime, {
    mode: "real", degraded: false, requestFailed: false,
    lastSuccessfulFetchAt: "2026-06-05T12:00:00-04:00",
  }, extraRuntime || {});
  return snapshot;
}

function okJson(body) { return { ok: true, status: 200, json: async () => body }; }

function makeWindow(cfg) {
  const listeners = {};
  const events = [];
  const assignments = [];
  const win = {
    SignalApiAdapter: cfg.adapter,
    SIGNAL_API_BASE: cfg.apiBase || "http://localhost:8000",
    fetch: cfg.fetchImpl || (async () => { throw new Error("unexpected fetch"); }),
    AbortController,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatchEvent(event) {
      events.push(event);
      (listeners[event.type] || []).forEach((fn) => fn(event));
      return true;
    },
  };
  Object.defineProperty(win, "DATA", {
    get() { return assignments[assignments.length - 1]; },
    set(value) { assignments.push(value); },
  });
  win.__events = events;
  win.__assignments = assignments;
  return win;
}

function runLoader(cfg) {
  const win = makeWindow(cfg);
  const sandbox = { window: win, CustomEvent: FakeCustomEvent, Promise, Date, URL, AbortController, console };
  vm.createContext(sandbox);
  vm.runInContext(DATA_JS_SRC, sandbox, { filename: "data.js" });
  return { win, ready: win.__signalDataReady, controller: win.SignalDataController };
}

function adapterStub(loadSnapshot) {
  return {
    loadSnapshot,
    buildEmptySnapshot: REAL_ADAPTER.buildEmptySnapshot,
    buildFixtureSnapshot: REAL_ADAPTER.buildFixtureSnapshot,
  };
}

test("HTML keeps the classic adapter before the real-first data controller", () => {
  const adapterIdx = HTML_SRC.indexOf('src="app/api-adapter.js');
  const dataIdx = HTML_SRC.indexOf('src="app/data.js');
  assert.ok(adapterIdx > -1 && dataIdx > adapterIdx);
  assert.ok(!/type=/.test(HTML_SRC.match(/<script[^>]*app\/api-adapter\.js[^>]*>\s*<\/script>/)[0]));
  assert.ok(!/type=/.test(HTML_SRC.match(/<script[^>]*app\/data\.js[^>]*>\s*<\/script>/)[0]));
});

test("default boot is real and never requests fixture files", async () => {
  const capture = { options: null, fetches: [] };
  const expected = realSnapshot("real-event");
  const fetchImpl = async (url) => { capture.fetches.push(url); throw new Error("unexpected"); };
  const { win, ready } = runLoader({
    fetchImpl,
    adapter: adapterStub(async (options) => { capture.options = options; return expected; }),
  });
  const resolved = await ready;
  assert.equal(resolved, expected);
  assert.equal(win.DATA, expected);
  assert.equal(capture.options.mode, "real");
  assert.ok(!("fixtures" in capture.options));
  assert.deepEqual(capture.fetches, []);
  assert.equal(win.__events.filter((e) => e.type === "signal:data-ready").length, 1);
});

test("unreachable API publishes a real error and no sample content", async () => {
  const fetchImpl = async () => { throw new Error("ECONNREFUSED"); };
  const { win, ready } = runLoader({ adapter: REAL_ADAPTER, fetchImpl });
  const snapshot = await ready;
  assert.equal(snapshot.runtime.mode, "real");
  assert.equal(snapshot.runtime.requestFailed, true);
  assert.equal(snapshot.runtime.lastSuccessfulFetchAt, null);
  assert.deepEqual(snapshot.events, []);
  assert.deepEqual(snapshot.evidence, []);
  assert.equal(win.DATA, snapshot);
});

test("a degraded refresh preserves prior data and its successful fetch time", async () => {
  const good = realSnapshot("kept-event");
  const degraded = realSnapshot("replacement-event", { degraded: true, lastSuccessfulFetchAt: "2026-06-05T13:00:00-04:00" });
  degraded.apiErrors = [{ endpoint: "/x", code: "http_503", message: "unavailable", requestId: "req-x" }];
  let call = 0;
  const { win, ready, controller } = runLoader({ adapter: adapterStub(async () => (++call === 1 ? good : degraded)) });
  await ready;
  const refreshed = await controller.refresh();
  assert.equal(refreshed.events[0].id, "kept-event");
  assert.equal(refreshed.runtime.stale, true);
  assert.equal(refreshed.runtime.requestFailed, true);
  assert.equal(refreshed.runtime.lastSuccessfulFetchAt, good.runtime.lastSuccessfulFetchAt);
  assert.equal(refreshed.apiErrors[0].requestId, "req-x");
  assert.equal(win.DATA, refreshed);
});

test("pending real response cannot populate an explicitly selected demo", async () => {
  const lateReal = deferred();
  const fixtureFetches = [];
  const fetchImpl = async (url) => {
    fixtureFetches.push(url);
    if (url.includes("signal-mock-data.json")) return okJson({ meta: { now: "2026-06-05T08:00:00-07:00" }, dashboard: {}, risk: {}, admin: {} });
    if (url.includes("events.json")) return okJson([{ id: "demo-event", title: "sample" }]);
    throw new Error("unexpected " + url);
  };
  const { win, ready, controller } = runLoader({ adapter: adapterStub(() => lateReal.promise), fetchImpl });
  const demo = await controller.setMode("demo");
  assert.equal(demo.runtime.mode, "demo");
  assert.equal(demo.events[0].id, "demo-event");
  lateReal.resolve(realSnapshot("late-real"));
  assert.equal(await ready, null);
  await Promise.resolve();
  assert.equal(win.DATA.runtime.mode, "demo");
  assert.ok(!win.DATA.eventsById["late-real"]);
  assert.equal(fixtureFetches.length, 2);
});

test("pending demo response cannot populate real after a mode switch", async () => {
  const core = deferred();
  const events = deferred();
  const firstReal = realSnapshot("first-real");
  const secondReal = realSnapshot("second-real");
  let realCalls = 0;
  const fetchImpl = (url) => url.includes("signal-mock-data.json") ? core.promise : events.promise;
  const { win, ready, controller } = runLoader({
    fetchImpl,
    adapter: adapterStub(async () => (++realCalls === 1 ? firstReal : secondReal)),
  });
  await ready;
  const demoWork = controller.setMode("demo");
  assert.equal(win.DATA.runtime.mode, "demo");
  assert.equal(win.DATA.runtime.loading, true);
  const realWork = controller.setMode("real");
  assert.equal(win.DATA.runtime.mode, "real");
  assert.equal(win.DATA.runtime.loading, true);
  core.resolve(okJson({ meta: { now: "2026-06-05T08:00:00-07:00" }, dashboard: {}, risk: {}, admin: {} }));
  events.resolve(okJson([{ id: "late-demo", title: "sample" }]));
  await demoWork;
  const finalReal = await realWork;
  assert.equal(finalReal.events[0].id, "second-real");
  assert.equal(win.DATA.runtime.mode, "real");
  assert.ok(!win.DATA.eventsById["late-demo"]);
});

test("mode changes clear displayed collections before the next mode resolves", async () => {
  const fixture = deferred();
  const { win, ready, controller } = runLoader({
    adapter: adapterStub(async () => realSnapshot("real-event")),
    fetchImpl: async (url) => { await fixture.promise; return okJson(url.includes("events.json") ? [] : {}); },
  });
  await ready;
  const switching = controller.setMode("demo");
  assert.equal(win.DATA.runtime.mode, "demo");
  assert.equal(win.DATA.runtime.loading, true);
  assert.deepEqual(Array.from(win.DATA.events), []);
  fixture.resolve();
  await switching;
});

test("the loader owns no backend routes or wire-shape keys", () => {
  assert.ok(!/\/api\//.test(DATA_JS_SRC));
  assert.ok(!/api\/v1/.test(DATA_JS_SRC));
  assert.ok(!/[a-z]{2,}_[a-z]{2,}/.test(DATA_JS_SRC));
  assert.ok(/signal-mock-data\.json/.test(DATA_JS_SRC));
  assert.ok(/events\.json/.test(DATA_JS_SRC));
});
