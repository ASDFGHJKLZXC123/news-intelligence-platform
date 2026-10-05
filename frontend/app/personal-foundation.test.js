"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_DIR = __dirname;
const read = (name) => fs.readFileSync(path.join(APP_DIR, name), "utf8");
const SHELL = read("shell.jsx");
const MAIN = read("main.jsx");
const PERSONAL = read("page-personal.jsx");
const WIDGETS = read("widgets.jsx");
const COMPONENTS = read("components.jsx");
const HTML = fs.readFileSync(path.join(APP_DIR, "..", "SIGNAL - Intelligence Platform.html"), "utf8");

function functionSource(source, name) {
  const start = source.indexOf("function " + name + "(");
  assert.ok(start >= 0, name + " found");
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("unbalanced function " + name);
}

test("Today keeps partial workspace and history read failures visible", () => {
  const selectError = vm.runInNewContext("(" + functionSource(PERSONAL, "personalTodayReadError") + ")");
  const workspaceError = new Error("workspace unavailable");
  const eventError = new Error("events unavailable");
  const historyError = new Error("history unavailable");
  assert.equal(selectError([
    { status: "rejected", reason: workspaceError },
    { status: "fulfilled", value: { items: [], displayedRun: null } },
    { status: "fulfilled", value: { items: [] } },
  ]), workspaceError);
  assert.equal(selectError([
    { status: "rejected", reason: workspaceError },
    { status: "rejected", reason: eventError },
    { status: "rejected", reason: historyError },
  ]), eventError);
  assert.equal(selectError([
    { status: "fulfilled", value: {} },
    { status: "fulfilled", value: {} },
    { status: "rejected", reason: historyError },
  ]), historyError);
  assert.equal(selectError([
    { status: "fulfilled", value: {} },
    { status: "fulfilled", value: {} },
    { status: "fulfilled", value: {} },
  ]), null);
});

test("capture coverage shows useful recorded counts without schema or diagnostic arrays", () => {
  const rowsFor = vm.runInNewContext("(" + functionSource(PERSONAL, "personalCoverageRows") + ")", {
    personalDateTime: (value) => "time:" + value,
    personalValue: (value) => value === null || value === undefined ? "Not recorded" : String(value),
  });
  const rows = JSON.parse(JSON.stringify(rowsFor({
    captureStartedAt: "start", captureEndedAt: "end",
    coverage: {
      schema: "personal-capture-coverage.v1", feedsConfigured: 2, feedsAttempted: 2,
      feedsSucceeded: 2, feedsFailed: 0, feedsPaused: 0, feedFailures: [], feedPauses: [],
      itemsFetched: 4, articlesCaptured: 4, articlesAdmitted: 1, pendingTotal: 0, pendingCapacity: 500,
    },
  })));
  assert.deepEqual(rows, [
    ["Capture started", "time:start"], ["Capture ended", "time:end"],
    ["Feeds configured", "2"], ["Feeds succeeded", "2"], ["Feeds failed", "0"],
    ["Feeds paused", "0"], ["Articles admitted", "1"], ["Pending items", "0 of 500 capacity"],
  ]);
  assert.ok(!JSON.stringify(rows).includes("schema"));
  assert.ok(!JSON.stringify(rows).includes("failures"));
});

test("hash routes preserve personal aliases, event ids, and deferred destinations", () => {
  const routeFromHash = vm.runInNewContext("(" + functionSource(SHELL, "routeFromHash") + ")");
  const route = (hash) => JSON.parse(JSON.stringify(routeFromHash(hash)));
  assert.deepEqual(route(""), { name: "today", param: null });
  assert.deepEqual(route("#/dashboard"), { name: "today", param: null });
  assert.deepEqual(route("#/events"), { name: "today", param: null });
  assert.deepEqual(route("#/watchlist"), { name: "saved", param: null });
  assert.deepEqual(route("#/reports"), { name: "briefs", param: null });
  assert.deepEqual(route("#/event/evt%2F123"), { name: "event", param: "evt/123" });
  assert.deepEqual(route("#/event/evt-1?run=run-2"), { name: "event", param: "evt-1", runId: "run-2" });
  assert.deepEqual(route("#/brief/report-3"), { name: "brief", param: "report-3" });
  assert.deepEqual(route("#/risk"), { name: "risk", param: null });
  assert.match(MAIN, /default:\s*return <UnavailablePersonalPage route=\{route\}/);
});

