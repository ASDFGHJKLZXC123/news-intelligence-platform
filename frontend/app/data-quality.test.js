/* ============================================================================
   SIGNAL — data-quality helpers + item-7 UI wiring tests (Stage 7, item 7)

   Zero-build: plain `node --test`, Node built-ins only. No package.json / npm /
   dependency / build artifact. Run from the repo root:

       node --test frontend/app/data-quality.test.js

   Two halves:
     1. The pure helpers in app/data-quality.js are exercised directly (quality
        resolution/aggregation, demo vs degraded, request-id collection, and the
        null-safe value/horizon formatters).
     2. Source/structure assertions enforce the item-7 UI contract without a DOM:
        the helper module is wired into the HTML before the JSX that consumes it;
        every route that renders a top-level block has a badge path; the global
        demo/degraded surfaces are mounted and gated correctly; request IDs are
        surfaced under a human label; live blocks stay quiet; the new surfaces
        carry accessible semantics; and no endpoint/wire key leaks into the UI.
   ========================================================================== */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const APP_DIR = __dirname;
const DQ = require(path.join(APP_DIR, "data-quality.js"));
const HTML_PATH = path.join(APP_DIR, "..", "SIGNAL - Intelligence Platform.html");
const read = (rel) => fs.readFileSync(path.join(APP_DIR, rel), "utf8");
const HTML_SRC = fs.readFileSync(HTML_PATH, "utf8");
const COMPONENTS_SRC = read("components.jsx");
const MAIN_SRC = read("main.jsx");
const DQ_SRC = read("data-quality.js");
const CSS_SRC = read("styles.css");

// Balanced-brace slice of a media block's body — lets the CSS regression assertions
// scope to the narrow layout without a DOM, build step, or CSS parser dependency.
function mediaBody(css, query) {
  const start = css.indexOf(query);
  if (start < 0) return "";
  const open = css.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}" && --depth === 0) return css.slice(open + 1, i);
  }
  return "";
}

/* ======================================================================== */
/*  1. Pure helpers                                                          */
/* ======================================================================== */
test("qualityMeta: live/unknown -> null (no badge noise); non-live -> labelled", () => {
  assert.equal(DQ.qualityMeta("live"), null);
  assert.equal(DQ.qualityMeta(""), null);
  assert.equal(DQ.qualityMeta(undefined), null);
  assert.equal(DQ.qualityMeta("partial").label, "Partial");
  assert.equal(DQ.qualityMeta("synthetic").label, "Sample");
  assert.equal(DQ.qualityMeta("empty").label, "No data");
  // every non-live meta carries a human title for the tooltip/aria description
  ["partial", "synthetic", "empty"].forEach((q) => assert.ok(DQ.qualityMeta(q).title.length > 10));
});

test("isNonLive only flags partial/synthetic/empty", () => {
  assert.equal(DQ.isNonLive("partial"), true);
  assert.equal(DQ.isNonLive("synthetic"), true);
  assert.equal(DQ.isNonLive("empty"), true);
  assert.equal(DQ.isNonLive("live"), false);
  assert.equal(DQ.isNonLive(""), false);
});

test("blockQuality reads the registry, then the block's dataQuality tag, else ''", () => {
  const data = { blockQuality: { metrics: { quality: "live" }, riskRadar: { quality: "partial" } }, metrics: [], riskRadar: [] };
  assert.equal(DQ.blockQuality(data, "metrics"), "live");
  assert.equal(DQ.blockQuality(data, "riskRadar"), "partial");
  // no registry entry -> fall back to the non-enumerable dataQuality tag
  const tagged = [];
  Object.defineProperty(tagged, "dataQuality", { value: "synthetic", enumerable: false });
  assert.equal(DQ.blockQuality({ blockQuality: {}, events: tagged }, "events"), "synthetic");
  // unknown -> "" (treated as live, no badge)
  assert.equal(DQ.blockQuality({ blockQuality: {} }, "nope"), "");
  assert.equal(DQ.blockQuality(null, "x"), "");
});

