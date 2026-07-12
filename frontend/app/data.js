/* ============================================================================
   SIGNAL — data loader
   Fetches the formatted fixtures in /data/*.json (which conform to the schema
   in app/types.ts), then assembles window.DATA and rebuilds derived lookups
   (evidenceById, eventsById) and the synthetic 30-day risk-trend series.
   Signals readiness via window.__signalDataReady (Promise) and a
   "signal:data-ready" event so the React app mounts only once data is present.
   ========================================================================== */
(function () {
  "use strict";

  // numeric confidence → label (0.70+ High, 0.40+ Medium, else Low)
  const conf = (s) => (s >= 0.7 ? "High" : s >= 0.4 ? "Medium" : "Low");

  // deterministic synthetic trend series (last 30 days, clamped 2..98)
  function series(seed, base, amp, drift) {
    const out = []; let v = base;
    for (let i = 0; i < 30; i++) {
      const n = Math.sin((i + seed) * 0.7) * amp + (Math.sin((i + seed) * 2.3) * amp) / 3;
      v = base + n + drift * i;
      out.push(Math.max(2, Math.min(98, Math.round(v))));
    }
    return out;
  }

  function assemble(core, events) {
    const evidence = core.evidence;
    const evidenceById = Object.fromEntries(evidence.map((e) => [e.id, e]));
    const eventsById = Object.fromEntries(events.map((e) => [e.id, e]));

    const riskTrends = {
      "Macro Risk": series(1, 52, 4, 0.2),
      "Financial Stress": series(3, 33, 5, 0.27),
      "Geopolitical Risk": series(5, 64, 6, 0.45),
      "Supply Chain Risk": series(2, 57, 4, 0.23),
      "Policy / Regulatory": series(4, 60, 5, 0.4),
      "Industry Shock": series(6, 51, 4, 0.13),
      "Company Crisis": series(7, 34, 3, -0.03),
    };

    return {
      NOW: core.meta.now,
      conf,
      dailySummary: core.dashboard.dailySummary,
      metrics: core.dashboard.metrics,
      upcomingTriggers: core.dashboard.upcomingTriggers,
      eventMap: core.dashboard.eventMap,
      riskRadar: core.risk.riskRadar,
      riskDetails: core.risk.riskDetails,
      industries: core.industries,
      companies: core.companies,
      companyProfiles: core.companyProfiles || {},
      evidence,
      evidenceById,
      events,
      eventsById,
      alerts: core.alerts,
      watchlist: core.watchlist,
      adminJobs: core.admin.jobs,
      adminSources: core.admin.sources,
      adminModels: core.admin.models,
      askSuggestions: core.ask.suggestions,
      riskTrends,
    };
  }

  function ready(data) {
    window.DATA = data;
    window.dispatchEvent(new CustomEvent("signal:data-ready", { detail: data }));
  }

  function unique(values) {
    return [...new Set(values.filter(Boolean))];
  }

  function normalizeTicker(value) {
    return value == null ? "" : String(value).trim().toUpperCase();
  }

  function normalizeCik(value) {
    const digits = value == null ? "" : String(value).replace(/\D/g, "");
    return digits ? digits.padStart(10, "0") : "";
  }

  function companyCik(company) {
    return company.cik || company.secCik || company.primaryCik || (company.identifiers && company.identifiers.cik) || "";
  }

  function normalizeId(value) {
    return value == null ? "" : String(value).trim();
  }

  function normalizeName(value) {
    return value == null ? "" : String(value).trim().toLowerCase().replace(/\s+/g, " ");
  }

  function profileMatchesCompany(profile, company) {
    const identity = (profile && profile.identity) || {};
    const profileTicker = normalizeTicker(identity.ticker);
    const companyTicker = normalizeTicker(company.ticker);
    const profileCik = normalizeCik(identity.cik);
    const currentCompanyCik = normalizeCik(companyCik(company));
    const profileName = normalizeName(identity.name);
    const companyName = normalizeName(company.name);
    const companyId = normalizeName(company.companyId);
    return (
      (profileTicker && companyTicker && profileTicker === companyTicker) ||
      (profileCik && currentCompanyCik && profileCik === currentCompanyCik) ||
      (profileName && (profileName === companyName || profileName === companyId))
    );
  }

  async function fetchCompanyResearchBatch(apiBase, tickers, ciks, companyIds) {
    const params = new URLSearchParams();
    if (tickers.length) params.set("tickers", tickers.join(","));
    if (ciks.length) params.set("ciks", ciks.join(","));
    if (companyIds.length) params.set("company_ids", companyIds.join(","));
    const query = params.toString();
    let response = await fetch(apiBase + "/api/v1/company-research/profiles?" + query);
    if (response.status === 404) {
      response = await fetch(apiBase + "/api/v1/provider-data/company-research/profiles?" + query);
    }
    if (!response.ok) throw new Error("Failed to load company research profiles (" + response.status + ")");
    const body = await response.json();
    return body.items || [];
  }

  async function enrichCompanyProfiles(data) {
    // Approved live integration (build.md §9, 2026-07-06): company-research profiles only — must never enable crisis-risk views (Gate G).
    const apiBase = (window.SIGNAL_API_BASE || "").replace(/\/$/, "");
    if (!apiBase) return data;
    const tickers = unique(data.companies.map((c) => normalizeTicker(c.ticker)));
    const ciks = unique(data.companies.map((c) => normalizeCik(companyCik(c))));
    const companyIds = unique(data.companies.map((c) => normalizeId(c.companyId)));
    if (!tickers.length && !ciks.length && !companyIds.length) return data;
    const profiles = {};
    const batchSize = 80;
    for (let i = 0; i < Math.max(tickers.length, ciks.length, companyIds.length); i += batchSize) {
      const batchProfiles = await fetchCompanyResearchBatch(
        apiBase,
        tickers.slice(i, i + batchSize),
        ciks.slice(i, i + batchSize),
        companyIds.slice(i, i + batchSize)
      );
      batchProfiles.forEach((profile) => {
        const company = data.companies.find((c) => profileMatchesCompany(profile, c));
        if (company) profiles[company.companyId] = profile;
      });
    }
    return {
      ...data,
      companyProfiles: {
        ...data.companyProfiles,
        ...profiles,
      },
    };
  }

  window.__signalDataReady = (async function load() {
    const bust = "?v=" + Date.now();
    const [core, events] = await Promise.all([
      fetch("data/signal-mock-data.json" + bust).then((r) => {
        if (!r.ok) throw new Error("Failed to load signal-mock-data.json (" + r.status + ")");
        return r.json();
      }),
      fetch("data/events.json" + bust).then((r) => {
        if (!r.ok) throw new Error("Failed to load events.json (" + r.status + ")");
        return r.json();
      }),
    ]);
    let data = assemble(core, events);
    data = await enrichCompanyProfiles(data).catch((err) => {
      console.warn("[SIGNAL] company research profile enrichment skipped:", err);
      return data;
    });
    ready(data);
    return data;
  })().catch((err) => {
    console.error("[SIGNAL] data load failed:", err);
    const root = document.getElementById("root");
    if (root) {
      root.innerHTML =
        '<div style="display:grid;place-items:center;height:100vh;font-family:system-ui;color:#aab4c5;text-align:center;padding:24px">' +
        '<div><div style="font-size:16px;font-weight:600;color:#e8edf6;margin-bottom:8px">Unable to load intelligence data</div>' +
        '<div style="font-size:13px">' + (err && err.message ? err.message : "Unknown error") +
        '<br/>Serve this file over http (not file://) so the data fixtures can be fetched.</div></div></div>';
    }
    throw err;
  });
})();