test("personal navigation and command palette advertise only Today, Saved, Briefs, and stories", () => {
  const navBlock = SHELL.slice(SHELL.indexOf("const NAV"), SHELL.indexOf("/* route → nav"));
  assert.match(navBlock, /label: "Today"/);
  assert.match(navBlock, /label: "Saved"/);
  assert.match(navBlock, /label: "Briefs"/);
  for (const hidden of ["Risk Radar", "Ask AI", "Companies", "Industries", "Admin", "Alerts"]) {
    assert.ok(!navBlock.includes(hidden), hidden + " absent from personal navigation");
  }
  const paletteBlock = SHELL.slice(SHELL.indexOf("function CommandPalette"), SHELL.indexOf("/* ---- Toast"));
  assert.ok(!/D\.companies|D\.industries/.test(paletteBlock));
  assert.match(paletteBlock, /PersonalUiCache/);
});

test("evidence drawer renders the retained excerpt as escaped React text and activates only HTTP(S)", () => {
  const drawer = MAIN.slice(MAIN.indexOf("function EvidenceDrawer"), MAIN.indexOf("/* ---- Router"));
  assert.match(drawer, /\{ev\.excerpt\}/);
  assert.match(drawer, /No excerpt available\./);
  assert.ok(!/dangerouslySetInnerHTML/.test(drawer));
  assert.ok(!/representative|Reporting indicates/.test(drawer));
  assert.match(drawer, /SignalApiAdapter\.safeHttpUrl\(ev\.url\)/);
  assert.match(drawer, /href=\{sourceUrl\}/);
  assert.match(drawer, /target="_blank"/);
  assert.match(drawer, /rel="noopener noreferrer"/);
  assert.ok(!/preventDefault/.test(drawer));
  assert.match(drawer, /Source link unavailable/);
  assert.match(drawer, /Demo — sample data/);
  assert.match(drawer, /Array\.isArray\(ev\.relatedClaims\)/);
});

test("demo is persistently labelled and all personal mutations are gated out of demo", () => {
  assert.match(COMPONENTS, /<strong>Demo — sample data<\/strong>/);
  assert.match(PERSONAL, /function TodayPage\(\) \{ return personalIsDemo\(\) \? <DemoTodayPage \/> : <RealTodayPage \/>; \}/);
  assert.match(PERSONAL, /Updates are disabled in the read-only demo/);
  assert.match(PERSONAL, /Saved stories are unavailable in the demo/);
  assert.match(PERSONAL, /Personal briefs are unavailable in the demo/);
  assert.match(PERSONAL, /if \(pending \|\| personalIsDemo\(\)\) return/);
  assert.match(PERSONAL, /if \(mounted\.current && !personalIsDemo\(\)\)/);
  assert.match(WIDGETS, /function WatchBtn[\s\S]*?disabled[\s\S]*?Saving is unavailable in this version/);
  assert.ok(!/Store\.toggleWatch/.test(WIDGETS));
});

test("freshness labels keep publication, display fetch, and processing success distinct", () => {
  assert.match(PERSONAL, /Latest known publication/);
  assert.match(PERSONAL, /Last successful update/);
  assert.match(PERSONAL, /Capture started/);
  assert.match(PERSONAL, /Capture ended/);
  assert.match(PERSONAL, /lastSuccessfulUpdateAt/);
  assert.ok(!/Last processed|First recorded/.test(PERSONAL));
});

test("Today implements server filters, previous-run selection, quiet results, and explicit retry", () => {
  assert.match(PERSONAL, /requestState\.current = \{ selectedRunId, query, savedOnly, offset \}/);
  assert.match(PERSONAL, /PersonalApi\.listEvents\(\{ runId: requested\.selectedRunId \|\| undefined, query: requested\.query, savedOnly: requested\.savedOnly, limit: PERSONAL_PAGE_SIZE, offset: requested\.offset \}\)/);
  assert.match(PERSONAL, /setQuery\(queryDraft\.trim\(\)\)/);
  assert.match(PERSONAL, /setOffset\(0\)/);
  assert.match(PERSONAL, /Latest readable result/);
  assert.match(PERSONAL, /No stories matched this update/);
  assert.match(PERSONAL, /PersonalApi\.retryRun\(retryId\)/);
  assert.match(PERSONAL, /displayedNeedsRetry/);
  assert.match(PERSONAL, /<strong>Already processed\.<\/strong>/);
  assert.match(PERSONAL, /result\.alreadyProcessed/);
  assert.match(PERSONAL, /Request an update to collect RSS articles/);
  assert.match(PERSONAL, /interval: 2000/);
  assert.match(PERSONAL, /runStatusStale/);
});

