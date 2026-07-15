/* ============================================================================
   SIGNAL — api-adapter tests (Stage 7, item 6A)

   Zero-build: plain `node --test`, Node built-ins only. No package.json / npm /
   dependency / build artifact. Run from the repo root:

       node --test frontend/app/api-adapter.test.js

   Covers: every major mapper + field-presence/safe defaults; mapping-table
   alignment with types.ts; New York DST conversion; alert statuses; real-history
   deltas; pagination / partial successes; request_id capture; evidence adaptation;
   full API-unreachable fixture snapshot; no synthetic trend in a reachable but
   degraded state; the required literal SYNTHETIC tags; independent company-research
   batching with mismatched list lengths; and the CommonJS/browser export contract.
   No network is used — a deterministic in-memory fake `fetch` drives every case.
   ========================================================================== */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ADAPTER_PATH = path.join(__dirname, "api-adapter.js");
const TYPES_PATH = path.join(__dirname, "types.ts");
const A = require(ADAPTER_PATH);

/* ---- Fake Response / fetch ------------------------------------------------ */
function makeResponse(status, body, headers) {
  const hdr = headers || {};
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (name in hdr ? hdr[name] : (hdr[name.toLowerCase()] || null)) },
    json: async () => {
      if (body === undefined) throw new Error("no json body");
      return body;
    },
  };
}

function page(items, total, limit, offset) {
  return { items, total, limit, offset };
}

