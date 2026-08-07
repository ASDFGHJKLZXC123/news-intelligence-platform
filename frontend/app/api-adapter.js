/* ============================================================================
   SIGNAL — API adapter (Stage 7, item 6A)

   The ONLY frontend runtime file that knows backend endpoints, snake_case wire
   keys, pagination/error envelopes, the alert lifecycle map, and the canonical
   risk-token vocabulary. It maps the FastAPI `/api/v1` wire shapes to the
   `types.ts` fixture schema and assembles the flattened `window.DATA` contract
   the pages already consume. Pages never see wire formats.

   Contract sources: docs/adr/0007-frontend-architecture-for-mvp.md and
   docs/specs/api-adapter-contract.md. Field-by-field target schema: app/types.ts.

   This module has NO side effects at evaluation time: it does not fetch, does
   not assign window.DATA, and does not dispatch events. Item 6B wires the loader
   (readiness Promise + `signal:data-ready`) around `loadSnapshot` / the fixture
   fallback; item 7 renders the data-quality badges/banner from the metadata this
   adapter attaches (blockQuality / runtime / apiErrors).

   Exports a browser global (`window.SignalApiAdapter`) and a CommonJS export for
   `node --test`. No module bundler / build step.
   ========================================================================== */
(function (globalRoot) {
  "use strict";

  /* ---- Configuration ------------------------------------------------------ */
  var DEFAULT_API_BASE = "http://localhost:8000"; // ADR 0007 / adapter-contract
  var API_PREFIX = "/api/v1";

  // Four data-quality states pages may render a badge for (adapter-contract).
  var DATA_QUALITY = { LIVE: "live", PARTIAL: "partial", SYNTHETIC: "synthetic", EMPTY: "empty" };

  // Whole-snapshot MVP bounds (ADR 0007): the full dataset loads, but each block
  // is still capped so a data spike cannot produce an unbounded payload/scan.
  var PAGE_LIMIT = 100;         // per request
  var SNAPSHOT_ITEM_CAP = 500;  // per paginated block
  var EVIDENCE_CLAIM_CAP = 200; // distinct claim refs resolved for the drawer
  var COMPANY_BATCH_SIZE = 80;  // company-research identifiers per request
  var RISK_HISTORY_DAYS = 30;   // adapter-contract shape gap 3

  // numeric confidence -> label (mirrors the retired data.js `conf`).
  function conf(s) { return s >= 0.7 ? "High" : s >= 0.4 ? "Medium" : "Low"; }

  /* ---- Canonical enum maps ------------------------------------------------ */

  // Alert lifecycle (ADR 0010 wire states) -> UI triad (types.ts Alert.status).
  var ALERT_STATUS_MAP = {
    open: "new",
    escalated: "acknowledged",
    downgraded: "acknowledged",
    resolved: "resolved",
    superseded: "resolved",
  };
  function mapAlertStatus(state) { return ALERT_STATUS_MAP[state] || "new"; }

  // Canonical risk_type tokens (db.models.enums.RiskType) -> stable UI labels.
  // These labels also key riskDetails / riskTrends so the risk pages resolve them.
  var RISK_TYPE_LABELS = {
    banking: "Banking",
    currency: "Currency",
    sovereign: "Sovereign",
    recession: "Recession",
    market_liquidity: "Market Liquidity",
    geopolitical_supply_chain: "Geopolitical / Supply Chain",
    company: "Company Crisis",
  };
  function riskTypeLabel(token) {
    if (token == null) return "Unknown";
    if (RISK_TYPE_LABELS[token]) return RISK_TYPE_LABELS[token];
    // Deterministic Title Case fallback for an unmapped-but-real token.
    return String(token)
      .split(/[_\s]+/)
      .map(function (w) { return w ? w.charAt(0).toUpperCase() + w.slice(1) : w; })
      .join(" ");
  }

  // Pipeline job state -> types.ts PipelineJob.status.
  var JOB_STATUS_MAP = {
    queued: "queued", pending: "queued", scheduled: "queued",
    running: "running", in_progress: "running", started: "running",
    succeeded: "succeeded", success: "succeeded", completed: "succeeded", done: "succeeded",
    failed: "failed", error: "failed", errored: "failed",
    partially_failed: "partially_failed", partial: "partially_failed",
  };
  function mapJobStatus(state) { return JOB_STATUS_MAP[state] || state || "queued"; }

  // Source-health status -> types.ts SourceStatus.status.
  var SOURCE_STATUS_SET = { healthy: 1, degraded: 1, down: 1 };
  function mapSourceStatus(status) { return SOURCE_STATUS_SET[status] ? status : "degraded"; }

  /* ---- Timezone: UTC-Z wire -> America/New_York display ISO (ADR 0009) ----- */
  // Uses Intl (full ICU) so the offset is correct across DST and never depends
  // on the browser-local timezone. Output stays ISO 8601 and Date-parseable.
  function _pad2(n) { return (n < 10 ? "0" : "") + n; }

  function _nyParts(date) {
    var dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
    var out = {};
    var parts = dtf.formatToParts(date);
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].type !== "literal") out[parts[i].type] = parts[i].value;
    }
    if (out.hour === "24") out.hour = "00"; // some ICU builds emit hour 24 for midnight
    return out;
  }

  function _nyOffsetMinutes(date, parts) {
    var asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
    return Math.round((asUtc - date.getTime()) / 60000); // NY minus UTC (negative for America/New_York)
  }

  function toNyDisplayIso(input) {
    if (input == null) return input;
    // Date-only values (as_of_date, brief_date, createdAt fixtures) are NOT shifted.
    if (typeof input === "string" && /^\d{4}-\d{2}-\d{2}$/.test(input)) return input;
    var d = new Date(input);
    if (isNaN(d.getTime())) return input; // unparseable -> pass through unchanged (honest)
    var parts = _nyParts(d);
    var off = _nyOffsetMinutes(d, parts);
    var sign = off <= 0 ? "-" : "+";
    var abs = Math.abs(off);
    var offStr = sign + _pad2(Math.floor(abs / 60)) + ":" + _pad2(abs % 60);
    return parts.year + "-" + parts.month + "-" + parts.day + "T" +
      parts.hour + ":" + parts.minute + ":" + parts.second + offStr;
  }

  /* ---- Small helpers ------------------------------------------------------ */
  function _normBase(base) { return String(base || DEFAULT_API_BASE).replace(/\/+$/, ""); }
  function _num(v, dflt) { return v == null ? dflt : v; }
  function unique(values) {
    var seen = {}, out = [];
    for (var i = 0; i < values.length; i++) {
      var v = values[i];
      if (v && !seen[v]) { seen[v] = 1; out.push(v); }
    }
    return out;
  }
  function chunk(list, size) {
    var out = [];
    for (var i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
    return out;
  }
  function normalizeTicker(value) { return value == null ? "" : String(value).trim().toUpperCase(); }
  function normalizeCik(value) {
    var digits = value == null ? "" : String(value).replace(/\D/g, "");
    return digits ? digits.replace(/^0+(?=\d)/, "").padStart(10, "0") : "";
  }
  function normalizeId(value) { return value == null ? "" : String(value).trim(); }
  function normalizeName(value) {
    return value == null ? "" : String(value).trim().toLowerCase().replace(/\s+/g, " ");
  }

  // Attach a NON-enumerable `dataQuality` marker so arrays keep array behavior and
  // objects keep key iteration / JSON.stringify unchanged (adapter-contract).
  function tagQuality(value, quality) {
    if (value && typeof value === "object") {
      try {
        Object.defineProperty(value, "dataQuality", {
          value: quality, enumerable: false, configurable: true, writable: true,
        });
      } catch (e) { /* frozen input: registry still records the quality */ }
    }
    return value;
  }

  // Preserve a backend request failure as a structured object (adapter-contract:
  // endpoint/code/message/requestId; body error.request_id preferred, then header).
  function apiError(endpoint, res) {
    var bodyErr = res && res.body && res.body.error ? res.body.error : null;
    return {
      endpoint: endpoint,
      code: (bodyErr && bodyErr.code) || (res && res.reached ? "http_" + (res.status || 0) : "network_error"),
      message: (bodyErr && bodyErr.message) || (res && res.reached ? "request failed (" + (res.status || 0) + ")" : "network error"),
      requestId: (bodyErr && bodyErr.request_id) || (res && res.requestId) || null,
    };
  }

  /* ---- HTTP: one request, and full-block pagination ----------------------- */
  async function requestJson(fetchImpl, url) {
    try {
      var res = await fetchImpl(url);
      var requestId = null;
      try {
        if (res.headers && typeof res.headers.get === "function") requestId = res.headers.get("X-Request-ID");
      } catch (e) { /* headers not exposed */ }
      var body = null;
      try { body = await res.json(); } catch (e2) { body = null; }
      return { reached: true, ok: !!res.ok, status: res.status, body: body, requestId: requestId };
    } catch (err) {
      return { reached: false, ok: false, status: 0, body: null, requestId: null, error: String((err && err.message) || err) };
    }
  }

  // Walk `?limit=&offset=` pages using the real `total`, up to `cap`. A failed page
  // stops the walk and keeps prior items (never erases successes); exceeding `cap`
  // sets `truncated` (never a silent cut).
  async function fetchPaged(fetchImpl, base, path, opts) {
    opts = opts || {};
    var limit = opts.limit || PAGE_LIMIT;
    var cap = opts.cap || SNAPSHOT_ITEM_CAP;
    var items = [], offset = 0, total = 0;
    var reached = false, ok = true, error = null, truncated = false;
    while (true) {
      var sep = path.indexOf("?") >= 0 ? "&" : "?";
      var url = base + path + sep + "limit=" + limit + "&offset=" + offset;
      var res = await requestJson(fetchImpl, url);
      if (!res.reached) { ok = false; error = res; break; }
      reached = true;
      if (!res.ok) { ok = false; error = res; break; }
      var pageBody = res.body || {};
      var pageItems = Array.isArray(pageBody.items) ? pageBody.items : [];
      total = typeof pageBody.total === "number" ? pageBody.total : offset + pageItems.length;
      for (var i = 0; i < pageItems.length; i++) items.push(pageItems[i]);
      offset += limit;
      if (pageItems.length === 0) break;
      if (items.length >= total) break;
      if (items.length >= cap) { if (total > items.length) truncated = true; break; }
    }
    return { items: items, total: total, reached: reached, ok: ok, error: error, truncated: truncated };
  }

  /* ======================================================================== */
  /*  Wire -> types.ts field mappers (one per product type; every field named) */
  /* ======================================================================== */

  // -> CrisisRating (types.ts). The wire crisis-rating is already snake_case and
  // field-aligned with the type, so this is a defensive shallow copy, not a rename.
  function mapCrisisRating(w) {
    if (!w) return undefined;
    var out = {};
    for (var k in w) if (Object.prototype.hasOwnProperty.call(w, k)) out[k] = w[k];
    return out;
  }

  // -> DailySummary (types.ts) from /dashboard `summary`. modelRating is intentionally
  // omitted: the backend supplies only model_rating_prediction_id, not the full rating
  // (hence dailySummary is `partial`, never a fabricated rating).
  function mapDailySummary(w) {
    if (!w) return null;
    return {
      date: w.summary_date,                       // date-only, unshifted
      lastUpdatedAt: toNyDisplayIso(w.created_at),
      summary: w.summary || "",
      keyPoints: Array.isArray(w.key_points) ? w.key_points : [],
      overallRiskLevel: w.overall_risk_level || "low",
      confidenceScore: _num(w.confidence_score, 0),
    };
  }

  // -> DashboardMetric (types.ts). previousValue/change/changeDirection are omitted,
  // not invented: the backend computes no comparison window. severity is set only when
  // the backend marks it (the overall-risk metric).
  function mapMetric(w) {
    var m = { label: w.label, value: w.value };
    if (w.severity != null) m.severity = w.severity;
    return m;
  }

  // -> UpcomingTrigger (types.ts).
  function mapUpcomingTrigger(w) {
    return {
      id: w.id,
      title: w.title || "",
      expectedAt: toNyDisplayIso(w.expected_at),
      relatedEventId: w.related_event_id || undefined,
      relatedCompanyId: w.related_company_id || undefined, // null on the wire (no persisted link)
      importance: w.importance || "medium",
      reason: w.reason || "",
    };
  }

  // -> EventMapPoint (types.ts).
  function mapEventMapPoint(w) {
    return {
      locationName: w.location_name,
      latitude: _num(w.latitude, 0),
      longitude: _num(w.longitude, 0),
      x: _num(w.x, 0),
      y: _num(w.y, 0),
      eventCount: _num(w.event_count, 0),
      maxRiskScore: _num(w.max_risk_score, 0),
      dominantEventType: w.dominant_event_type || "",
      relatedEventIds: Array.isArray(w.related_event_ids) ? w.related_event_ids : [],
    };
  }

  // -> RiskDetail (types.ts) from /risk-radar/{risk_type}. model_rating and its derived
  // arrays may be null/empty while Gate G is closed; observation-backed fields still map.
  // signals & leadingIndicators remain [] because no persisted row carries their required fields.
  function mapRiskDetail(w, label) {
    return {
      riskType: label,
      score: _num(w.score, 0),
      severity: w.severity || "low",
      modelRating: w.model_rating ? mapCrisisRating(w.model_rating) : undefined,
      probabilityByHorizon: (Array.isArray(w.probability_by_horizon) ? w.probability_by_horizon : [])
        .map(function (p) { return { horizon: p.horizon, probability: p.probability }; }),
      mainDrivers: Array.isArray(w.main_drivers) ? w.main_drivers : [],
      signals: [],                 // no live source
      historicalComparisons: Array.isArray(w.historical_comparisons) ? w.historical_comparisons : [],
      leadingIndicators: [],       // no live source
      invalidationSignals: Array.isArray(w.invalidation_signals) ? w.invalidation_signals : [],
      relatedEventIds: Array.isArray(w.related_event_ids) ? w.related_event_ids : [],
      relatedIndustryIds: Array.isArray(w.related_industry_ids) ? w.related_industry_ids : [],
      relatedCompanyIds: Array.isArray(w.related_company_ids) ? w.related_company_ids : [],
    };
  }

  // The score at/just-before `nowMs - days` from a chronological real-history series;
  // returns null when no observation is old enough (delta never invented).
  function _deltaFromHistory(series, currentScore, nowMs, days) {
    if (!series.length || currentScore == null) return null;
    var cutoff = nowMs - days * 86400000;
    var past = null;
    for (var i = 0; i < series.length; i++) {
      if (series[i].t <= cutoff) past = series[i]; else break;
    }
    if (past == null) return null;
    return Math.round(currentScore - past.score);
  }

  // -> RiskRadarItem (types.ts). change24h/change7d come only from real history;
  // topDriver only from the real detail.main_drivers — both null/empty when absent.
  function mapRiskRadarItem(obs, detail, series, nowMs) {
    var score = _num(obs.score, 0);
    return {
      riskType: riskTypeLabel(obs.risk_type),
      score: score,
      level: obs.level || "low",
      change24h: _deltaFromHistory(series, score, nowMs, 1),
      change7d: _deltaFromHistory(series, score, nowMs, 7),
      topDriver: detail && Array.isArray(detail.main_drivers) && detail.main_drivers.length ? detail.main_drivers[0] : "",
      confidenceScore: _num(obs.confidence_score, 0),
    };
  }

  // -> IndustryHeatmapItem (types.ts) from /industries. The rollup persists no
  // direction and no display name distinct from industry_id; those become honest
  // "unknown"/id defaults (never pulled from fixtures), so industries is `partial`.
  function mapIndustry(w) {
    return {
      industryId: w.industry_id,
      industryName: w.industry_name || w.industry_id,
      impactScore: _num(w.impact_score, null),
      riskScore: _num(w.risk_score, null),
      opportunityScore: _num(w.opportunity_score, null),
      relatedEventCount: _num(w.related_event_count, 0),
      direction: "unknown",
      newsVelocityScore: _num(w.news_velocity_score, null),
      summary: w.summary || "",
    };
  }

  // -> CompanyImpactRow (types.ts) from /companies, enriched by the dashboard
  // company_ranking (same backend) when the company is ranked. Impact/risk fields
  // the list endpoint cannot supply become honest null/unknown defaults — never
  // pulled from unrelated fixtures — so companies is `partial`.
  function mapCompany(w, rank) {
    var r = rank || null;
    return {
      companyId: w.id,
      name: w.display_name || w.legal_name || "",
      ticker: w.primary_ticker || undefined,
      exchange: w.exchange || undefined,
      industry: w.industry || w.sector || "",
      country: w.country || undefined,
      impactDirection: r && r.impact_direction ? r.impact_direction : "unknown",
      impactScore: r ? _num(r.impact_score, null) : null,
      riskScore: r ? _num(r.risk_score, null) : null,
      relatedEventCount: r ? _num(r.related_event_count, 0) : 0,
      topDriver: r && r.top_driver ? r.top_driver : "",
      lastUpdatedAt: toNyDisplayIso((r && r.last_updated_at) || w.updated_at),
    };
  }

  // -> EventCard core (types.ts) from an expanded /events list item. Deep-analysis
  // tabs are left empty: a live event carries no live overview/timeline/debate/etc.,
  // and those are never fabricated (the synthetic fixture events supply the demo tabs).
  function mapEventListItem(w) {
    var companies = Array.isArray(w.companies) ? w.companies : [];
    var industries = Array.isArray(w.industries) ? w.industries : [];
    var locations = Array.isArray(w.locations) ? w.locations : [];
    return {
      id: w.id,
      title: w.title || "",
      summary: w.summary || "",
      eventType: w.event_type || "",
      eventTypes: w.event_type ? String(w.event_type).split(/\s*·\s*/).filter(Boolean) : [],
      status: w.status || "new",              // wire status already matches EventCard.status
      riskLevel: w.risk_level || "low",       // page-safe default when the event is unscored
      hotnessScore: _num(w.hotness_score, 0),
      riskScore: _num(w.risk_score, 0),
      confidenceScore: _num(w.confidence_score, 0),
      affectedIndustries: industries.map(function (i) { return i.industry_id; }),
      affectedCompanies: companies.map(function (c) {
        return { name: c.display_name, ticker: c.primary_ticker || undefined, impactDirection: c.impact_direction || "unknown" };
      }),
      locations: locations.map(function (l) { return l.location_name; }),
      primaryLocation: locations.length ? locations[0].location_name : undefined,
      sourceCount: _num(w.source_count, 0),
      articleCount: _num(w.article_count, 0),
      firstSeenAt: toNyDisplayIso(w.first_seen_at),
      lastUpdatedAt: toNyDisplayIso(w.last_seen_at || w.updated_at),
      whyItMatters: "",                        // no live source; not fabricated
    };
  }

  function _evidenceIdsFromRefs(refs) {
    if (!Array.isArray(refs)) return [];
    var out = [];
    for (var i = 0; i < refs.length; i++) {
      var r = refs[i];
      if (r == null) continue;
      if (typeof r === "string") out.push(r);
      else if (r.source_id) out.push(String(r.source_id));
      else if (r.id) out.push(String(r.id));
    }
    return out;
  }

  // -> Alert (types.ts). status is the ADR 0010 lifecycle state mapped to the UI triad.
  function mapAlert(w) {
    return {
      id: w.id,
      title: w.title || "",
      message: w.message || "",
      severity: w.severity || "low",
      riskScore: _num(w.risk_score, 0),
      alertType: w.alert_type || "",
      relatedEventId: w.related_event_id || undefined,
      relatedCompanyId: w.related_company_id || undefined,
      relatedIndustryId: w.related_industry_id || undefined,
      evidenceSourceIds: _evidenceIdsFromRefs(w.evidence_refs),
      status: mapAlertStatus(w.status || w.state),
      createdAt: toNyDisplayIso(w.created_at),
    };
  }

  // -> WatchlistItem (types.ts).
  function mapWatchlistItem(w) {
    return {
      id: w.id,
      itemType: w.item_type,
      label: w.label || "",
      metadata: w.metadata || undefined,
      alertEnabled: !!w.alert_enabled,
      createdAt: toNyDisplayIso(w.created_at),
    };
  }

  // -> PipelineJob (types.ts). The admin/jobs wire has no start/finish/duration/
  // throughput columns; those stay null/undefined (page-safe, item 7), not fabricated.
  function mapJob(w) {
    return {
      id: w.id,
      jobType: w.job_type || w.job_key || "",
      status: mapJobStatus(w.state),
      startedAt: null,
      finishedAt: null,
      durationMs: null,
      errorMessage: null,
    };
  }

  // -> SourceStatus (types.ts) from an admin/sources row + its embedded latest health.
  function mapSource(w) {
    var h = w.latest_health || null;
    return {
      sourceId: w.id,
      name: w.name || "",
      sourceType: w.source_type || "rss",
      status: h ? mapSourceStatus(h.status) : "degraded",
      lastFetchedAt: h ? toNyDisplayIso(h.checked_at) : undefined,
      articlesFetchedToday: h ? _num(h.items_fetched, undefined) : undefined,
      errorRate: h ? _num(h.error_rate, undefined) : undefined,
      averageLatencyMs: h ? _num(h.latency_ms, undefined) : undefined,
    };
  }

  // -> ModelUsageStats (types.ts) from one admin/models run row. requestCount /
  // successRate / schemaValidityRate / fallbackCount have no live aggregate source
  // and are null (never fabricated); latency/cost are the real per-run values.
  function mapModel(w) {
    return {
      provider: w.provider || "",
      modelName: w.model || "",
      taskType: w.prompt_name || "",
      requestCount: null,
      successRate: null,
      averageLatencyMs: _num(w.latency_ms, null),
      averageCostUsd: _num(w.cost_usd, null),
      schemaValidityRate: null,
      fallbackCount: null,
    };
  }

  // -> EvidenceSource (types.ts) from a real Evidence Drawer link (/evidence/{claim_id}).
  // relatedClaims is the real claim this link supports.
  function mapEvidenceLink(link, claimId) {
    return {
      id: String(link.evidence_item_id),
      sourceType: link.source_type,
      title: link.title || "",
      publisher: link.publisher || undefined,
      url: link.url || undefined,
      publishedAt: toNyDisplayIso(link.published_at),
      credibilityScore: link.credibility != null ? link.credibility : undefined,
      relatedClaims: claimId != null ? [String(claimId)] : [],
    };
  }

  /* ---- Fixture retention (synthetic content with no live source) ---------- */

  // Retain a fully-authored fixture event for the unsupported ask/query flows and
  // the deep-analysis-tab demo. Its deep fields have no live endpoint:
  //   // SYNTHETIC: event.modelDebate is fixture-backed (no model-debate endpoint).
  //   // SYNTHETIC: event.forecastScenarios are fixture-backed (no forecast endpoint).
  //   // SYNTHETIC: event.crisisRating / historicalAnalogies deep fields are fixture-backed.
  // Marked synthetic so it is never mislabeled live.
  function syntheticFixtureEvent(ev) {
    var copy = {};
    for (var k in ev) if (Object.prototype.hasOwnProperty.call(ev, k)) copy[k] = ev[k];
    return tagQuality(copy, DATA_QUALITY.SYNTHETIC);
  }

  // Deterministic synthetic 30-day curve. ONLY used in the full-demo fallback, where a
  // complete fixture requires a trend and no live history exists. Tagged synthetic.
  function syntheticSeries(seed, base, amp, drift) {
    // SYNTHETIC: fallback-only generated risk-trend curve (no live history endpoint reached).
    var out = [];
    for (var i = 0; i < 30; i++) {
      var n = Math.sin((i + seed) * 0.7) * amp + (Math.sin((i + seed) * 2.3) * amp) / 3;
      var v = base + n + drift * i;
      out.push(Math.max(2, Math.min(98, Math.round(v))));
    }
    return out;
  }

  /* ---- Root assembly ------------------------------------------------------ */

  // The flattened, page-facing field order (preserved exactly from the retired data.js).
  var ROOT_BLOCK_FIELDS = [
    "dailySummary", "metrics", "upcomingTriggers", "eventMap",
    "riskRadar", "riskDetails", "industries", "companies", "companyProfiles",
    "evidence", "events", "alerts", "watchlist",
    "adminJobs", "adminSources", "adminModels", "askSuggestions", "riskTrends",
  ];

  // Build the final window.DATA object: every page-facing field exists, each block
  // carries a non-enumerable dataQuality tag, and the adapter metadata (blockQuality
  // registry, runtime, apiErrors) is attached without wrapping/retyping the blocks.
  function assembleRoot(blocks, meta) {
    var registry = meta.registry || {};
    // Derived lookups mirror their source block's quality.
    var evidence = blocks.evidence || [];
    var events = blocks.events || [];
    var evidenceById = {};
    for (var i = 0; i < evidence.length; i++) evidenceById[evidence[i].id] = evidence[i];
    var eventsById = {};
    for (var j = 0; j < events.length; j++) eventsById[events[j].id] = events[j];

    var root = {
      NOW: meta.now,
      conf: conf,
      dailySummary: blocks.dailySummary != null ? blocks.dailySummary : null,
      metrics: blocks.metrics || [],
      upcomingTriggers: blocks.upcomingTriggers || [],
      eventMap: blocks.eventMap || [],
      riskRadar: blocks.riskRadar || [],
      riskDetails: blocks.riskDetails || {},
      industries: blocks.industries || [],
      companies: blocks.companies || [],
      companyProfiles: blocks.companyProfiles || {},
      evidence: evidence,
      evidenceById: evidenceById,
      events: events,
      eventsById: eventsById,
      alerts: blocks.alerts || [],
      watchlist: blocks.watchlist || [],
      adminJobs: blocks.adminJobs || [],
      adminSources: blocks.adminSources || [],
      adminModels: blocks.adminModels || [],
      askSuggestions: blocks.askSuggestions || [],
      riskTrends: blocks.riskTrends || {},
      // ---- adapter metadata (item 7) ----
      blockQuality: registry,
      runtime: meta.runtime,
      apiErrors: meta.apiErrors || [],
    };

    // Tag each block + derived lookup with its registry quality (derived entries skip).
    for (var b = 0; b < ROOT_BLOCK_FIELDS.length; b++) {
      var name = ROOT_BLOCK_FIELDS[b];
      var q = registry[name] && registry[name].quality;
      if (q) tagQuality(root[name], q);
    }
    var evQ = registry.evidence && registry.evidence.quality;
    if (evQ) { tagQuality(root.evidenceById, evQ); }
    var evtQ = registry.events && registry.events.quality;
    if (evtQ) { tagQuality(root.eventsById, evtQ); }
    // NOW / conf are derived helpers, not content blocks (recorded, not boxed).
    registry.NOW = { kind: "derived" };
    registry.conf = { kind: "derived" };
    registry.evidenceById = registry.evidence || { quality: DATA_QUALITY.EMPTY };
    registry.eventsById = registry.events || { quality: DATA_QUALITY.EMPTY };
    return root;
  }

  function resolveNow(nowInput, core, convert) {
    var basis = nowInput || (core && core.meta && core.meta.now) || new Date().toISOString();
    return convert ? toNyDisplayIso(basis) : basis;
  }

  /* ======================================================================== */
  /*  Fixture fallback: full demo snapshot (API totally unreachable)          */
  /* ======================================================================== */
  function buildFixtureSnapshot(core, events, meta) {
    core = core || {};
    events = events || [];
    meta = meta || {};
    var dash = core.dashboard || {};
    var risk = core.risk || {};
    var admin = core.admin || {};
    var radar = Array.isArray(risk.riskRadar) ? risk.riskRadar : [];

    var riskTrends = {};
    for (var i = 0; i < radar.length; i++) {
      // SYNTHETIC: fallback-only generated 30-day risk trend (demo mode, no live history).
      riskTrends[radar[i].riskType] = syntheticSeries(i + 1, _num(radar[i].score, 50), 4, 0.2);
    }

    var blocks = {
      dailySummary: dash.dailySummary || null,
      metrics: dash.metrics || [],
      upcomingTriggers: dash.upcomingTriggers || [],
      eventMap: dash.eventMap || [],
      riskRadar: radar,
      riskDetails: risk.riskDetails || {},
      industries: core.industries || [],
      companies: core.companies || [],
      companyProfiles: core.companyProfiles || {},
      evidence: (core.evidence || []).slice(),
      events: events.map(syntheticFixtureEvent),
      alerts: core.alerts || [],
      watchlist: core.watchlist || [],
      adminJobs: admin.jobs || [],
      adminSources: admin.sources || [],
      adminModels: admin.models || [],
      // SYNTHETIC: ask/query suggestions + canned answers are fixture-backed (no ask endpoint).
      askSuggestions: (core.ask && core.ask.suggestions) || [],
      riskTrends: riskTrends,
    };

    // Everything is demo data: every block is synthetic.
    var registry = {};
    for (var k = 0; k < ROOT_BLOCK_FIELDS.length; k++) registry[ROOT_BLOCK_FIELDS[k]] = { quality: DATA_QUALITY.SYNTHETIC };
    var now = resolveNow(meta.now, core, false); // demo timestamps stay as authored in the fixtures

    return assembleRoot(blocks, {
      now: now,
      registry: registry,
      apiErrors: [],
      runtime: {
        mode: "demo",
        apiBase: _normBase(meta.apiBase),
        now: now,
        degraded: true,
        reason: meta.reason || "api unreachable — full fixture fallback",
        truncated: [],
      },
    });
  }

  /* ======================================================================== */
  /*  Company-research enrichment (batching-bug fix)                          */
  /* ======================================================================== */
  function companyCik(company) {
    return company.cik || company.secCik || company.primaryCik ||
      (company.identifiers && company.identifiers.cik) || "";
  }

  function profileMatchesCompany(profile, company) {
    var identity = (profile && profile.identity) || {};
    var profileTicker = normalizeTicker(identity.ticker);
    var companyTicker = normalizeTicker(company.ticker);
    var profileCik = normalizeCik(identity.cik);
    var currentCik = normalizeCik(companyCik(company));
    var profileName = normalizeName(identity.name);
    var companyName = normalizeName(company.name);
    var companyId = normalizeName(company.companyId);
    return (
      (profileTicker && companyTicker && profileTicker === companyTicker) ||
      (profileCik && currentCik && profileCik === currentCik) ||
      (profileName && (profileName === companyName || profileName === companyId))
    );
  }

  // One company-research request, with the alias fallback on 404 (primary route ->
  // provider-data route). Returns the profile items; throws on a real failure so the
  // caller records a partial (companies are never erased by an enrichment failure).
  async function fetchCompanyResearchBatch(fetchImpl, base, kind, values) {
    var params = new URLSearchParams();
    params.set(kind, values.join(","));
    var query = params.toString();
    var primary = base + "/api/v1/company-research/profiles?" + query;
    var alias = base + "/api/v1/provider-data/company-research/profiles?" + query;
    var res = await fetchImpl(primary);
    if (res.status === 404) res = await fetchImpl(alias);
    if (!res.ok) throw new Error("company research profiles failed (" + res.status + ")");
    var body = await res.json();
    return Array.isArray(body.items) ? body.items : [];
  }

  // FIX (ADR 0007 / data.js batching bug): tickers, CIKs and company IDs are chunked
  // and requested in INDEPENDENT per-category loops — each category paged by its own
  // index. The old shared-index slice coupled unrelated-length lists; here every
  // identifier of every category is requested exactly once, with no omissions.
  async function enrichCompanyProfiles(companies, base, fetchImpl) {
    var tickers = unique(companies.map(function (c) { return normalizeTicker(c.ticker); }));
    var ciks = unique(companies.map(function (c) { return normalizeCik(companyCik(c)); }));
    var companyIds = unique(companies.map(function (c) { return normalizeId(c.companyId); }));

    var profiles = {}, errors = [], requested = { tickers: [], ciks: [], company_ids: [] };

    async function runCategory(wireKey, statKey, values) {
      var groups = chunk(values, COMPANY_BATCH_SIZE);
      for (var i = 0; i < groups.length; i++) {
        var group = groups[i];
        if (!group.length) continue;
        for (var v = 0; v < group.length; v++) requested[statKey].push(group[v]);
        try {
          var items = await fetchCompanyResearchBatch(fetchImpl, base, wireKey, group);
          for (var p = 0; p < items.length; p++) {
            var company = companies.find(function (co) { return profileMatchesCompany(items[p], co); });
            if (company) profiles[company.companyId] = items[p];
          }
        } catch (err) {
          errors.push(apiError("/api/v1/company-research/profiles?" + wireKey, { reached: true, ok: false, status: 0 }));
        }
      }
    }

    await runCategory("tickers", "tickers", tickers);
    await runCategory("ciks", "ciks", ciks);
    await runCategory("company_ids", "company_ids", companyIds);
    return { profiles: profiles, errors: errors, requested: requested };
  }

  /* ======================================================================== */
  /*  Evidence assembly from the latest daily brief                          */
  /* ======================================================================== */
  function collectClaimRefs(report) {
    var refs = [], seen = {};
    var sections = report && Array.isArray(report.sections) ? report.sections : [];
    for (var s = 0; s < sections.length; s++) {
      var er = Array.isArray(sections[s].evidence_refs) ? sections[s].evidence_refs : [];
      for (var i = 0; i < er.length; i++) {
        var key = String(er[i]);
        if (!seen[key]) { seen[key] = 1; refs.push(key); if (refs.length >= EVIDENCE_CLAIM_CAP) return refs; }
      }
    }
    return refs;
  }

  // Fetch each claim's evidence links independently (settled) and fold into unique
  // EvidenceSource items (relatedClaims unioned). Real drawer links only.
  async function loadRealEvidence(fetchImpl, base, report) {
    var claimIds = collectClaimRefs(report);
    var byId = {}, order = [];
    var results = await Promise.all(claimIds.map(function (cid) {
      return requestJson(fetchImpl, base + API_PREFIX + "/evidence/" + encodeURIComponent(cid))
        .then(function (r) { return { cid: cid, r: r }; });
    }));
    for (var i = 0; i < results.length; i++) {
      var cid = results[i].cid, r = results[i].r;
      if (!r.reached || !r.ok || !r.body) continue;
      var links = Array.isArray(r.body.evidence) ? r.body.evidence : [];
      for (var l = 0; l < links.length; l++) {
        var mapped = mapEvidenceLink(links[l], cid);
        if (byId[mapped.id]) {
          if (mapped.relatedClaims[0] && byId[mapped.id].relatedClaims.indexOf(mapped.relatedClaims[0]) < 0) {
            byId[mapped.id].relatedClaims.push(mapped.relatedClaims[0]);
          }
        } else { byId[mapped.id] = mapped; order.push(mapped.id); }
      }
    }
    return order.map(function (id) { return byId[id]; });
  }

  /* ======================================================================== */
  /*  Reachable snapshot: independent settled requests, merged block by block */
  /* ======================================================================== */
  async function loadSnapshot(options) {
    options = options || {};
    var fixtures = options.fixtures || {};
    var core = fixtures.core || {};
    var events = fixtures.events || [];
    var base = _normBase(options.apiBase);
    var fetchImpl = options.fetch || (typeof fetch !== "undefined" ? fetch : null);

    if (!fetchImpl) {
      return buildFixtureSnapshot(core, events, { apiBase: base, now: options.now, reason: "no fetch implementation" });
    }

    var nowIso = resolveNow(options.now, core, true);
    var nowMs = new Date(options.now || (core.meta && core.meta.now) || Date.now()).getTime();
    if (isNaN(nowMs)) nowMs = Date.now();

    var apiErrors = [];
    var registry = {};
    var truncations = [];

    function record(name, res, endpoint, baseQuality) {
      // Decide a block's quality from reachability/http/emptiness + its live/partial base.
      if (!res.reached || !res.ok) {
        var err = apiError(endpoint, res.error || res);
        apiErrors.push(err);
        registry[name] = { quality: DATA_QUALITY.EMPTY, error: err };
        return DATA_QUALITY.EMPTY;
      }
      if (res.truncated) {
        truncations.push({ endpoint: endpoint, total: res.total, shown: res.items.length });
        apiErrors.push({ endpoint: endpoint, code: "truncated", message: "showing " + res.items.length + " of " + res.total, requestId: null });
        registry[name] = { quality: DATA_QUALITY.PARTIAL, truncated: true, total: res.total, shown: res.items.length };
        return DATA_QUALITY.PARTIAL;
      }
      var count = res.items ? res.items.length : 0;
      var quality = count === 0 ? DATA_QUALITY.EMPTY : baseQuality;
      registry[name] = { quality: quality };
      return quality;
    }

    // ---- Fire the independent top-level requests concurrently (settled) ----
    var pDashboard = requestJson(fetchImpl, base + API_PREFIX + "/dashboard");
    var pEvents = fetchPaged(fetchImpl, base, API_PREFIX + "/events");
    var pRadar = fetchPaged(fetchImpl, base, API_PREFIX + "/risk-radar");
    var pIndustries = fetchPaged(fetchImpl, base, API_PREFIX + "/industries");
    var pCompanies = fetchPaged(fetchImpl, base, API_PREFIX + "/companies");
    var pAlerts = fetchPaged(fetchImpl, base, API_PREFIX + "/alerts");
    var pWatchlist = fetchPaged(fetchImpl, base, API_PREFIX + "/watchlist");
    var pJobs = fetchPaged(fetchImpl, base, API_PREFIX + "/admin/jobs");
    var pSources = fetchPaged(fetchImpl, base, API_PREFIX + "/admin/sources");
    var pModels = fetchPaged(fetchImpl, base, API_PREFIX + "/admin/models");
    var pBrief = requestJson(fetchImpl, base + API_PREFIX + "/reports/daily-brief/latest");

    var dashboardRes = await pDashboard;
    var eventsRes = await pEvents;
    var radarRes = await pRadar;
    var industriesRes = await pIndustries;
    var companiesRes = await pCompanies;
    var alertsRes = await pAlerts;
    var watchlistRes = await pWatchlist;
    var jobsRes = await pJobs;
    var sourcesRes = await pSources;
    var modelsRes = await pModels;
    var briefRes = await pBrief;

    // Total-unreachable detection: no request produced ANY HTTP response -> demo.
    var reachedAny = dashboardRes.reached || eventsRes.reached || radarRes.reached ||
      industriesRes.reached || companiesRes.reached || alertsRes.reached ||
      watchlistRes.reached || jobsRes.reached || sourcesRes.reached ||
      modelsRes.reached || briefRes.reached;
    if (!reachedAny) {
      return buildFixtureSnapshot(core, events, { apiBase: base, now: options.now, reason: "api unreachable" });
    }

    /* ---- Dashboard block (summary/metrics/triggers/map/company_ranking) ---- */
    var dashBody = dashboardRes.reached && dashboardRes.ok ? (dashboardRes.body || {}) : null;
    var rankingById = {};
    var blocks = {};
    if (dashBody) {
      registry.metrics = { quality: DATA_QUALITY.LIVE };
      registry.upcomingTriggers = { quality: DATA_QUALITY.LIVE };
      registry.eventMap = { quality: DATA_QUALITY.LIVE };
      // dailySummary lacks a live modelRating -> partial (or empty when absent).
      registry.dailySummary = { quality: dashBody.summary ? DATA_QUALITY.PARTIAL : DATA_QUALITY.EMPTY };
      blocks.dailySummary = mapDailySummary(dashBody.summary);
      blocks.metrics = (Array.isArray(dashBody.metrics) ? dashBody.metrics : []).map(mapMetric);
      blocks.upcomingTriggers = (Array.isArray(dashBody.upcoming_triggers) ? dashBody.upcoming_triggers : []).map(mapUpcomingTrigger);
      blocks.eventMap = (Array.isArray(dashBody.event_map) ? dashBody.event_map : []).map(mapEventMapPoint);
      var ranking = Array.isArray(dashBody.company_ranking) ? dashBody.company_ranking : [];
      for (var ri = 0; ri < ranking.length; ri++) rankingById[ranking[ri].company_id] = ranking[ri];
    } else {
      var dErr = apiError(API_PREFIX + "/dashboard", dashboardRes);
      apiErrors.push(dErr);
      registry.dailySummary = { quality: DATA_QUALITY.EMPTY, error: dErr };
      registry.metrics = { quality: DATA_QUALITY.EMPTY, error: dErr };
      registry.upcomingTriggers = { quality: DATA_QUALITY.EMPTY, error: dErr };
      registry.eventMap = { quality: DATA_QUALITY.EMPTY, error: dErr };
      blocks.dailySummary = null; blocks.metrics = []; blocks.upcomingTriggers = []; blocks.eventMap = [];
    }

    /* ---- Risk radar: dedupe per risk_type, then detail + 30-day history ---- */
    var radarQuality = record("riskRadar", radarRes, API_PREFIX + "/risk-radar", DATA_QUALITY.PARTIAL);
    var radarByType = {}; // one observation per risk_type (latest as_of wins)
    if (radarRes.ok) {
      for (var ro = 0; ro < radarRes.items.length; ro++) {
        var obs = radarRes.items[ro];
        var prev = radarByType[obs.risk_type];
        if (!prev || String(obs.as_of || "") > String(prev.as_of || "")) radarByType[obs.risk_type] = obs;
      }
    }
    var riskTypes = Object.keys(radarByType);
    var detailResults = await Promise.all(riskTypes.map(function (rt) {
      var seg = encodeURIComponent(rt);
      return Promise.all([
        requestJson(fetchImpl, base + API_PREFIX + "/risk-radar/" + seg),
        fetchPaged(fetchImpl, base, API_PREFIX + "/risk-radar/" + seg + "/history?days=" + RISK_HISTORY_DAYS, { cap: 400 }),
      ]).then(function (pair) { return { rt: rt, detail: pair[0], history: pair[1] }; });
    }));

    var riskRadar = [], riskDetails = {}, riskTrends = {};
    var anyHistory = false, allDeltas = riskTypes.length > 0, historyReached = false;
    for (var d = 0; d < detailResults.length; d++) {
      var rt = detailResults[d].rt;
      var label = riskTypeLabel(rt);
      var detailRes = detailResults[d].detail;
      var historyRes = detailResults[d].history;
      var detailBody = detailRes.reached && detailRes.ok && detailRes.body ? detailRes.body.risk : null;

      var series = [];
      if (historyRes.reached) historyReached = true;
      if (historyRes.ok) {
        for (var h = 0; h < historyRes.items.length; h++) {
          var o = historyRes.items[h];
          var t = new Date(o.as_of).getTime();
          if (!isNaN(t)) series.push({ t: t, score: _num(o.score, 0) });
        }
        series.sort(function (a, b) { return a.t - b.t; });
      }
      if (series.length) anyHistory = true;

      var item = mapRiskRadarItem(radarByType[rt], detailBody, series, nowMs);
      riskRadar.push(item);
      if (item.change24h == null || item.change7d == null) allDeltas = false;
      if (detailBody) riskDetails[label] = mapRiskDetail(detailBody, label);
      // Reachable mode: real scores in chronological order, or an empty series —
      // NEVER a synthetic curve.
      riskTrends[label] = series.map(function (p) { return p.score; });
    }
    blocks.riskRadar = riskRadar;
    blocks.riskDetails = riskDetails;
    blocks.riskTrends = riskTrends;
    // riskRadar is live only when every rendered delta is a real number.
    if (radarQuality !== DATA_QUALITY.EMPTY) {
      registry.riskRadar = { quality: allDeltas ? DATA_QUALITY.LIVE : DATA_QUALITY.PARTIAL };
    }
    registry.riskDetails = { quality: Object.keys(riskDetails).length ? DATA_QUALITY.PARTIAL : DATA_QUALITY.EMPTY };
    registry.riskTrends = { quality: anyHistory ? DATA_QUALITY.PARTIAL : DATA_QUALITY.EMPTY };
    if (riskTypes.length && !historyReached) {
      apiErrors.push(apiError(API_PREFIX + "/risk-radar/{risk_type}/history", { reached: false, ok: false }));
    }

    /* ---- Industries / companies ------------------------------------------ */
    record("industries", industriesRes, API_PREFIX + "/industries", DATA_QUALITY.PARTIAL);
    blocks.industries = (industriesRes.ok ? industriesRes.items : []).map(mapIndustry);

    record("companies", companiesRes, API_PREFIX + "/companies", DATA_QUALITY.PARTIAL);
    var mappedCompanies = (companiesRes.ok ? companiesRes.items : []).map(function (c) { return mapCompany(c, rankingById[c.id]); });
    blocks.companies = mappedCompanies;

    /* ---- Alerts / watchlist ---------------------------------------------- */
    record("alerts", alertsRes, API_PREFIX + "/alerts", DATA_QUALITY.LIVE);
    blocks.alerts = (alertsRes.ok ? alertsRes.items : []).map(mapAlert);

    record("watchlist", watchlistRes, API_PREFIX + "/watchlist", DATA_QUALITY.LIVE);
    blocks.watchlist = (watchlistRes.ok ? watchlistRes.items : []).map(mapWatchlistItem);

    /* ---- Admin ------------------------------------------------------------ */
    record("adminJobs", jobsRes, API_PREFIX + "/admin/jobs", DATA_QUALITY.PARTIAL);
    blocks.adminJobs = (jobsRes.ok ? jobsRes.items : []).map(mapJob);

    record("adminSources", sourcesRes, API_PREFIX + "/admin/sources", DATA_QUALITY.PARTIAL);
    blocks.adminSources = (sourcesRes.ok ? sourcesRes.items : []).map(mapSource);

    record("adminModels", modelsRes, API_PREFIX + "/admin/models", DATA_QUALITY.PARTIAL);
    blocks.adminModels = (modelsRes.ok ? modelsRes.items : []).map(mapModel);

    /* ---- Company-research enrichment (batching-bug fix) ------------------- */
    blocks.companyProfiles = {};
    if (mappedCompanies.length) {
      var enrich = await enrichCompanyProfiles(mappedCompanies, base, fetchImpl);
      blocks.companyProfiles = enrich.profiles;
      for (var e = 0; e < enrich.errors.length; e++) apiErrors.push(enrich.errors[e]);
      var profileCount = Object.keys(enrich.profiles).length;
      registry.companyProfiles = {
        quality: enrich.errors.length ? DATA_QUALITY.PARTIAL : (profileCount ? DATA_QUALITY.LIVE : DATA_QUALITY.EMPTY),
      };
    } else {
      registry.companyProfiles = { quality: DATA_QUALITY.EMPTY };
    }

    /* ---- Evidence: real drawer links + retained fixture evidence ---------- */
    var realEvidence = [];
    if (briefRes.reached && briefRes.ok && briefRes.body && briefRes.body.report) {
      realEvidence = await loadRealEvidence(fetchImpl, base, briefRes.body.report);
    } else if (briefRes.reached && briefRes.status !== 404) {
      apiErrors.push(apiError(API_PREFIX + "/reports/daily-brief/latest", briefRes));
    } // a 404 latest brief is an honest empty evidence source, not an error/demo fallback.

    // Retain fixture evidence — it backs the synthetic fixture events' drawers. Because
    // fixture (synthetic) evidence is present, the block is `partial`, never `live`.
    var fixtureEvidence = Array.isArray(core.evidence) ? core.evidence : [];
    var mergedEvidence = [], evSeen = {};
    for (var re = 0; re < realEvidence.length; re++) { mergedEvidence.push(realEvidence[re]); evSeen[realEvidence[re].id] = 1; }
    for (var fe = 0; fe < fixtureEvidence.length; fe++) {
      if (!evSeen[fixtureEvidence[fe].id]) { mergedEvidence.push(fixtureEvidence[fe]); evSeen[fixtureEvidence[fe].id] = 1; }
    }
    blocks.evidence = mergedEvidence;
    registry.evidence = { quality: mergedEvidence.length ? DATA_QUALITY.PARTIAL : DATA_QUALITY.EMPTY };

    /* ---- Events: live cores + retained synthetic fixture events ----------- */
    var liveEvents = (eventsRes.ok ? eventsRes.items : []).map(mapEventListItem);
    if (!eventsRes.reached || !eventsRes.ok) {
      apiErrors.push(apiError(API_PREFIX + "/events", eventsRes));
    } else if (eventsRes.truncated) {
      truncations.push({ endpoint: API_PREFIX + "/events", total: eventsRes.total, shown: eventsRes.items.length });
      apiErrors.push({ endpoint: API_PREFIX + "/events", code: "truncated", message: "showing " + eventsRes.items.length + " of " + eventsRes.total, requestId: null });
    }
    var mergedEvents = [], evtSeen = {};
    for (var le = 0; le < liveEvents.length; le++) { mergedEvents.push(liveEvents[le]); evtSeen[liveEvents[le].id] = 1; }
    for (var se = 0; se < events.length; se++) {
      if (!evtSeen[events[se].id]) mergedEvents.push(syntheticFixtureEvent(events[se]));
    }
    blocks.events = mergedEvents;
    // Merged synthetic fixture events keep this block `partial`, never mislabeled live.
    registry.events = { quality: mergedEvents.length ? DATA_QUALITY.PARTIAL : DATA_QUALITY.EMPTY };

    /* ---- Ask suggestions: fixture-backed, no endpoint -------------------- */
    // SYNTHETIC: ask/query suggestions + canned answers are fixture-backed (no ask endpoint).
    blocks.askSuggestions = (core.ask && core.ask.suggestions) || [];
    registry.askSuggestions = { quality: DATA_QUALITY.SYNTHETIC };

    var degraded = apiErrors.length > 0;
    return assembleRoot(blocks, {
      now: nowIso,
      registry: registry,
      apiErrors: apiErrors,
      runtime: {
        mode: "api",
        apiBase: base,
        now: nowIso,
        degraded: degraded,
        truncated: truncations,
      },
    });
  }

  /* ---- Public surface ----------------------------------------------------- */
  var SignalApiAdapter = {
    DEFAULT_API_BASE: DEFAULT_API_BASE,
    DATA_QUALITY: DATA_QUALITY,
    // entry points
    loadSnapshot: loadSnapshot,
    buildFixtureSnapshot: buildFixtureSnapshot,
    // pure helpers / mappers (exposed for item 6B + tests)
    conf: conf,
    toNyDisplayIso: toNyDisplayIso,
    riskTypeLabel: riskTypeLabel,
    mapAlertStatus: mapAlertStatus,
    mapJobStatus: mapJobStatus,
    mapSourceStatus: mapSourceStatus,
    mapDailySummary: mapDailySummary,
    mapMetric: mapMetric,
    mapUpcomingTrigger: mapUpcomingTrigger,
    mapEventMapPoint: mapEventMapPoint,
    mapRiskDetail: mapRiskDetail,
    mapRiskRadarItem: mapRiskRadarItem,
    mapCrisisRating: mapCrisisRating,
    mapIndustry: mapIndustry,
    mapCompany: mapCompany,
    mapEventListItem: mapEventListItem,
    mapAlert: mapAlert,
    mapWatchlistItem: mapWatchlistItem,
    mapJob: mapJob,
    mapSource: mapSource,
    mapModel: mapModel,
    mapEvidenceLink: mapEvidenceLink,
    // company-research batching
    enrichCompanyProfiles: enrichCompanyProfiles,
    profileMatchesCompany: profileMatchesCompany,
    // mapping tables
    ALERT_STATUS_MAP: ALERT_STATUS_MAP,
    RISK_TYPE_LABELS: RISK_TYPE_LABELS,
    JOB_STATUS_MAP: JOB_STATUS_MAP,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = SignalApiAdapter;
  if (globalRoot) globalRoot.SignalApiAdapter = SignalApiAdapter;
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this));