test("aggregateQuality picks the most-concerning non-live block (empty > partial > synthetic)", () => {
  const data = { blockQuality: { a: { quality: "synthetic" }, b: { quality: "partial" }, c: { quality: "empty" }, d: { quality: "live" } } };
  assert.equal(DQ.aggregateQuality(data, ["a", "b"]), "partial");
  assert.equal(DQ.aggregateQuality(data, ["a", "b", "c"]), "empty");
  assert.equal(DQ.aggregateQuality(data, ["a"]), "synthetic");
  assert.equal(DQ.aggregateQuality(data, ["d"]), ""); // all-live -> quiet
  assert.equal(DQ.aggregateQuality(data, []), "");
});

test("isDemo / isDegraded gate the two global surfaces (never both)", () => {
  const demo = { runtime: { mode: "demo", degraded: true }, apiErrors: [] };
  assert.equal(DQ.isDemo(demo), true);
  assert.equal(DQ.isDegraded(demo), false); // demo shows the demo banner, not the degraded notice

  const partial = { runtime: { mode: "api", degraded: true }, apiErrors: [{ endpoint: "/x", code: "http_500", message: "boom", requestId: null }] };
  assert.equal(DQ.isDemo(partial), false);
  assert.equal(DQ.isDegraded(partial), true);

  const healthy = { runtime: { mode: "api", degraded: false }, apiErrors: [] };
  assert.equal(DQ.isDegraded(healthy), false);
  // an error present but degraded flag false still counts as degraded
  assert.equal(DQ.isDegraded({ runtime: { mode: "api", degraded: false }, apiErrors: [{ message: "x" }] }), true);
});

test("requestIds returns only distinct, non-null ids (never fabricated)", () => {
  const data = { apiErrors: [
    { requestId: "req-1" }, { requestId: null }, { requestId: "req-1" }, { requestId: "req-2" }, {},
  ] };
  assert.deepEqual(DQ.requestIds(data), ["req-1", "req-2"]);
  assert.deepEqual(DQ.requestIds({ apiErrors: [] }), []);
});

test("errorSummaries dedupes, caps, keeps request ids, and never exposes endpoints", () => {
  const errs = [];
  for (let i = 0; i < 20; i++) errs.push({ endpoint: "/api/v1/dashboard", code: "http_500", message: "same failure", requestId: "r" + i });
  errs.push({ endpoint: "/api/v1/events", code: "network_error", message: "network error", requestId: null });
  const out = DQ.errorSummaries({ apiErrors: errs });
  assert.ok(out.length <= 8, "capped");
  assert.equal(out[0].message, "same failure");
  assert.equal(out[0].requestId, "r0"); // first id for the deduped group
  assert.equal(out[1].message, "network error");
  out.forEach((e) => assert.ok(!("endpoint" in e), "endpoint is never handed to the UI layer"));
});

test("value formatters: null/absent -> dash, never a fabricated zero", () => {
  assert.equal(DQ.numOr(null), "—");
  assert.equal(DQ.numOr(undefined), "—");
  assert.equal(DQ.numOr(0), 0);           // a real zero is preserved
  assert.equal(DQ.numOr(42), 42);
  assert.equal(DQ.intOr(null), "—");
  assert.equal(DQ.intOr(1234), "1,234");
  assert.equal(DQ.moneyOr(null, 4), "—");
  assert.equal(DQ.moneyOr(0.1234, 4), "$0.1234");
  assert.equal(DQ.unitOr(null, "ms"), "—");
  assert.equal(DQ.unitOr(42, "ms"), "42ms");
  assert.equal(DQ.pctOr(null), "—");
  assert.equal(DQ.pctOr(99.6), "100%");
  assert.equal(DQ.numOr(NaN), "—");       // NaN is not a real value
});

test("horizonLabel maps canonical tokens, passes friendly labels through, dashes null", () => {
  assert.equal(DQ.horizonLabel("0_6m"), "0–6m");
  assert.equal(DQ.horizonLabel("within_18m"), "≤18m");
  assert.equal(DQ.horizonLabel("6 months"), "6 months"); // already friendly -> unchanged
  assert.equal(DQ.horizonLabel(null), "—");
});

test("module exports both a browser global and CommonJS (adapter pattern)", () => {
  assert.equal(typeof DQ.qualityMeta, "function");
  assert.deepEqual(Object.keys(DQ.QUALITY).sort(), ["EMPTY", "LIVE", "PARTIAL", "SYNTHETIC"]);
});