test("persistent saves use confirmed responses and preserve unavailable pointers", () => {
  assert.match(PERSONAL, /if \(story\.saved\) await PersonalApi\.unsaveEvent/);
  assert.match(PERSONAL, /else await PersonalApi\.saveEvent/);
  assert.match(PERSONAL, /setPage\(\(current\) =>/);
  assert.match(PERSONAL, /else await PersonalApi\.unsaveEntry\(item\.id\)/);
  assert.match(PERSONAL, /Target unavailable/);
  assert.match(PERSONAL, /Legacy saved items/);
  assert.match(PERSONAL, /Read-only · all owners and item types/);
  assert.match(PERSONAL, /PersonalApi\.listSaved\(\{ limit: PERSONAL_PAGE_SIZE, offset: requested\.offset \}\)/);
});

test("detail and briefs keep frozen scope, report identity, citations, and exports explicit", () => {
  assert.match(PERSONAL, /Articles considered in this update/);
  assert.match(PERSONAL, /hasUpdateScope && <Row k="Articles considered in this update"/);
  assert.match(PERSONAL, /No daily update is selected/);
  assert.match(PERSONAL, /Used in this brief/);
  assert.match(PERSONAL, /Additional current coverage/);
  assert.match(PERSONAL, /PersonalApi\.claimEvidence\(reportId, claimId\)/);
  assert.match(PERSONAL, /PersonalApi\.reportExportUrl\(report\.id, "md"\)/);
  assert.match(PERSONAL, /PersonalApi\.reportExportUrl\(report\.id, "pdf"\)/);
  assert.match(PERSONAL, /Failed versions remain visible without replacing published content/);
  assert.ok(!/dangerouslySetInnerHTML/.test(PERSONAL));
});

test("late personal responses are isolated by current filter, route, and mode identity", () => {
  assert.match(PERSONAL, /token !== loadGeneration\.current/);
  assert.match(PERSONAL, /token === generation\.current/);
  assert.match(PERSONAL, /\[id, refreshToken, mode\]/);
  assert.match(MAIN, /key=\{\[[\s\S]*?runtime && window\.DATA\.runtime\.mode,[\s\S]*?st\.route\.param/);
});

test("access key is tab-memory only and setup does not silently configure owner or feeds", () => {
  assert.match(PERSONAL, /type="password"/);
  assert.match(PERSONAL, /stays only in this browser tab/);
  assert.ok(!/localStorage|sessionStorage/.test(PERSONAL));
  assert.match(PERSONAL, /PersonalApi\.setupWorkspace\(\)/);
  assert.match(PERSONAL, /asks you to choose when ownership is ambiguous/);
  assert.match(PERSONAL, /Choose feeds and collection limits in Settings/);
  assert.match(PERSONAL, /Saved preferences · not applied/);
  assert.match(PERSONAL, /PersonalApi\.updateSettings\(personalSettingsBody\(draft\)\)/);
  assert.match(PERSONAL, /execution_route_unavailable: "The configured processing route is unavailable on this server\."/);
});

test("settings payload preserves exact decimal strings and explicit source selection", () => {
  const context = vm.createContext({});
  vm.runInContext(functionSource(PERSONAL, "personalSnakeValues") + "\n" + functionSource(PERSONAL, "personalSettingsBody"), context);
  const body = context.personalSettingsBody({
    selectedSourceIds: ["source-a"], includeText: "  rate policy  \n", excludeText: "",
    aiEnabled: false, monthlyAllowanceUsd: "0.030000000001", runAllowanceUsd: "",
    modelRoute: { mode: "live", embedding: { inputUsdPerMillionTokens: "0.02" } },
  });
  assert.deepEqual(JSON.parse(JSON.stringify(body)), {
    selected_source_ids: ["source-a"], include_phrases: ["rate policy"], exclude_phrases: [],
    ai_enabled: false, monthly_allowance_usd: "0.030000000001", run_allowance_usd: null,
    model_route: { mode: "live", embedding: { input_usd_per_million_tokens: "0.02" } },
  });
});

test("raw reader separates retained RSS, unknown publication, grouping, and admission", () => {
  assert.match(PERSONAL, /Publication time unknown/);
  assert.match(PERSONAL, /Retained RSS article/);
  assert.match(PERSONAL, /ungrouped: true, interestOnly: true/);
  assert.match(PERSONAL, /k="Captured"/);
  assert.match(PERSONAL, /k="Admitted"/);
  assert.match(PERSONAL, /item\.eventId && <button/);
  assert.match(PERSONAL, /Unresolved, all periods/);
  assert.match(PERSONAL, /No ongoing allowance has been supplied/);
});

test("disabled or blocked grouping never presents a healthy quiet update", () => {
  const describe = vm.runInNewContext("(" + functionSource(PERSONAL, "personalGroupingEmptyState") + ")");
  assert.equal(describe({ stageResults: { grouping: { status: "disabled" } } }).title, "Grouping is disabled");
  assert.equal(describe({ stageResults: { grouping: { status: "blocked" } } }).title, "Grouped stories unavailable");
  assert.match(describe({ stageResults: { grouping: { status: "disabled" } } }).hint, /RSS articles remain readable/);
  assert.equal(describe({ stageResults: { grouping: { status: "succeeded" } } }), null);
  assert.equal(describe(null), null);
});

test("failed capture without grouping is explained before healthy quiet results", () => {
  const describe = vm.runInNewContext("(" + functionSource(PERSONAL, "personalGroupingEmptyState") + ")");
  const result = describe({
    state: "failed", error: { code: "all_feeds_failed" },
    counts: { admitted: 1, selectedForEnrichment: 0, grouped: 0 },
    stageResults: { capture: { status: "failed", feedsSucceeded: 0, feedsFailed: 2 } },
  });
  assert.ok(result, "a failed capture must not fall through to a healthy quiet result");
  assert.equal(result.title, "Grouped stories unavailable");
  assert.match(result.hint, /update failed/i);
  assert.match(result.hint, /retained RSS articles remain readable/i);
  assert.equal(result.failed, true);
});

test("failed provider before grouping is explained without hiding retained raw articles", () => {
  const describe = vm.runInNewContext("(" + functionSource(PERSONAL, "personalGroupingEmptyState") + ")");
  const result = describe({
    state: "failed", error: { code: "personal_workflow_failed" },
    counts: { admitted: 9, selectedForEnrichment: 9, grouped: 0 },
    stageResults: { capture: { status: "succeeded" }, workflow: { status: "failed", code: "personal_workflow_failed" } },
  });
  assert.ok(result, "a provider failure must not fall through to a healthy quiet result");
  assert.equal(result.title, "Grouped stories unavailable");
  assert.match(result.hint, /update failed/i);
  assert.equal(result.failed, true);
  assert.equal(describe({ state: "succeeded", stageResults: { grouping: { status: "succeeded" } } }), null);
  assert.equal(describe({ state: "succeeded", stageResults: {} }), null);
});

test("partial failure stays truthful while disabled and successful grouping keep their states", () => {
  const describe = vm.runInNewContext("(" + functionSource(PERSONAL, "personalGroupingEmptyState") + ")");
  const partial = describe({ state: "partially_failed", stageResults: { grouping: { status: "succeeded" } } });
  assert.equal(partial.title, "Grouped stories unavailable");
  assert.match(partial.hint, /did not finish cleanly/);
  assert.equal(partial.failed, true);
  assert.equal(describe({ state: "partially_failed", stageResults: { grouping: { status: "disabled" } } }).title, "Grouping is disabled");
  assert.equal(describe({ state: "succeeded", stageResults: { grouping: { status: "succeeded" } } }), null);
  assert.match(PERSONAL, /groupingEmptyState\.failed \? Icon\.warn : Icon\.info/);
  assert.match(PERSONAL, /title="No stories match these filters"/);
});

test("personal page loads before the router and legacy prototype files remain included", () => {
  const personalIndex = HTML.indexOf('src="app/page-personal.jsx');
  const mainIndex = HTML.indexOf('src="app/main.jsx');
  assert.ok(personalIndex > -1 && personalIndex < mainIndex);
  for (const prototype of ["page-dashboard.jsx", "page-risk.jsx", "page-entities.jsx", "page-alerts-admin.jsx", "page-misc.jsx"]) {
    assert.ok(HTML.includes(prototype), prototype + " preserved");
  }
});

test("URL API override accepts explicit credential-free loopback only", () => {
  const marker = "// A disposable local harness may use a random port.";
  const start = HTML.indexOf(marker);
  const end = HTML.indexOf("</script>", start);
  assert.ok(start > -1 && end > start);
  const bootstrap = HTML.slice(start, end);
  const run = (api) => {
    const location = new URL("http://127.0.0.1:3000/page?api=" + encodeURIComponent(api));
    const context = { window: { location }, URL, URLSearchParams };
    vm.runInNewContext(bootstrap, context);
    return context.window.SIGNAL_API_BASE;
  };
  assert.equal(run("http://127.0.0.1:49123/path"), "http://127.0.0.1:49123");
  assert.equal(run("http://localhost:49124"), "http://localhost:49124");
  assert.equal(run("https://127.0.0.1:49123"), undefined);
  assert.equal(run("http://example.com:49123"), undefined);
  assert.equal(run("http://user:secret@127.0.0.1:49123"), undefined);
});