// A configurable in-memory backend. `overrides` maps a pathname to a handler
// (url) => Response | "throw" (network error).
function makeServer(overrides) {
  overrides = overrides || {};
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(url);
    const u = new URL(url);
    const p = u.pathname;
    if (overrides[p] !== undefined) {
      const h = overrides[p];
      if (h === "throw") throw new Error("ECONNREFUSED");
      const r = typeof h === "function" ? h(u) : h;
      if (r === "throw") throw new Error("ECONNREFUSED");
      return r;
    }
    return defaultHandler(p, u);
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

function offsetOf(u) { return parseInt(u.searchParams.get("offset") || "0", 10); }
function limitOf(u) { return parseInt(u.searchParams.get("limit") || "100", 10); }

function pagedSlice(all, u) {
  const off = offsetOf(u), lim = limitOf(u);
  return page(all.slice(off, off + lim), all.length, lim, off);
}

/* ---- Default backend fixtures (wire shapes) ------------------------------- */
const WIRE = {
  dashboard: {
    summary: {
      id: "s1", summary_date: "2026-06-05", overall_risk_level: "high", confidence_score: 0.71,
      summary: "Markets opened tighter.", key_points: ["a", "b"],
      model_rating_prediction_id: "pred-1", generated_by_run_id: "run-1", created_at: "2026-06-05T15:30:00Z",
    },
    metrics: [
      { label: "Events Today", value: 42 },
      { label: "Overall Risk", value: "high", severity: "high" },
    ],
    upcoming_triggers: [
      { id: "ut1", title: "Labor data", expected_at: "2026-06-06T12:30:00Z", related_event_id: "e-uuid-1", related_company_id: null, importance: "high", reason: "Key input." },
    ],
    event_map: [
      { location_name: "Washington", latitude: 38.9, longitude: -77, x: 0.27, y: 0.4, event_count: 6, max_risk_score: 76, dominant_event_type: "Policy", related_event_ids: ["e-uuid-1"] },
    ],
    company_ranking: [
      { company_id: "c-nvda", name: "NVIDIA", ticker: "NVDA", exchange: "NASDAQ", industry: "Semiconductors", country: "US", impact_direction: null, impact_score: 82, risk_score: 76, related_event_count: 6, top_driver: "Export-control concerns", confidence_score: 0.7, last_updated_at: "2026-06-05T15:10:00Z" },
    ],
    industry_summary: [],
    risk_scores: [],
    alerts: { open_count: 3 },
  },
  events: [
    {
      id: "e-uuid-1", title: "Semi export risk", summary: "New reports.", event_type: "Policy · Technology",
      hotness_score: 88, risk_score: 76, risk_level: "high", confidence_score: 0.72, status: "developing",
      first_seen_at: "2026-06-05T13:10:00Z", last_seen_at: "2026-06-05T15:20:00Z", updated_at: "2026-06-05T15:20:00Z",
      companies: [{ company_id: "c-nvda", display_name: "NVIDIA", primary_ticker: "NVDA", exchange: "NASDAQ", industry: "Semiconductors", impact_direction: "negative", impact_score: 82, risk_score: 76, confidence_score: 0.72, exposure_explanation: "x" }],
      industries: [{ industry_id: "semis", impact_direction: "negative", impact_score: 84, risk_score: 78, opportunity_score: 14 }],
      locations: [{ id: "l1", event_id: "e-uuid-1", location_name: "Washington", country_code: "US", region: null, latitude: 38.9, longitude: -77, location_type: "hq", confidence_score: 0.9, created_at: "2026-06-05T13:10:00Z" }],
    },
    // an unscored live event -> exercises safe defaults (null risk_level/scores)
    {
      id: "e-uuid-2", title: "Unscored", summary: "", event_type: null,
      hotness_score: null, risk_score: null, risk_level: null, confidence_score: null, status: "new",
      first_seen_at: "2026-06-05T10:00:00Z", last_seen_at: null, updated_at: "2026-06-05T10:00:00Z",
      companies: [], industries: [], locations: [],
    },
  ],
  riskRadar: [
    { id: "r1", target_type: "global_posture", target_id: "G", risk_type: "geopolitical_supply_chain", score: 78, level: "high", confidence_score: 0.7, as_of: "2026-06-05T00:00:00Z", model_version: "v1", evidence_refs: [], driver_refs: [] },
    { id: "r2", target_type: "global_posture", target_id: "G", risk_type: "banking", score: 41, level: "medium", confidence_score: 0.59, as_of: "2026-06-05T00:00:00Z", model_version: "v1", evidence_refs: [], driver_refs: [] },
    // duplicate risk_type observation (older) -> must dedupe to the latest
    { id: "r3", target_type: "industry", target_id: "semis", risk_type: "geopolitical_supply_chain", score: 60, level: "medium", confidence_score: 0.6, as_of: "2026-06-01T00:00:00Z", model_version: "v1", evidence_refs: [], driver_refs: [] },
  ],
  riskDetail: {
    geopolitical_supply_chain: {
      risk_type: "geopolitical_supply_chain", score: 78, severity: "high", confidence_score: 0.7, as_of: "2026-06-05T00:00:00Z",
      model_version: "v1", target_type: "global_posture", target_id: "G", model_rating: { target_type: "global_posture", target_id: "G", risk_type: "geopolitical_supply_chain", risk_score: 78, risk_level: "high" },
      probability_by_horizon: [{ horizon: "0_6m", probability: 0.18 }, { horizon: "within_18m", probability: 0.4 }],
      main_drivers: ["Gulf shipping escalation", "Export-control proposals"],
      signals: [], historical_comparisons: ["2022 cycle"], leading_indicators: [],
      invalidation_signals: ["De-escalation"], related_event_ids: ["e-uuid-1"], related_industry_ids: ["semis"], related_company_ids: [],
    },
    banking: {
      risk_type: "banking", score: 41, severity: "medium", confidence_score: 0.59, as_of: "2026-06-05T00:00:00Z",
      model_version: "v1", target_type: "global_posture", target_id: "G", model_rating: null,
      probability_by_horizon: [], main_drivers: ["Deposit outflows"], signals: [], historical_comparisons: [],
      leading_indicators: [], invalidation_signals: [], related_event_ids: [], related_industry_ids: [], related_company_ids: [],
    },
  },
  // 30-day history keyed by risk_type
  history: {
    geopolitical_supply_chain: (function () {
      // ascending series; the newest is score 78, ~2d ago 73, ~8d ago 64
      const day = 86400000;
      const now = Date.parse("2026-06-05T00:00:00Z");
      return [
        { id: "h0", risk_type: "geopolitical_supply_chain", score: 64, as_of: new Date(now - 8 * day).toISOString() },
        { id: "h1", risk_type: "geopolitical_supply_chain", score: 73, as_of: new Date(now - 2 * day).toISOString() },
        { id: "h2", risk_type: "geopolitical_supply_chain", score: 78, as_of: new Date(now).toISOString() },
      ];
    })(),
    banking: [],
  },
  industries: [
    { id: "i1", industry_id: "semis", as_of: "2026-06-05T00:00:00Z", impact_score: 86, risk_score: 78, opportunity_score: 22, news_velocity_score: 91, related_event_count: 9, summary: "Export-control." },
  ],
  companies: [
    { id: "c-nvda", entity_profile_id: "ep1", display_name: "NVIDIA", legal_name: "NVIDIA Corp", primary_ticker: "NVDA", exchange: "NASDAQ", country: "US", sector: "Tech", industry: "Semiconductors", website: null, logo_url: null, logo_source: null, active: true, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-06-05T00:00:00Z" },
    { id: "c-msft", entity_profile_id: "ep2", display_name: "Microsoft", legal_name: "Microsoft Corp", primary_ticker: "MSFT", exchange: "NASDAQ", country: "US", sector: "Tech", industry: "Cloud", website: null, logo_url: null, logo_source: null, active: true, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-06-05T00:00:00Z" },
  ],
  alerts: [
    { id: "al1", user_id: "u1", title: "Policy risk", message: "m", severity: "high", risk_score: 72, alert_type: "Risk threshold", related_event_id: "e-uuid-1", related_company_id: null, related_industry_id: "semis", evidence_refs: [{ source_type: "official", source_id: "ev1" }], evidence_signal_ids: [], status: "open", state: "open", created_at: "2026-06-05T14:15:00Z", updated_at: "2026-06-05T14:15:00Z", resolved_at: null },
    { id: "al2", user_id: "u1", title: "Escalated", message: "m", severity: "high", risk_score: 76, alert_type: "watchlist", related_event_id: "e-uuid-1", related_company_id: "c-nvda", related_industry_id: null, evidence_refs: [], evidence_signal_ids: [], status: "escalated", state: "escalated", created_at: "2026-06-05T13:45:00Z", updated_at: "2026-06-05T13:45:00Z", resolved_at: null },
    { id: "al3", user_id: "u1", title: "Resolved", message: "m", severity: "medium", risk_score: 55, alert_type: "source", related_event_id: null, related_company_id: null, related_industry_id: null, evidence_refs: [], evidence_signal_ids: [], status: "resolved", state: "resolved", created_at: "2026-06-05T12:00:00Z", updated_at: "2026-06-05T12:00:00Z", resolved_at: "2026-06-05T12:30:00Z" },
  ],
  watchlist: [
    { id: "w1", user_id: "u1", item_type: "company", item_id: "c-nvda", label: "NVIDIA", metadata: { ticker: "NVDA" }, alert_enabled: true, created_at: "2026-05-20T00:00:00Z" },
  ],
  jobs: [
    { id: "j1", job_key: "ingest", job_type: "Ingestion · News API", state: "succeeded", attempt: 1, max_attempts: 3, safe_to_rerun: true, created_at: "2026-06-05T08:25:00Z", updated_at: "2026-06-05T08:25:48Z" },
  ],
  sources: [
    { id: "src1", name: "Reuters Wire", source_type: "news_api", feed_url: "http://x", homepage_url: null, active: true, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-06-05T00:00:00Z", latest_health: { id: "sh1", source_id: "src1", provider: "reuters", checked_at: "2026-06-05T08:25:00Z", status: "healthy", latency_ms: 320, error_rate: 0.4, items_fetched: 1284 } },
    { id: "src2", name: "SEC EDGAR", source_type: "filing", feed_url: "http://y", homepage_url: null, active: true, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-06-05T00:00:00Z", latest_health: null },
  ],
  models: [
    { id: "m1", prompt_name: "Event analysis", prompt_version: "v1", provider: "Anthropic", model: "claude-analyst", output_schema_version: "1", cost_usd: 0.018, latency_ms: 2300, created_at: "2026-06-05T08:00:00Z" },
  ],
  brief: {
    report: {
      id: "rep1", report_type: "daily_brief", brief_date: "2026-06-05", title: "Daily Brief", status: "published", version: 2,
      sections: [
        { id: "sec1", section_order: 1, title: "Overview", body: "b", blocks: [], evidence_refs: ["claim-1", "claim-2"], grounding_status: "grounded" },
        { id: "sec2", section_order: 2, title: "Risk", body: "b", blocks: [], evidence_refs: ["claim-2"], grounding_status: "grounded" },
      ],
    },
  },
  evidenceByClaim: {
    "claim-1": {
      claim: { id: "claim-1", text: "Officials weigh curbs", type: "assertion", confidence: 0.8 },
      evidence: [
        { evidence_item_id: "ei-1", support_type: "supports", confidence: 0.9, source_type: "official", source_id: "src-a", title: "Commerce statement", publisher: "Commerce", url: "http://gov", published_at: "2026-06-05T14:10:00Z", credibility: 0.95, snippet: "…", snippet_origin: "summary", snippet_truncated: false },
      ],
    },
    "claim-2": {
      claim: { id: "claim-2", text: "Sector repriced", type: "assertion", confidence: 0.7 },
      evidence: [
        { evidence_item_id: "ei-1", support_type: "supports", confidence: 0.9, source_type: "official", source_id: "src-a", title: "Commerce statement", publisher: "Commerce", url: "http://gov", published_at: "2026-06-05T14:10:00Z", credibility: 0.95, snippet: "…", snippet_origin: "summary", snippet_truncated: false },
        { evidence_item_id: "ei-2", support_type: "supports", confidence: 0.88, source_type: "market_data", source_id: "src-b", title: "PHLX -2.8%", publisher: "Feed", url: "http://mkt", published_at: "2026-06-05T15:05:00Z", credibility: 0.9, snippet: "…", snippet_origin: "summary", snippet_truncated: false },
      ],
    },
  },
};

function defaultHandler(p, u) {
  if (p === "/api/v1/dashboard") return makeResponse(200, WIRE.dashboard, { "X-Request-ID": "req-dash" });
  if (p === "/api/v1/events") return makeResponse(200, pagedSlice(WIRE.events, u));
  if (p === "/api/v1/risk-radar") return makeResponse(200, pagedSlice(WIRE.riskRadar, u));
  if (p === "/api/v1/industries") return makeResponse(200, pagedSlice(WIRE.industries, u));
  if (p === "/api/v1/companies") return makeResponse(200, pagedSlice(WIRE.companies, u));
  if (p === "/api/v1/alerts") return makeResponse(200, pagedSlice(WIRE.alerts, u));
  if (p === "/api/v1/watchlist") return makeResponse(200, pagedSlice(WIRE.watchlist, u));
  if (p === "/api/v1/admin/jobs") return makeResponse(200, pagedSlice(WIRE.jobs, u));
  if (p === "/api/v1/admin/sources") return makeResponse(200, pagedSlice(WIRE.sources, u));
  if (p === "/api/v1/admin/models") return makeResponse(200, pagedSlice(WIRE.models, u));
  if (p === "/api/v1/reports/daily-brief/latest") return makeResponse(200, WIRE.brief);
  const rr = p.match(/^\/api\/v1\/risk-radar\/([^/]+)\/history$/);
  if (rr) { const t = decodeURIComponent(rr[1]); return makeResponse(200, pagedSlice(WIRE.history[t] || [], u)); }
  const rd = p.match(/^\/api\/v1\/risk-radar\/([^/]+)$/);
  if (rd) { const t = decodeURIComponent(rd[1]); const d = WIRE.riskDetail[t]; return d ? makeResponse(200, { risk: d }) : makeResponse(404, { error: { code: "not_found", message: "no obs", request_id: "req-rd" } }); }
  const ev = p.match(/^\/api\/v1\/evidence\/([^/]+)$/);
  if (ev) { const c = decodeURIComponent(ev[1]); const e = WIRE.evidenceByClaim[c]; return e ? makeResponse(200, e) : makeResponse(404, { error: { code: "not_found", message: "claim", request_id: "req-ev" } }); }
  if (p === "/api/v1/company-research/profiles" || p === "/api/v1/provider-data/company-research/profiles") {
    // resolve tickers to profiles carrying a matching identity
    const items = [];
    const tks = (u.searchParams.get("tickers") || "").split(",").filter(Boolean);
    tks.forEach((t) => { if (t === "NVDA") items.push({ identity: { name: "NVIDIA", ticker: "NVDA", cik: null }, asOf: "2026-06-05", financial: { headline: "h", metrics: [], notes: [] } }); });
    return makeResponse(200, { items, total: items.length, limit: 50, offset: 0 });
  }
  return makeResponse(404, { error: { code: "not_found", message: "unknown", request_id: "req-404" } });
}

const FIXTURES = {
  core: {
    meta: { now: "2026-06-05T08:30:00-07:00" },
    dashboard: {
      dailySummary: { date: "2026-06-05", lastUpdatedAt: "2026-06-05T08:30:00-07:00", summary: "demo", keyPoints: ["k"], overallRiskLevel: "high", confidenceScore: 0.71 },
      metrics: [{ label: "Top Events", value: 42 }],
      upcomingTriggers: [{ id: "ut1", title: "Labor", importance: "high", reason: "r" }],
      eventMap: [{ locationName: "DC", latitude: 38.9, longitude: -77, x: 0.27, y: 0.4, eventCount: 6, maxRiskScore: 76, dominantEventType: "Policy", relatedEventIds: ["evt-semis"] }],
    },
    risk: {
      riskRadar: [
        { riskType: "Macro Risk", score: 58, level: "medium", change24h: 2, change7d: 5, topDriver: "Labor", confidenceScore: 0.64 },
        { riskType: "Geopolitical Risk", score: 78, level: "high", change24h: 8, change7d: 14, topDriver: "Gulf", confidenceScore: 0.7 },
      ],
      riskDetails: { "Macro Risk": { riskType: "Macro Risk", score: 58, severity: "medium", probabilityByHorizon: [], mainDrivers: [], signals: [], historicalComparisons: [], leadingIndicators: [], invalidationSignals: [], relatedEventIds: [], relatedIndustryIds: [], relatedCompanyIds: [] } },
    },
    industries: [{ industryId: "semis", industryName: "Semiconductors", impactScore: 86, riskScore: 78, opportunityScore: 22, relatedEventCount: 9, direction: "negative", newsVelocityScore: 91, summary: "s" }],
    companies: [{ companyId: "nvda", name: "NVIDIA", ticker: "NVDA", exchange: "NASDAQ", industry: "Semiconductors", country: "US", impactDirection: "negative", impactScore: 82, riskScore: 76, relatedEventCount: 6, topDriver: "x", lastUpdatedAt: "2026-06-05T08:10:00-07:00" }],
    companyProfiles: { nvda: { asOf: "2026-06-05", financial: { headline: "h", metrics: [], notes: [] } } },
    evidence: [
      { id: "ev1", sourceType: "rss", title: "Officials weigh curbs", publisher: "Reuters", url: "#", publishedAt: "2026-06-05T05:40:00-07:00", credibilityScore: 0.86, relatedClaims: ["c1"] },
      { id: "ev3", sourceType: "official", title: "Commerce statement", publisher: "Commerce", url: "#", publishedAt: "2026-06-05T07:10:00-07:00", credibilityScore: 0.95, relatedClaims: ["c1"] },
    ],
    alerts: [{ id: "al1", title: "t", message: "m", severity: "high", riskScore: 72, alertType: "x", relatedEventId: "evt-semis", evidenceSourceIds: ["ev1"], status: "new", createdAt: "2026-06-05T07:15:00-07:00" }],
    watchlist: [{ id: "w1", itemType: "company", label: "NVIDIA", metadata: { ticker: "NVDA" }, alertEnabled: true, createdAt: "2026-05-20" }],
    admin: { jobs: [{ id: "j1", jobType: "Ingest", status: "succeeded" }], sources: [{ sourceId: "s1", name: "Reuters", sourceType: "news_api", status: "healthy" }], models: [{ provider: "Anthropic", modelName: "c", taskType: "t", requestCount: 1, successRate: 99, averageLatencyMs: 2, averageCostUsd: 0.01, schemaValidityRate: 99, fallbackCount: 0 }] },
    ask: { suggestions: ["What are the biggest risks?", "Is recession risk increasing?"] },
    events: [],
  },
  events: [
    {
      id: "evt-semis", title: "Semiconductor export-control risk rises", summary: "s", eventType: "Policy", eventTypes: ["Policy"],
      status: "developing", riskLevel: "high", hotnessScore: 88, riskScore: 76, confidenceScore: 0.72,
      affectedIndustries: ["Semiconductors"], affectedCompanies: [{ name: "NVIDIA", ticker: "NVDA", impactDirection: "negative" }],
      locations: ["Washington"], primaryLocation: "Washington", sourceCount: 18, articleCount: 31,
      firstSeenAt: "2026-06-05T06:10:00-07:00", lastUpdatedAt: "2026-06-05T08:20:00-07:00", whyItMatters: "chokepoint",
      crisisRating: { target_type: "event_cluster", target_id: "evt-semis", risk_type: "geopolitical_supply_chain", risk_score: 76, risk_level: "critical" },
      evidenceSourceIds: ["ev1", "ev3"],
      forecastScenarios: [{ id: "f1", name: "base_case", probability: 0.55, timeHorizon: "1–4 weeks", narrative: "n", triggers: [], leadingIndicators: [], expectedImpact: { industries: [], companies: [] }, invalidationSignals: [], confidenceScore: 0.68, lastUpdatedAt: "2026-06-05T08:30:00-07:00" }],
      modelDebate: { consensusPoints: ["c"], disagreementPoints: [], criticNotes: [], modelVotes: [] },
    },
  ],
};

function loadReachable(overrides) {
  return A.loadSnapshot({ fixtures: FIXTURES, apiBase: "http://localhost:8000", fetch: makeServer(overrides), now: "2026-06-05T16:00:00Z" });
}

/* ---- types.ts field extraction (mapping-table alignment) ------------------ */
function parseTypeFields(src, name) {
  const lines = src.split("\n");
  let i = 0;
  const re = new RegExp("^export type " + name + "\\b");
  while (i < lines.length && !re.test(lines[i])) i++;
  assert.ok(i < lines.length, "type not found: " + name);
  const required = new Set(), all = new Set();
  let depth = 0, started = false;
  for (; i < lines.length; i++) {
    const line = lines[i];
    const startDepth = depth;
    if (started && startDepth === 1) {
      const m = line.match(/^\s*([A-Za-z0-9_]+)(\?)?\s*:/);
      if (m) { all.add(m[1]); if (!m[2]) required.add(m[1]); }
    }
    for (const ch of line) { if (ch === "{") { depth++; started = true; } else if (ch === "}") depth--; }
    if (started && depth === 0) break;
  }
  return { required, all };
}
const TYPES_SRC = fs.readFileSync(TYPES_PATH, "utf8");

function assertAligned(typeName, produced) {
  const { required, all } = parseTypeFields(TYPES_SRC, typeName);
  const keys = Object.keys(produced);
  for (const k of keys) assert.ok(all.has(k), typeName + ": produced unknown field '" + k + "'");
  for (const r of required) assert.ok(keys.includes(r), typeName + ": missing required field '" + r + "'");
}

/* ======================================================================== */
/*  Export contract / no side effects                                        */
/* ======================================================================== */
test("CommonJS + browser export, no data side effects at eval", () => {
  assert.equal(typeof A.loadSnapshot, "function");
  assert.equal(typeof A.buildFixtureSnapshot, "function");
  assert.equal(A.DEFAULT_API_BASE, "http://localhost:8000");
  assert.deepEqual(A.DATA_QUALITY, { LIVE: "live", PARTIAL: "partial", SYNTHETIC: "synthetic", EMPTY: "empty" });
  // requiring the module attaches the browser global (globalThis in node) but never
  // assembles window.DATA or dispatches an event.
  assert.equal(globalThis.SignalApiAdapter, A);
  assert.equal(globalThis.DATA, undefined);
});

/* ======================================================================== */
/*  Timezone: America/New_York DST conversion                                */
/* ======================================================================== */
test("toNyDisplayIso converts UTC-Z to America/New_York with correct DST offset", () => {
  const summer = A.toNyDisplayIso("2026-06-05T12:00:00Z"); // EDT
  assert.equal(summer, "2026-06-05T08:00:00-04:00");
  assert.ok(summer.endsWith("-04:00"));
  const winter = A.toNyDisplayIso("2026-01-15T12:00:00Z"); // EST
  assert.equal(winter, "2026-01-15T07:00:00-05:00");
  assert.ok(winter.endsWith("-05:00"));
  // stays Date-parseable and instant-preserving
  assert.equal(new Date(summer).getTime(), Date.parse("2026-06-05T12:00:00Z"));
  assert.equal(new Date(winter).getTime(), Date.parse("2026-01-15T12:00:00Z"));
  // date-only values are not shifted; null passes through; junk passes through
  assert.equal(A.toNyDisplayIso("2026-06-05"), "2026-06-05");
  assert.equal(A.toNyDisplayIso(null), null);
  assert.equal(A.toNyDisplayIso("not-a-date"), "not-a-date");
});

/* ======================================================================== */
/*  Alert lifecycle status mapping                                           */
/* ======================================================================== */
test("alert lifecycle states map to the UI triad", () => {
  assert.equal(A.mapAlertStatus("open"), "new");
  assert.equal(A.mapAlertStatus("escalated"), "acknowledged");
  assert.equal(A.mapAlertStatus("downgraded"), "acknowledged");
  assert.equal(A.mapAlertStatus("resolved"), "resolved");
  assert.equal(A.mapAlertStatus("superseded"), "resolved");
  assert.equal(A.mapAlertStatus("weird"), "new"); // safe default
});

/* ======================================================================== */
/*  Individual mappers: field presence + mapping-table alignment            */
/* ======================================================================== */
test("mappers align with types.ts and apply safe defaults", () => {
  assertAligned("DailySummary", A.mapDailySummary(WIRE.dashboard.summary));
  assertAligned("DashboardMetric", A.mapMetric(WIRE.dashboard.metrics[0]));
  assertAligned("DashboardMetric", A.mapMetric(WIRE.dashboard.metrics[1]));
  assertAligned("UpcomingTrigger", A.mapUpcomingTrigger(WIRE.dashboard.upcoming_triggers[0]));
  assertAligned("EventMapPoint", A.mapEventMapPoint(WIRE.dashboard.event_map[0]));
  assertAligned("RiskDetail", A.mapRiskDetail(WIRE.riskDetail.geopolitical_supply_chain, "Geopolitical / Supply Chain"));
  assertAligned("IndustryHeatmapItem", A.mapIndustry(WIRE.industries[0]));
  assertAligned("CompanyImpactRow", A.mapCompany(WIRE.companies[0], WIRE.dashboard.company_ranking[0]));
  assertAligned("CompanyImpactRow", A.mapCompany(WIRE.companies[1], null));
  assertAligned("EventCard", A.mapEventListItem(WIRE.events[0]));
  assertAligned("EventCard", A.mapEventListItem(WIRE.events[1]));
  assertAligned("Alert", A.mapAlert(WIRE.alerts[0]));
  assertAligned("WatchlistItem", A.mapWatchlistItem(WIRE.watchlist[0]));
  assertAligned("PipelineJob", A.mapJob(WIRE.jobs[0]));
  assertAligned("SourceStatus", A.mapSource(WIRE.sources[0]));
  assertAligned("SourceStatus", A.mapSource(WIRE.sources[1]));
  assertAligned("ModelUsageStats", A.mapModel(WIRE.models[0]));
  assertAligned("EvidenceSource", A.mapEvidenceLink(WIRE.evidenceByClaim["claim-1"].evidence[0], "claim-1"));
  assertAligned("RiskRadarItem", A.mapRiskRadarItem(WIRE.riskRadar[0], WIRE.riskDetail.geopolitical_supply_chain, [], Date.now()));
});

test("safe defaults for an unscored live event", () => {
  const e = A.mapEventListItem(WIRE.events[1]);
  assert.equal(e.riskLevel, "low");
  assert.equal(e.riskScore, 0);
  assert.equal(e.confidenceScore, 0);
  assert.equal(e.hotnessScore, 0);
  assert.deepEqual(e.eventTypes, []);
  assert.deepEqual(e.affectedIndustries, []);
  assert.equal(typeof e.whyItMatters, "string");
});

test("industries and list-companies expose honest unknown/null defaults, never fixtures", () => {
  const ind = A.mapIndustry(WIRE.industries[0]);
  assert.equal(ind.direction, "unknown");
  assert.equal(ind.industryName, "semis"); // industry_id used as name (no live catalog)
  const comp = A.mapCompany(WIRE.companies[1], null); // MSFT not in ranking
  assert.equal(comp.impactDirection, "unknown");
  assert.equal(comp.impactScore, null);
  assert.equal(comp.riskScore, null);
  assert.equal(comp.topDriver, "");
  // admin models: no aggregate source -> null, not fabricated
  const m = A.mapModel(WIRE.models[0]);
  assert.equal(m.requestCount, null);
  assert.equal(m.successRate, null);
  assert.equal(m.schemaValidityRate, null);
  assert.equal(m.fallbackCount, null);
  assert.equal(m.averageLatencyMs, 2300); // real per-run value
});

/* ======================================================================== */
/*  Real-history deltas                                                       */
/* ======================================================================== */
test("radar deltas are computed only from real history, else null", () => {
  const day = 86400000;
  const now = Date.parse("2026-06-05T00:00:00Z");
  const series = [
    { t: now - 8 * day, score: 64 },
    { t: now - 2 * day, score: 73 },
    { t: now, score: 78 },
  ];
  const item = A.mapRiskRadarItem(WIRE.riskRadar[0], WIRE.riskDetail.geopolitical_supply_chain, series, now);
  assert.equal(item.change24h, 5);  // 78 - 73 (latest <= now-1d)
  assert.equal(item.change7d, 14);  // 78 - 64 (latest <= now-7d)
  assert.equal(item.topDriver, "Gulf shipping escalation"); // from real detail.main_drivers[0]
  // no history -> deltas null (never invented)
  const bare = A.mapRiskRadarItem(WIRE.riskRadar[0], null, [], now);
  assert.equal(bare.change24h, null);
  assert.equal(bare.change7d, null);
  assert.equal(bare.topDriver, "");
});

/* ======================================================================== */
/*  Full reachable snapshot                                                  */
/* ======================================================================== */
test("reachable snapshot: every field present, blocks merged, metadata attached", async () => {
  const D = await loadReachable();
  // every page-facing field exists
  for (const f of ["NOW", "conf", "dailySummary", "metrics", "upcomingTriggers", "eventMap", "riskRadar", "riskDetails", "industries", "companies", "companyProfiles", "evidence", "evidenceById", "events", "eventsById", "alerts", "watchlist", "adminJobs", "adminSources", "adminModels", "askSuggestions", "riskTrends", "blockQuality", "runtime", "apiErrors"]) {
    assert.ok(f in D, "missing field " + f);
  }
  assert.equal(typeof D.conf, "function");
  assert.equal(D.conf(0.8), "High");
  assert.equal(typeof D.NOW, "string");
  assert.equal(D.runtime.mode, "api");
  assert.ok(D.NOW.endsWith("-04:00")); // now converted to NY summer offset

  // alerts mapped + status triad
  assert.equal(D.alerts.length, 3);
  assert.deepEqual(D.alerts.map((a) => a.status), ["new", "acknowledged", "resolved"]);

  // radar deduped to one per risk_type (2), labels mapped
  assert.equal(D.riskRadar.length, 2);
  const labels = D.riskRadar.map((r) => r.riskType).sort();
  assert.deepEqual(labels, ["Banking", "Geopolitical / Supply Chain"]);
  const geo = D.riskRadar.find((r) => r.riskType === "Geopolitical / Supply Chain");
  assert.equal(geo.score, 78); // latest observation won over the older duplicate
  assert.equal(geo.change24h, 5);
  assert.equal(geo.change7d, 14);
  assert.equal(geo.topDriver, "Gulf shipping escalation");

  // riskDetails keyed by label; canonical horizons preserved
  assert.ok(D.riskDetails["Geopolitical / Supply Chain"]);
  assert.deepEqual(D.riskDetails["Geopolitical / Supply Chain"].probabilityByHorizon.map((p) => p.horizon), ["0_6m", "within_18m"]);
  assert.deepEqual(D.riskDetails["Geopolitical / Supply Chain"].signals, []); // never fabricated

  // riskTrends from REAL history (score arrays), not synthetic curves
  assert.deepEqual(D.riskTrends["Geopolitical / Supply Chain"], [64, 73, 78]);
  assert.deepEqual(D.riskTrends["Banking"], []); // no history -> empty, not a fake curve

  // companies enriched from dashboard ranking (real), MSFT honest defaults
  const nv = D.companies.find((c) => c.companyId === "c-nvda");
  assert.equal(nv.impactScore, 82);
  assert.equal(nv.topDriver, "Export-control concerns");
  const ms = D.companies.find((c) => c.companyId === "c-msft");
  assert.equal(ms.impactScore, null);

  // company-research enrichment matched NVDA by ticker
  assert.ok(D.companyProfiles["c-nvda"]);

  // events merged: live cores + retained synthetic fixture event
  assert.ok(D.eventsById["e-uuid-1"]); // live
  assert.ok(D.eventsById["evt-semis"]); // synthetic fixture retained
  assert.equal(D.eventsById["evt-semis"].dataQuality, "synthetic");

  // evidence: real drawer links + retained fixture evidence, deduped, unioned claims
  const ei1 = D.evidenceById["ei-1"];
  assert.ok(ei1); // real
  assert.deepEqual(ei1.relatedClaims.sort(), ["claim-1", "claim-2"]); // unioned across claims
  assert.ok(D.evidenceById["ev1"]); // fixture retained for synthetic events

  // block quality registry + non-enumerable dataQuality tags
  assert.equal(D.blockQuality.metrics.quality, "live");
  assert.equal(D.blockQuality.industries.quality, "partial");
  assert.equal(D.blockQuality.askSuggestions.quality, "synthetic");
  assert.equal(D.blockQuality.NOW.kind, "derived");
  assert.equal(D.metrics.dataQuality, "live");
  assert.equal(D.events.dataQuality, "partial");
  // non-enumerable: does not show up in key iteration or JSON
  assert.ok(!Object.keys(D.metrics).includes("dataQuality"));
  assert.ok(Array.isArray(D.metrics));
});

/* ======================================================================== */
/*  Partial success + request_id capture                                     */
/* ======================================================================== */
test("one failed endpoint does not erase successful blocks; request_id captured", async () => {
  const D = await loadReachable({
    "/api/v1/industries": makeResponse(500, { error: { code: "internal_error", message: "boom", request_id: "req-ind-1" } }, { "X-Request-ID": "hdr-ind" }),
  });
  // industries degraded to empty, but everything else still present
  assert.deepEqual(D.industries, []);
  assert.equal(D.blockQuality.industries.quality, "empty");
  assert.equal(D.alerts.length, 3);
  assert.equal(D.companies.length, 2);
  const indErr = D.apiErrors.find((e) => e.endpoint.includes("/industries"));
  assert.ok(indErr);
  assert.equal(indErr.code, "internal_error");
  assert.equal(indErr.message, "boom");
  assert.equal(indErr.requestId, "req-ind-1"); // body error.request_id preferred
  assert.equal(D.runtime.degraded, true);
});

test("request_id falls back to the X-Request-ID header when body has no envelope", async () => {
  const D = await loadReachable({
    "/api/v1/alerts": makeResponse(503, undefined, { "X-Request-ID": "hdr-alerts" }),
  });
  const err = D.apiErrors.find((e) => e.endpoint.includes("/alerts"));
  assert.ok(err);
  assert.equal(err.requestId, "hdr-alerts");
  assert.equal(err.code, "http_503");
});

/* ======================================================================== */
/*  Pagination + truncation                                                  */
/* ======================================================================== */
test("paginates across pages using the real total", async () => {
  const many = [];
  for (let i = 0; i < 230; i++) many.push({ id: "u1", item_type: "company", item_id: "x", label: "W" + i, metadata: null, alert_enabled: true, created_at: "2026-05-20T00:00:00Z" });
  const D = await loadReachable({ "/api/v1/watchlist": (u) => makeResponse(200, pagedSlice(many, u)) });
  assert.equal(D.watchlist.length, 230);
  assert.equal(D.blockQuality.watchlist.quality, "live");
});

test("over-cap pagination marks the block partial and records a truncation, never silent", async () => {
  const many = [];
  for (let i = 0; i < 1200; i++) many.push({ id: "a" + i, user_id: "u1", title: "t", message: "m", severity: "low", risk_score: 1, alert_type: "x", evidence_refs: [], evidence_signal_ids: [], status: "open", state: "open", created_at: "2026-06-05T00:00:00Z" });
  const D = await loadReachable({ "/api/v1/alerts": (u) => makeResponse(200, pagedSlice(many, u)) });
  assert.equal(D.alerts.length, 500); // SNAPSHOT_ITEM_CAP
  assert.equal(D.blockQuality.alerts.quality, "partial");
  assert.equal(D.blockQuality.alerts.truncated, true);
  const trunc = D.apiErrors.find((e) => e.code === "truncated" && e.endpoint.includes("/alerts"));
  assert.ok(trunc);
});

/* ======================================================================== */
/*  Evidence adaptation & 404 brief                                          */
/* ======================================================================== */
test("evidence comes from real drawer links; a 404 brief is an honest empty source", async () => {
  const D = await loadReachable({ "/api/v1/reports/daily-brief/latest": makeResponse(404, { error: { code: "not_found", message: "no brief", request_id: "req-nb" } }) });
  // no real evidence, but fixture evidence retained for the synthetic events -> partial, no error
  assert.ok(D.evidenceById["ev1"]);
  assert.ok(!D.evidenceById["ei-1"]);
  assert.equal(D.blockQuality.evidence.quality, "partial");
  assert.ok(!D.apiErrors.some((e) => e.endpoint.includes("daily-brief"))); // 404 is not an error
});

/* ======================================================================== */
/*  No synthetic trend in a reachable-but-degraded state                     */
/* ======================================================================== */
test("reachable API with failed history yields empty series, never a fake curve", async () => {
  const D = await loadReachable({
    "/api/v1/risk-radar/geopolitical_supply_chain/history": "throw",
    "/api/v1/risk-radar/banking/history": "throw",
  });
  assert.equal(D.runtime.mode, "api");
  for (const label of Object.keys(D.riskTrends)) {
    assert.deepEqual(D.riskTrends[label], []); // empty, not a 30-point synthetic curve
  }
  assert.notEqual(D.blockQuality.riskTrends.quality, "synthetic");
});

/* ======================================================================== */
/*  Full API-unreachable fixture fallback                                    */
/* ======================================================================== */
test("total API-unreachable -> full fixture snapshot in demo mode", async () => {
  const D = await A.loadSnapshot({ fixtures: FIXTURES, apiBase: "http://localhost:8000", fetch: async () => { throw new Error("ECONNREFUSED"); }, now: "2026-06-05T16:00:00Z" });
  assert.equal(D.runtime.mode, "demo");
  assert.equal(D.runtime.degraded, true);
  // fixtures passed through
  assert.equal(D.events.length, 1);
  assert.equal(D.events[0].id, "evt-semis");
  assert.equal(D.companies[0].name, "NVIDIA");
  assert.deepEqual(D.askSuggestions, FIXTURES.core.ask.suggestions);
  // every block synthetic
  for (const f of ["metrics", "events", "riskRadar", "alerts", "companies", "askSuggestions"]) {
    assert.equal(D.blockQuality[f].quality, "synthetic", f + " should be synthetic in demo");
  }
  // demo mode is the ONLY place a synthetic 30-day trend is generated
  assert.equal(D.riskTrends["Macro Risk"].length, 30);
  assert.equal(D.riskTrends["Geopolitical Risk"].length, 30);
  // derived lookups still built
  assert.ok(D.eventsById["evt-semis"]);
  assert.ok(D.evidenceById["ev1"]);
});

test("buildFixtureSnapshot is pure and self-contained", () => {
  const D = A.buildFixtureSnapshot(FIXTURES.core, FIXTURES.events, { apiBase: "http://x", now: "2026-06-05T16:00:00Z" });
  assert.equal(D.runtime.mode, "demo");
  assert.equal(typeof D.conf, "function");
  assert.ok(Array.isArray(D.metrics));
  assert.equal(D.events[0].dataQuality, "synthetic");
});

/* ======================================================================== */
/*  Company-research batching: independent, mismatched list lengths           */
/* ======================================================================== */
test("company-research batching requests every identifier exactly once (mismatched lengths)", async () => {
  // 170 companyIds, 100 tickers, 30 CIKs — all categories, several > batch size (80).
  const companies = [];
  for (let i = 0; i < 170; i++) {
    const c = { companyId: "ID" + i };
    if (i < 100) c.ticker = "T" + i;
    if (i < 30) c.cik = String(1000 + i);
    companies.push(c);
  }
  const seenTickers = [], seenCiks = [], seenIds = [], sharedIndexViolations = [];
  const fetchImpl = async (url) => {
    const u = new URL(url);
    const t = (u.searchParams.get("tickers") || "").split(",").filter(Boolean);
    const c = (u.searchParams.get("ciks") || "").split(",").filter(Boolean);
    const id = (u.searchParams.get("company_ids") || "").split(",").filter(Boolean);
    // independent loops => each request carries exactly one category
    const categories = [t.length > 0, c.length > 0, id.length > 0].filter(Boolean).length;
    if (categories !== 1) sharedIndexViolations.push(url);
    t.forEach((x) => seenTickers.push(x));
    c.forEach((x) => seenCiks.push(x));
    id.forEach((x) => seenIds.push(x));
    return makeResponse(200, { items: [], total: 0, limit: 80, offset: 0 });
  };

  const out = await A.enrichCompanyProfiles(companies, "http://localhost:8000", fetchImpl);

  // no request ever mixed unrelated-length categories (no shared slicing index)
  assert.deepEqual(sharedIndexViolations, []);
  // exact coverage: every identifier requested once, no dupes, no omissions
  const expTickers = []; for (let i = 0; i < 100; i++) expTickers.push("T" + i);
  const expIds = []; for (let i = 0; i < 170; i++) expIds.push("ID" + i);
  assert.deepEqual(seenTickers.slice().sort(), expTickers.slice().sort());
  assert.deepEqual(new Set(seenTickers).size, 100);
  assert.deepEqual(seenIds.slice().sort(), expIds.slice().sort());
  assert.deepEqual(new Set(seenIds).size, 170);
  assert.deepEqual(new Set(seenCiks).size, 30);
  // adapter's own accounting agrees
  assert.equal(out.requested.tickers.length, 100);
  assert.equal(out.requested.company_ids.length, 170);
  assert.equal(out.requested.ciks.length, 30);
});

test("company-research alias fallback on 404 and match-by-ticker", async () => {
  const companies = [{ companyId: "c-nvda", name: "NVIDIA", ticker: "NVDA" }];
  const urls = [];
  const fetchImpl = async (url) => {
    urls.push(url);
    const u = new URL(url);
    if (u.pathname === "/api/v1/company-research/profiles") return makeResponse(404, { error: { code: "not_found", message: "n", request_id: "r" } });
    // alias route serves the profile
    return makeResponse(200, { items: [{ identity: { name: "NVIDIA", ticker: "NVDA", cik: null }, asOf: "2026-06-05" }], total: 1, limit: 80, offset: 0 });
  };
  const out = await A.enrichCompanyProfiles(companies, "http://localhost:8000", fetchImpl);
  assert.ok(urls.some((x) => x.includes("/provider-data/company-research/profiles")));
  assert.ok(out.profiles["c-nvda"]);
  assert.equal(out.errors.length, 0);
});

test("company-research enrichment failure is partial, never erases companies", async () => {
  const companies = [{ companyId: "c-nvda", name: "NVIDIA", ticker: "NVDA" }];
  const D = await loadReachable({
    "/api/v1/company-research/profiles": makeResponse(500, { error: { code: "internal_error", message: "x", request_id: "r" } }),
    "/api/v1/provider-data/company-research/profiles": makeResponse(500, { error: { code: "internal_error", message: "x", request_id: "r" } }),
  });
  assert.equal(D.companies.length, 2); // companies preserved
  assert.equal(D.blockQuality.companyProfiles.quality, "partial");
  assert.ok(D.apiErrors.some((e) => e.endpoint.includes("company-research")));
});

/* ======================================================================== */
/*  Required literal SYNTHETIC source tags                                    */
/* ======================================================================== */
test("adapter source carries the required literal // SYNTHETIC: tags", () => {
  const src = fs.readFileSync(ADAPTER_PATH, "utf8");
  assert.match(src, /\/\/ SYNTHETIC: ask\/query suggestions/);
  assert.match(src, /\/\/ SYNTHETIC: event\.modelDebate is fixture-backed/);
  assert.match(src, /\/\/ SYNTHETIC: event\.forecastScenarios are fixture-backed/);
  assert.match(src, /\/\/ SYNTHETIC: event\.crisisRating \/ historicalAnalogies/);
  assert.match(src, /\/\/ SYNTHETIC: fallback-only generated/);
});