/* ======================================================================== */
/*  2. HTML wiring + UI-source contract                                      */
/* ======================================================================== */
test("HTML loads data-quality.js as a classic script before the JSX that consumes it", () => {
  const dqIdx = HTML_SRC.indexOf('src="app/data-quality.js');
  const compIdx = HTML_SRC.indexOf('src="app/components.jsx');
  const mainIdx = HTML_SRC.indexOf('src="app/main.jsx');
  assert.ok(dqIdx > -1, "data-quality.js script tag present");
  assert.ok(dqIdx < compIdx && dqIdx < mainIdx, "loads before the components/pages that read window.SignalDataQuality");
  const tag = HTML_SRC.match(/<script[^>]*app\/data-quality\.js[^>]*>\s*<\/script>/);
  assert.ok(tag && !/type=/.test(tag[0]), "data-quality.js is a classic script (no type=)");
});

test("the global demo/degraded surface is mounted once and gated by mode", () => {
  assert.match(MAIN_SRC, /<GlobalDataNotices\s*\/>/, "mounted inside the app shell");
  // exactly one mount point
  assert.equal((MAIN_SRC.match(/<GlobalDataNotices/g) || []).length, 1);
  // demo banner only for demo mode; degraded notice only for reachable-but-partial
  assert.match(COMPONENTS_SRC, /DQ\.isDemo\(D\)\)\s*return\s*<DemoBanner/);
  assert.match(COMPONENTS_SRC, /DQ\.isDegraded\(D\)\)\s*return\s*<DegradedNotice/);
});

test("badge renders nothing for a live block (live stays quiet, no badge noise)", () => {
  // the guard lives in DataQualityBadge; qualityMeta already proves live -> null
  assert.match(COMPONENTS_SRC, /function DataQualityBadge[\s\S]*?if\s*\(!meta\)\s*return null/);
});

test("degraded notice surfaces request IDs under a human label, not a wire key", () => {
  assert.match(COMPONENTS_SRC, /Request ID/); // human-facing label
  assert.match(COMPONENTS_SRC, /DQ\.requestIds\(D\)/);
  assert.ok(!/request_id/.test(COMPONENTS_SRC), "no snake_case wire key in the UI");
  assert.ok(!/X-Request-ID/.test(COMPONENTS_SRC), "no request-id header name in the UI");
});

test("the new surfaces carry accessible semantics", () => {
  assert.match(COMPONENTS_SRC, /role="note"/);          // the badge
  assert.match(COMPONENTS_SRC, /aria-label=/);
  assert.match(COMPONENTS_SRC, /demo-banner[\s\S]*?role="status"/);
  assert.match(COMPONENTS_SRC, /degraded-notice[\s\S]*?role="status"/);
  assert.match(COMPONENTS_SRC, /aria-expanded=\{open\}/); // details toggle
});

test("every route that renders a top-level block has a visible badge path", () => {
  // route file -> the badge component it must reference (PageStatus/BlockBadge/DataQualityBadge)
  const pages = [
    "page-dashboard.jsx", "page-events.jsx", "page-risk.jsx", "page-entities.jsx",
    "page-alerts-admin.jsx", "page-misc.jsx", "page-event-detail.jsx", "page-geo-intel.jsx",
  ];
  const badge = /PageStatus|BlockBadge|DataQualityBadge/;
  pages.forEach((p) => assert.match(read(p), badge, p + " must expose a data-quality badge"));
  // the dashboard's aggregate row must actually enumerate its blocks
  assert.match(read("page-dashboard.jsx"), /blocks=\{\[[^\]]*"dailySummary"[^\]]*"companies"[^\]]*\]\}/);
  // synthetic-only surfaces are explicitly marked
  assert.match(read("page-misc.jsx"), /quality="synthetic"/);
  assert.match(read("page-event-detail.jsx"), /quality=\{e\.dataQuality\}/);
});

