/* ============================================================================
   SIGNAL — data-quality presentation helpers (Stage 7, item 7)

   Pure, side-effect-free functions the UI uses to turn the adapter's metadata
   (window.DATA.blockQuality / runtime / apiErrors and the non-enumerable
   per-block dataQuality tag) into user-facing badges, the demo banner and the
   degraded surface — plus defensive value formatters so a null/absent number is
   rendered as an honest dash instead of a crash or a fabricated zero.

   This file knows NOTHING about backend endpoints, wire keys, pagination or
   error envelopes — it only reads the already-mapped, page-facing metadata the
   adapter attaches. The wire boundary stays entirely inside app/api-adapter.js.

   Zero-build: a browser global (window.SignalDataQuality) and a CommonJS export
   for `node --test`. No bundler / build step. Mirrors the api-adapter pattern.
   ========================================================================== */
(function (root) {
  "use strict";

  var DASH = "—";
  var QUALITY = { LIVE: "live", PARTIAL: "partial", SYNTHETIC: "synthetic", EMPTY: "empty" };

  // Non-live severity ranking used when one badge must summarize several blocks
  // (a failed/empty block outranks a partial, which outranks sample/synthetic).
  var SEVERITY = { live: 0, synthetic: 1, partial: 2, empty: 3 };

  // Human-facing presentation for a non-live quality. LIVE (and any unknown
  // value) returns null so a healthy block renders NO badge — no badge noise.
  function qualityMeta(quality) {
    if (quality === QUALITY.PARTIAL) {
      return { quality: quality, label: "Partial", tone: "med",
        title: "Some live data for this section could not be loaded. The results that did load are shown." };
    }
    if (quality === QUALITY.SYNTHETIC) {
      return { quality: quality, label: "Sample", tone: "neutral",
        title: "Illustrative sample content — not a live backend result." };
    }
    if (quality === QUALITY.EMPTY) {
      return { quality: quality, label: "No data", tone: "neutral",
        title: "No live data is available for this section yet." };
    }
    return null; // live / unknown -> no badge
  }

  function isNonLive(quality) {
    return quality === QUALITY.PARTIAL || quality === QUALITY.SYNTHETIC || quality === QUALITY.EMPTY;
  }

  // Resolve a block's quality: the blockQuality registry first, then the block's
  // own non-enumerable dataQuality tag. Returns "" (treated as live) when unknown.
  function blockQuality(data, name) {
    if (!data) return "";
    var reg = data.blockQuality && data.blockQuality[name];
    if (reg && reg.quality) return reg.quality;
    var block = data[name];
    if (block && typeof block === "object" && block.dataQuality) return block.dataQuality;
    return "";
  }

  // Summarize several blocks to the single most-concerning non-live quality
  // (empty > partial > synthetic). Returns "" when every block is live/unknown.
  function aggregateQuality(data, names) {
    var worst = "", worstRank = 0;
    names = names || [];
    for (var i = 0; i < names.length; i++) {
      var q = blockQuality(data, names[i]);
      if (!isNonLive(q)) continue;
      var rank = SEVERITY[q] || 0;
      if (rank > worstRank) { worstRank = rank; worst = q; }
    }
    return worst;
  }

  // Whole-snapshot demo fallback (the API was wholly unreachable).
  function isDemo(data) {
    return !!(data && data.runtime && data.runtime.mode === "demo");
  }

  // Reachable but degraded: API mode with recorded errors or the degraded flag.
  // Demo mode is deliberately NOT "degraded" here — it shows the demo banner.
  function isDegraded(data) {
    if (!data || isDemo(data)) return false;
    var rt = data.runtime || {};
    var errs = data.apiErrors || [];
    return !!rt.degraded || errs.length > 0;
  }

  // Every distinct, non-null request id across the recorded errors. Never
  // fabricated — only ids the adapter actually captured are returned.
  function requestIds(data) {
    var errs = (data && data.apiErrors) || [];
    var seen = {}, out = [];
    for (var i = 0; i < errs.length; i++) {
      var id = errs[i] && errs[i].requestId;
      if (id && !seen[id]) { seen[id] = 1; out.push(id); }
    }
    return out;
  }

  // De-duplicated, capped error summaries (message/code + first request id seen)
  // so an error storm can never flood the surface. Endpoints are intentionally
  // NOT exposed to the UI layer.
  function errorSummaries(data, cap) {
    var errs = (data && data.apiErrors) || [];
    cap = cap || 8;
    var seen = {}, out = [];
    for (var i = 0; i < errs.length && out.length < cap; i++) {
      var e = errs[i] || {};
      var key = (e.code || "") + "|" + (e.message || "");
      if (seen[key]) continue;
      seen[key] = 1;
      out.push({
        code: e.code || "",
        message: e.message || e.code || "request failed",
        requestId: e.requestId || null,
      });
    }
    return out;
  }

  /* ---- Defensive value formatting: null/absent -> dash, never a fake zero --- */
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function numOr(v, dash) { return isNum(v) ? v : (dash == null ? DASH : dash); }
  function intOr(v, dash) { return isNum(v) ? Math.round(v).toLocaleString() : (dash == null ? DASH : dash); }
  function moneyOr(v, digits, dash) { return isNum(v) ? "$" + v.toFixed(digits == null ? 2 : digits) : (dash == null ? DASH : dash); }
  function unitOr(v, unit, dash) { return isNum(v) ? "" + v + (unit || "") : (dash == null ? DASH : dash); }
  function pctOr(v, dash) { return isNum(v) ? Math.round(v) + "%" : (dash == null ? DASH : dash); }

  // Canonical horizon wire tokens -> friendly labels. Anything already friendly
  // (or an unknown value) passes through unchanged — never blanked or invented.
  var HORIZONS = { "0_6m": "0–6m", "6_12m": "6–12m", "12_18m": "12–18m", "within_18m": "≤18m" };
  function horizonLabel(token) {
    if (token == null) return DASH;
    return HORIZONS[token] || String(token);
  }

  var api = {
    DASH: DASH,
    QUALITY: QUALITY,
    qualityMeta: qualityMeta,
    isNonLive: isNonLive,
    blockQuality: blockQuality,
    aggregateQuality: aggregateQuality,
    isDemo: isDemo,
    isDegraded: isDegraded,
    requestIds: requestIds,
    errorSummaries: errorSummaries,
    numOr: numOr,
    intOr: intOr,
    moneyOr: moneyOr,
    unitOr: unitOr,
    pctOr: pctOr,
    horizonLabel: horizonLabel,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.SignalDataQuality = api;
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this));