test("dashboard Risk Level card never fabricates a model rating (Stage 7 regression)", () => {
  const DASH_SRC = read("page-dashboard.jsx");

  // 1. No fabricated composite score survives: the removed `risk_score: 72` — and any
  //    numeric risk_score literal — must be gone. A composite score may ONLY come from a
  //    real CrisisRating (live modelRating or a fixture-supplied one), never a constant.
  assert.ok(!/risk_score\s*:\s*\d/.test(DASH_SRC),
    "no hardcoded numeric risk_score fallback in the dashboard");

  // 2. The composite rating is the real modelRating or null — never a synthesized object.
  assert.match(DASH_SRC, /overallRating\s*=\s*ds\.modelRating\s*\|\|\s*null/,
    "overallRating falls back to null, not a fabricated rating object");
  // overallScore is a real number only when a rating exists (no Math.round on a fake score).
  assert.match(DASH_SRC, /overallScore\s*=\s*overallRating\s*\?\s*Math\.round\(overallRating\.risk_score\)\s*:\s*null/,
    "overallScore is null when the rating is absent");

  // 3. Scope to the Risk Level card body: the gauge/badge/probability are gated behind a
  //    truthy rating, and the absent branch renders an honest EmptyState — never Gauge/
  //    RiskBadge/pct with fabricated or null inputs.
  const cardStart = DASH_SRC.indexOf('eyebrow="Model Rating"');
  const cardEnd = DASH_SRC.indexOf("{/* Metrics */}", cardStart);
  assert.ok(cardStart > -1 && cardEnd > cardStart, "Risk Level card body located");
  const card = DASH_SRC.slice(cardStart, cardEnd);

  assert.match(card, /EmptyState/, "absent branch renders an honest unavailable state");
  const guardIdx = card.search(/overallRating\s*\?/);
  const gaugeIdx = card.indexOf("<Gauge");
  const badgeIdx = card.indexOf("<RiskBadge");
  const emptyIdx = card.indexOf("EmptyState");
  assert.ok(guardIdx > -1, "the rating guard is present in the card");
  assert.ok(guardIdx < gaugeIdx && guardIdx < badgeIdx,
    "Gauge and RiskBadge only render inside the truthy-rating branch");
  assert.ok(guardIdx < emptyIdx && gaugeIdx < emptyIdx,
    "EmptyState is the alternative (absent) branch, after the real render");
});

test("narrow layout: page-head actions row is scoped to a sibling div, so a sole .titles stays stacked (Stage 7 browser regression)", () => {
  const narrow = mediaBody(CSS_SRC, "@media (max-width: 980px)");
  assert.ok(narrow.length > 0, "the narrow (max-width: 980px) media block is present");

  // The old broad selector matched a sole `.titles` (it is also the last child),
  // forcing eyebrow + h1 into one horizontal run at 390px on Events/Companies/
  // Historical Analogies/Ask AI/Settings. It must be gone from the narrow block.
  assert.ok(!/\.page-head\s*>\s*div:last-child/.test(narrow),
    "the broad `> div:last-child` selector (which mis-matched a sole .titles) is removed");

  // The replacement only lays out an actions div *immediately following* .titles.
  assert.match(narrow, /\.page-head\s*>\s*\.titles\s*\+\s*div\s*\{[^}]*display:\s*flex/,
    "actions row is scoped to a `.titles + div` sibling");

  // Selector semantics, no DOM needed: a sole `.titles` has no following div sibling,
  // so `.titles + div` cannot match it, and nothing else in the narrow block sets
  // `display: flex` on `.titles` itself — eyebrow + h1 keep normal block flow (stacked).
  // ([^,{+]* stops before the `+` combinator so the sibling rule above is excluded.)
  assert.ok(!/\.titles[^,{+]*\{[^}]*display:\s*flex/.test(narrow),
    "no narrow rule forces `.titles` itself into a flex row");
  assert.match(narrow, /\.page-head\s*\{[^}]*flex-direction:\s*column/,
    "page-head still stacks its children in the narrow layout");
});

test("data-quality.js carries no backend endpoints or wire keys (that boundary is the adapter's)", () => {
  assert.ok(!/\/api\//.test(DQ_SRC), "no /api/ endpoint paths");
  assert.ok(!/api\/v1/.test(DQ_SRC), "no api/v1 prefix");
  assert.ok(!/[a-z]{2,}_[a-z]{2,}/.test(DQ_SRC), "no snake_case wire keys");
  assert.ok(!/X-Request-ID|[?&]limit=|[?&]offset=/.test(DQ_SRC), "no pagination/envelope wire tokens");
  assert.ok(!/company-research|provider-data|risk-radar|daily-brief/.test(DQ_SRC), "no endpoint segments");
  // it DOES read the page-facing adapter metadata surface
  assert.ok(/blockQuality/.test(DQ_SRC) && /apiErrors/.test(DQ_SRC) && /requestId/.test(DQ_SRC), "reads page-facing metadata");
});
