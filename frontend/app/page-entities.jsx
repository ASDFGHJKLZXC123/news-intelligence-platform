/* SIGNAL — Companies & Industries (overview + detail) */
const { useState, useEffect, useRef, useMemo } = React;

/* ---- Companies overview --------------------------------------------------- */
function CompaniesPage() {
  const D = window.DATA;
  const [dir, setDir] = useState("all");
  const [ind, setInd] = useState("all");
  const inds = [...new Set(D.companies.map((c) => c.industry))];
  const rows = D.companies.filter((c) => {
    if (dir !== "all" && c.impactDirection !== dir) return false;
    if (ind !== "all" && c.industry !== ind) return false;
    return true;
  });
  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow">Entities · Impact Ranking</div>
          <h1 className="page-title">Companies</h1>
          <div className="page-sub">{rows.length} companies ranked by today's event exposure</div>
        </div>
      </div>
      <div className="filter-toolbar entities-toolbar">
        <select className="btn filter-control filter-select" value={ind} onChange={(e) => setInd(e.target.value)}>
          <option value="all">All industries</option>{inds.map((i) => <option key={i} value={i}>{i}</option>)}
        </select>
        <div className="segmented-control">
          {["all", "negative", "mixed", "positive"].map((d) => (
            <button key={d} className="btn btn-sm" onClick={() => setDir(d)}
              style={{ background: dir === d ? "var(--surface-3)" : "transparent", color: dir === d ? "var(--ink)" : "var(--ink-3)" }}>{d}</button>
          ))}
        </div>
      </div>
      <Card bodyClass="">{rows.length ? <CompanyTable rows={rows} watchCol /> : <EmptyState title="No companies match" hint="Adjust your filters." />}</Card>
    </div>
  );
}

function companyFallbackProfile(c) {
  return {
    asOf: c.lastUpdatedAt ? c.lastUpdatedAt.slice(0, 10) : window.DATA.NOW.slice(0, 10),
    financial: {
      headline: "Information not available",
      metrics: [
        { label: "Financial health", value: "Information not available", tone: "neutral" },
        { label: "Revenue trend", value: "Information not available", tone: "neutral" },
        { label: "Profitability", value: "Information not available", tone: "neutral" },
      ],
      notes: ["Information not available"]
    },
    business: {
      description: "Information not available",
      segments: [{ label: c.industry, value: "Information not available", detail: "Information not available" }],
      advantages: ["Information not available"],
      watchItems: ["Information not available"]
    },
    industry: {
      position: c.industry || "Information not available",
      growthDrivers: ["Information not available"],
      risks: ["Information not available"],
      outlook: "Information not available"
    },
    valuation: {
      view: "Information not available",
      price: "Information not available",
      marketCap: "Information not available",
      metrics: [{ label: "Valuation feed", value: "Information not available" }],
      interpretation: "Information not available"
    }
  };
}

function companyProfile(D, c) {
  return (D.companyProfiles && D.companyProfiles[c.companyId]) || companyFallbackProfile(c);
}

function metricToneClass(tone) {
  return "company-fund-metric " + (tone ? "tone-" + tone : "tone-neutral");
}

const COMPANY_INFO_NOT_AVAILABLE = "Information not available";
const COMPANY_RESEARCH_FIELDS = {
  financial: [
    ["revenue", "Revenue"],
    ["gross_profit", "Gross profit"],
    ["operating_profit", "Operating profit"],
    ["net_profit", "Net profit"],
    ["profit_margins", "Profit margins"],
    ["eps", "Earnings per share, EPS"],
    ["cash_flow", "Cash flow"],
    ["debt_level", "Debt level"],
    ["cash_position", "Cash position"],
    ["assets", "Assets"],
    ["liabilities", "Liabilities"],
    ["return_ratios", "Return ratios"],
    ["capital_expenditure", "Capital expenditure"],
    ["dividends_buybacks", "Dividends and buybacks"],
    ["financial_trends", "Financial trends"],
    ["accounting_notes", "Accounting notes"],
  ],
  business: [
    ["business_model", "Business model"],
    ["main_products_services", "Main products or services"],
    ["revenue_segments", "Revenue segments"],
    ["profit_segments", "Profit segments"],
    ["customers", "Customers"],
    ["customer_concentration", "Customer concentration"],
    ["suppliers", "Suppliers"],
    ["supplier_concentration", "Supplier concentration"],
    ["pricing_power", "Pricing power"],
    ["sales_channels", "Sales channels"],
    ["geographic_exposure", "Geographic exposure"],
    ["production_capacity", "Production capacity"],
    ["supply_chain", "Supply chain"],
    ["key_operating_metrics", "Key operating metrics"],
    ["competitive_advantage", "Competitive advantage"],
    ["management_strategy_team", "Management strategy and team"],
  ],
  industry: [
    ["industry_size", "Industry size"],
    ["industry_growth_rate", "Industry growth rate"],
    ["industry_trends", "Industry trends"],
    ["market_share", "Market share"],
    ["competition", "Competition"],
    ["barriers_to_entry", "Barriers to entry"],
    ["industry_profitability", "Industry profitability"],
    ["business_cycle", "Business cycle"],
    ["regulation", "Regulation"],
    ["technology_changes", "Technology changes"],
    ["supply_and_demand", "Supply and demand"],
    ["raw_material_exposure", "Raw material exposure"],
    ["customer_demand", "Customer demand"],
    ["substitution_risk", "Substitution risk"],
    ["macroeconomic_factors", "Macroeconomic factors"],
    ["geopolitical_risks", "Geopolitical risks"],
  ],
  valuation: [
    ["share_price", "Share price"],
    ["market_capitalization", "Market capitalization"],
    ["enterprise_value", "Enterprise value"],
    ["pe_ratio", "PE ratio"],
    ["forward_pe", "Forward PE"],
    ["pb_ratio", "PB ratio"],
    ["ps_ratio", "PS ratio"],
    ["ev_ebitda", "EV/EBITDA"],
    ["free_cash_flow_yield", "Free cash flow yield"],
    ["dividend_yield", "Dividend yield"],
    ["peg_ratio", "PEG ratio"],
    ["historical_valuation", "Historical valuation"],
    ["peer_comparison", "Peer comparison"],
    ["growth_assumptions", "Growth assumptions"],
    ["profit_assumptions", "Profit assumptions"],
    ["discounted_cash_flow", "Discounted cash flow, DCF"],
    ["bull_base_bear_scenarios", "Bull/base/bear scenarios"],
    ["margin_of_safety", "Margin of safety"],
  ],
};

function companyTextAvailable(value) {
  const text = value == null ? "" : String(value).trim();
  return Boolean(text) && text !== COMPANY_INFO_NOT_AVAILABLE && !text.includes(COMPANY_INFO_NOT_AVAILABLE);
}

function textOrUnavailable(value) {
  return companyTextAvailable(value) ? String(value).trim() : COMPANY_INFO_NOT_AVAILABLE;
}

function checklistField(category, key, label, value, source) {
  const available = companyTextAvailable(value);
  return {
    key,
    category,
    label,
    value: available ? String(value) : COMPANY_INFO_NOT_AVAILABLE,
    available,
    source: available ? source || "Profile data" : null,
    as_of: null,
    confidence: available ? 0.75 : 0,
    evidence_refs: available ? [{ source_type: "frontend_profile", source_id: source || "Profile data" }] : [],
    stale: false,
    stale_reason: null,
    freshness_as_of: null,
    conflict: false,
    conflict_reason: null,
    provider_values: [],
    reason: available ? null : "No supported provider data has been collected for this item.",
  };
}

function metricValue(metrics, tests) {
  const found = (metrics || []).find((m) => {
    const label = String(m.label || "").toLowerCase();
    return tests.some((test) => label.includes(test));
  });
  return found ? found.value : null;
}

function valuationMetricValue(v, tests) {
  return metricValue(v.metrics || [], tests);
}

function segmentSummary(segments) {
  if (!segments || !segments.length) return null;
  const clean = segments.map((s) => [s.label, s.value].filter(companyTextAvailable).join(" ")).filter(companyTextAvailable);
  return clean.length ? clean.join("; ") : null;
}

function listSummary(items) {
  const clean = (items || []).filter(companyTextAvailable);
  return clean.length ? clean.join("; ") : null;
}

function defaultChecklist(category, values) {
  const valueMap = values || {};
  return COMPANY_RESEARCH_FIELDS[category].map(([key, label]) => checklistField(category, key, label, valueMap[key], "Profile data"));
}

function normalizeChecklistField(category, key, label, field) {
  if (!field || !field.available || !companyTextAvailable(field.value)) {
    return checklistField(category, key, label, null, null);
  }
  return {
    key,
    category,
    label,
    value: String(field.value).trim(),
    available: true,
    source: field.source || "Profile data",
    as_of: field.as_of || null,
    confidence: typeof field.confidence === "number" ? field.confidence : 0.75,
    evidence_refs: Array.isArray(field.evidence_refs) ? field.evidence_refs : [],
    stale: Boolean(field.stale),
    stale_reason: field.stale_reason || null,
    freshness_as_of: field.freshness_as_of || field.as_of || null,
    conflict: Boolean(field.conflict),
    conflict_reason: field.conflict_reason || null,
    provider_values: Array.isArray(field.provider_values) ? field.provider_values : [],
    reason: null,
  };
}

function normalizeCompanyResearchChecklist(checklist) {
  const normalized = {};
  Object.entries(COMPANY_RESEARCH_FIELDS).forEach(([category, fields]) => {
    const sourceFields = Array.isArray(checklist && checklist[category]) ? checklist[category] : [];
    const byKey = Object.fromEntries(sourceFields.map((field) => [field.key, field]));
    normalized[category] = fields.map(([key, label]) => normalizeChecklistField(category, key, label, byKey[key]));
  });
  return normalized;
}

function derivedProfileChecklist(profile) {
  const f = profile.financial || {};
  const b = profile.business || {};
  const ind = profile.industry || {};
  const v = profile.valuation || {};
  const financialValues = {
    revenue: metricValue(f.metrics, ["revenue"]),
    net_profit: metricValue(f.metrics, ["net profit"]),
    profit_margins: metricValue(f.metrics, ["margin"]),
    cash_flow: metricValue(f.metrics, ["cash flow", "free cash"]),
    debt_level: metricValue(f.metrics, ["debt"]),
    cash_position: metricValue(f.metrics, ["cash"]),
    financial_trends: listSummary(f.notes),
  };
  const businessValues = {
    business_model: b.description,
    revenue_segments: segmentSummary(b.segments),
    competitive_advantage: listSummary(b.advantages),
    supply_chain: listSummary(b.watchItems),
    management_strategy_team: listSummary(b.watchItems),
  };
  const industryValues = {
    industry_growth_rate: listSummary(ind.growthDrivers),
    industry_trends: ind.outlook,
    competition: listSummary(ind.risks),
    geopolitical_risks: listSummary(ind.risks),
  };
  const valuationValues = {
    share_price: v.price,
    market_capitalization: v.marketCap,
    pe_ratio: valuationMetricValue(v, ["p/e", "pe ratio"]),
    forward_pe: valuationMetricValue(v, ["forward"]),
    ev_ebitda: valuationMetricValue(v, ["ev/ebitda"]),
    free_cash_flow_yield: valuationMetricValue(v, ["fcf", "free cash"]),
    dividend_yield: valuationMetricValue(v, ["dividend"]),
    historical_valuation: v.view,
    profit_assumptions: v.interpretation,
  };
  return {
    financial: defaultChecklist("financial", financialValues),
    business: defaultChecklist("business", businessValues),
    industry: defaultChecklist("industry", industryValues),
    valuation: defaultChecklist("valuation", valuationValues),
  };
}

function profileResearchChecklist(profile) {
  return normalizeCompanyResearchChecklist(profile.researchChecklist || derivedProfileChecklist(profile));
}

function normalizeMetricList(metrics, fallback) {
  const list = Array.isArray(metrics) && metrics.length ? metrics : fallback;
  return list.map((metric) => ({
    label: textOrUnavailable(metric && metric.label),
    value: textOrUnavailable(metric && metric.value),
    tone: metric && metric.tone ? metric.tone : "neutral",
  }));
}

function normalizeTextList(items) {
  const clean = (Array.isArray(items) ? items : []).map(textOrUnavailable).filter(companyTextAvailable);
  return clean.length ? clean : [COMPANY_INFO_NOT_AVAILABLE];
}

function normalizeSegmentList(segments, fallback) {
  const source = Array.isArray(segments) && segments.length ? segments : fallback;
  return source.map((segment) => ({
    label: textOrUnavailable(segment && segment.label),
    value: textOrUnavailable(segment && segment.value),
    detail: textOrUnavailable(segment && segment.detail),
  }));
}

function normalizeCompanyResearchProfile(company, profile) {
  const fallback = companyFallbackProfile(company);
  const p = profile || fallback;
  const f = p.financial || {};
  const b = p.business || {};
  const ind = p.industry || {};
  const v = p.valuation || {};
  return {
    ...fallback,
    ...p,
    asOf: p.asOf || fallback.asOf,
    lastUpdatedAt: p.lastUpdatedAt || p.asOf || fallback.asOf,
    financial: {
      headline: textOrUnavailable(f.headline),
      metrics: normalizeMetricList(f.metrics, fallback.financial.metrics),
      notes: normalizeTextList(f.notes),
    },
    business: {
      description: textOrUnavailable(b.description),
      segments: normalizeSegmentList(b.segments, fallback.business.segments),
      advantages: normalizeTextList(b.advantages),
      watchItems: normalizeTextList(b.watchItems),
    },
    industry: {
      position: textOrUnavailable(ind.position),
      growthDrivers: normalizeTextList(ind.growthDrivers),
      risks: normalizeTextList(ind.risks),
      outlook: textOrUnavailable(ind.outlook),
    },
    valuation: {
      view: textOrUnavailable(v.view),
      price: textOrUnavailable(v.price),
      marketCap: textOrUnavailable(v.marketCap),
      metrics: normalizeMetricList(v.metrics, fallback.valuation.metrics),
      interpretation: textOrUnavailable(v.interpretation),
    },
    researchChecklist: profileResearchChecklist(p),
  };
}

function CompanyFundMetricGrid({ metrics }) {
  return (
    <div className="company-fund-metric-grid">
      {(metrics || []).map((m) => (
        <div key={m.label} className={metricToneClass(m.tone)}>
          <div className="eyebrow">{m.label}</div>
          <strong>{m.value}</strong>
        </div>
      ))}
    </div>
  );
}

function CompanySegmentList({ segments }) {
  return (
    <div className="company-segment-list">
      {(segments || []).map((s) => (
        <div key={s.label} className="company-segment">
          <div>
            <strong>{s.label}</strong>
            <p>{s.detail}</p>
          </div>
          <span className="mono">{s.value}</span>
        </div>
      ))}
    </div>
  );
}

function CompanyBulletList({ items, icon, color }) {
  const Ic = icon || Icon.check;
  return (
    <div className="company-bullet-list">
      {(items || []).map((item) => (
        <div key={item}>
          <Ic style={{ width: 13, height: 13, color: color || "var(--accent)", flex: "0 0 auto", marginTop: 2 }} />
          <span>{item}</span>
        </div>
      ))}
    </div>
  );
}

function CompanyChecklistBlock({ fields }) {
  return (
    <div className="company-checklist-grid">
      {(fields || []).map((field) => (
        <div key={field.key || field.label} className={"company-checklist-row" + (field.available ? "" : " is-missing") + (field.stale ? " is-stale" : "")}>
          <span>{field.label}</span>
          <strong>{field.value || COMPANY_INFO_NOT_AVAILABLE}</strong>
          <div className="company-checklist-meta">
            {field.source && <em>{field.source}</em>}
            {field.stale && <b>Stale</b>}
          </div>
        </div>
      ))}
    </div>
  );
}

function coverageStatus(profile) {
  const coverage = profile.coverage || {};
  const pct = Number(coverage.coverage_pct || 0);
  const staleCount = Array.isArray(profile.staleFields) ? profile.staleFields.length : 0;
  let label = "Unavailable";
  if (pct >= 75) label = "Strong coverage";
  else if (pct >= 40) label = "Partial coverage";
  else if (pct > 0) label = "Limited coverage";
  return {
    label: staleCount ? label + " · stale fields" : label,
    pct,
    available: coverage.available || 0,
    total: coverage.total || 66,
  };
}

function CompanyCoverageStrip({ profile }) {
  const coverage = coverageStatus(profile);
  return (
    <div className="company-coverage-strip">
      <span>{coverage.label}</span>
      <strong>{coverage.available}/{coverage.total}</strong>
      <em>{coverage.pct}%</em>
    </div>
  );
}

function CompanyFundamentalsTab({ company, profile }) {
  const normalizedProfile = normalizeCompanyResearchProfile(company, profile);
  const f = normalizedProfile.financial || {};
  const b = normalizedProfile.business || {};
  const ind = normalizedProfile.industry || {};
  const v = normalizedProfile.valuation || {};
  const checklist = normalizedProfile.researchChecklist;
  return (
    <div className="company-fundamentals">
      <div className="company-fundamentals-span">
        <CompanyCoverageStrip profile={normalizedProfile} />
      </div>
      <Card icon={Icon.activity} title="Financial Information" eyebrow="Profitability & balance sheet">
        <p className="company-fund-lede">{f.headline}</p>
        <CompanyFundMetricGrid metrics={f.metrics} />
        <CompanyBulletList items={f.notes} icon={Icon.check} color="var(--r-low)" />
        <CompanyChecklistBlock fields={checklist.financial} />
      </Card>

      <Card icon={Icon.companies} title="Business Information" eyebrow="How the company makes money">
        <p className="company-fund-lede">{b.description}</p>
        <CompanySegmentList segments={b.segments} />
        <div className="company-fund-two-col">
          <div>
            <div className="eyebrow">Advantages</div>
            <CompanyBulletList items={b.advantages} icon={Icon.check} color="var(--r-low)" />
          </div>
          <div>
            <div className="eyebrow">Watch Items</div>
            <CompanyBulletList items={b.watchItems} icon={Icon.target} color="var(--r-med)" />
          </div>
        </div>
        <CompanyChecklistBlock fields={checklist.business} />
      </Card>

      <Card icon={Icon.industries} title="Industry Information" eyebrow="Growth durability">
        <p className="company-fund-lede">{ind.position}</p>
        <div className="company-fund-two-col">
          <div>
            <div className="eyebrow">Growth Drivers</div>
            <CompanyBulletList items={ind.growthDrivers} icon={Icon.trendUp} color="var(--r-low)" />
          </div>
          <div>
            <div className="eyebrow">Industry Risks</div>
            <CompanyBulletList items={ind.risks} icon={Icon.warn} color="var(--r-high)" />
          </div>
        </div>
        <div className="company-fund-outlook">{ind.outlook}</div>
        <CompanyChecklistBlock fields={checklist.industry} />
      </Card>

      <Card icon={Icon.scale} title="Valuation Information" eyebrow={"Market snapshot · " + (normalizedProfile.asOf || "latest")}>
        <div className="company-valuation-head">
          <div>
            <div className="eyebrow">Valuation View</div>
            <strong>{v.view}</strong>
          </div>
          <div>
            <div className="eyebrow">Stock Price</div>
            <strong className="mono">{v.price}</strong>
          </div>
          <div>
            <div className="eyebrow">Market Cap</div>
            <strong className="mono">{v.marketCap}</strong>
          </div>
        </div>
        <CompanyFundMetricGrid metrics={v.metrics} />
        <p className="company-fund-lede">{v.interpretation}</p>
        <div className="company-fund-note">Valuation is a market-data snapshot for {company.ticker}; it should be refreshed by the backend before production use.</div>
        <CompanyChecklistBlock fields={checklist.valuation} />
      </Card>
    </div>
  );
}

/* ---- Company detail ------------------------------------------------------- */
function CompanyDetail({ id }) {
  const D = window.DATA;
  const c = D.companies.find((x) => x.companyId === id);
  const [tab, setTab] = useState("overview");
  if (!c) return <div className="page"><EmptyState title="Company not found" /></div>;
  const relEvents = D.events.filter((e) => (e.affectedCompanies || []).some((a) => a.ticker === c.ticker) || (e.relatedCompanies || []).some((r) => r.companyId === id));
  const profile = companyProfile(D, c);
  const riskFactors = ["Geographic revenue concentration", "Policy / export-control exposure", "Demand-cycle sensitivity", "Supply-chain dependency"];

  const tabs = ["overview", "fundamentals", "events", "risk", "forecast"];
  const tabLabels = { overview: "Overview", fundamentals: "Fundamentals", events: "Related Events", risk: "Risk Factors", forecast: "Forecast" };
  return (
    <div className="page">
      <button className="btn btn-sm btn-ghost" style={{ marginBottom: 16 }} onClick={() => Store.nav("companies")}><Icon.chevL style={{ width: 15, height: 15 }} /> Companies</button>
      <Card bodyClass="card-pad">
        <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
          <CompanyLogo company={c} size={52} />
          <div style={{ flex: "1 1 240px", minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}><h1 style={{ fontSize: 23, minWidth: 0 }}>{c.name}</h1><DirPill d={c.impactDirection} /></div>
            <div style={{ display: "flex", gap: 10, marginTop: 4, fontSize: 12.5, color: "var(--ink-3)", flexWrap: "wrap" }} className="mono">
              <span>{c.ticker}</span><span>·</span><span>{c.exchange}</span><span>·</span><span>{c.industry}</span><span>·</span><span>{c.country}</span>
            </div>
          </div>
          <div style={{ display: "flex", gap: 24, alignItems: "center", flexWrap: "wrap" }}>
            <ScoreStat label="Impact" value={c.impactScore} tip />
            <ScoreStat label="Risk" value={c.riskScore} tip />
            <WatchBtn id={c.companyId} />
          </div>
        </div>
      </Card>

      <div className="tabs" style={{ margin: "18px 0" }}>
        {tabs.map((t) => <div key={t} className={"tab" + (tab === t ? " active" : "")} onClick={() => setTab(t)}>{tabLabels[t]}</div>)}
      </div>

      {tab === "overview" && (
        <div className="grid" style={{ gridTemplateColumns: "1fr 300px", alignItems: "start" }}>
          <div className="stack">
            <Card icon={Icon.info} title="Exposure Summary">
              <p style={{ fontSize: 13.5, lineHeight: 1.6, color: "var(--ink-2)", margin: 0 }}>
                {c.name} is currently flagged with <strong style={{ color: dirMeta(c.impactDirection).color }}>{dirMeta(c.impactDirection).label.toLowerCase()}</strong> impact, driven primarily by <strong>{c.topDriver.toLowerCase()}</strong>. The company appears across {relEvents.length} active event cluster{relEvents.length !== 1 ? "s" : ""} today, with an impact score of {c.impactScore} and a risk score of {c.riskScore}.
              </p>
            </Card>
            <Card icon={Icon.activity} title="Score Composition">
              <BarList rows={[
                { label: "Event exposure", value: c.impactScore, color: "var(--accent)" },
                { label: "Risk score", value: c.riskScore, color: levelColor(scoreLevel(c.riskScore)) },
                { label: "News velocity", value: Math.min(98, c.relatedEventCount * 14 + 28), color: "var(--r-med)" },
              ]} />
            </Card>
          </div>
          <div className="stack" style={{ position: "sticky", top: 0 }}>
            <Card icon={Icon.shield} title="Risk Score"><div style={{ display: "flex", justifyContent: "center" }}><Gauge value={c.riskScore} size={128} label="Risk" /></div></Card>
            <Card icon={Icon.bolt} title="Top Driver"><p style={{ fontSize: 13, color: "var(--ink-2)", margin: 0 }}>{c.topDriver}</p></Card>
          </div>
        </div>
      )}
      {tab === "fundamentals" && <CompanyFundamentalsTab company={c} profile={profile} />}
      {tab === "events" && (
        <div className="grid" style={{ gridTemplateColumns: "repeat(2, 1fr)" }}>
          {relEvents.length ? relEvents.map((e) => <HotEventCard key={e.id} e={e} />) : <Card><EmptyState title="No related events" /></Card>}
        </div>
      )}
      {tab === "risk" && (
        <Card icon={Icon.warn} title="Risk Factors">
          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
            {riskFactors.map((f, i) => (
              <div key={f} style={{ display: "flex", gap: 11, padding: 13, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)" }}>
                <span className="mono" style={{ fontSize: 11, color: "var(--ink-faint)", fontWeight: 700 }}>{String(i + 1).padStart(2, "0")}</span>
                <span style={{ fontSize: 13, color: "var(--ink-2)" }}>{f}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
      {tab === "forecast" && <Card><EmptyState icon={Icon.scale} title="Company-level forecast scenarios" hint="Forecast scenarios for this company are generated from its highest-exposure event. Open the related event's Forecast tab for the full probabilistic breakdown."
        action={relEvents[0] && <button className="btn btn-sm btn-primary" onClick={() => Store.nav("event", relEvents[0].id)}>Open event forecast <Icon.arrowRight style={{ width: 14, height: 14 }} /></button>} /></Card>}
    </div>
  );
}

/* ---- Industries overview -------------------------------------------------- */
function IndustriesPage() {
  const D = window.DATA;
  const [view, setView] = useState("heatmap");
  const sorted = [...D.industries].sort((a, b) => b.impactScore - a.impactScore);
  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow">Entities · Sector Impact</div>
          <h1 className="page-title">Industries</h1>
          <div className="page-sub">{D.industries.length} sectors ranked by impact intensity</div>
        </div>
        <div style={{ display: "flex", gap: 4, background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 8, padding: 3 }}>
          <button className="btn btn-sm" style={{ background: view === "heatmap" ? "var(--surface-3)" : "transparent", border: "none" }} onClick={() => setView("heatmap")}><Icon.grid style={{ width: 14, height: 14 }} /> Heatmap</button>
          <button className="btn btn-sm" style={{ background: view === "table" ? "var(--surface-3)" : "transparent", border: "none" }} onClick={() => setView("table")}><Icon.list style={{ width: 14, height: 14 }} /> Table</button>
        </div>
      </div>

      {view === "heatmap" ? (
        <Card icon={Icon.industries} title="Industry Heatmap" eyebrow="Impact intensity · click to drill in">
          <div className="grid" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>{D.industries.map((it) => <HeatCell key={it.industryId} it={it} />)}</div>
        </Card>
      ) : (
        <Card bodyClass="">
          <table className="tbl">
            <thead><tr><th className="no-sort">Industry</th><th className="no-sort">Impact</th><th className="no-sort">Risk</th><th className="no-sort">Opportunity</th><th className="no-sort">Direction</th><th className="no-sort">Events</th><th className="no-sort">News Velocity</th></tr></thead>
            <tbody>
              {sorted.map((it) => (
                <tr key={it.industryId} onClick={() => Store.nav("industry", it.industryId)}>
                  <td><span style={{ fontSize: 13, fontWeight: 600 }}>{it.industryName}</span></td>
                  <td><MiniBar value={it.impactScore} /></td>
                  <td><MiniBar value={it.riskScore} risk /></td>
                  <td><MiniBar value={it.opportunityScore} /></td>
                  <td><DirPill d={it.direction} /></td>
                  <td><span className="mono" style={{ fontSize: 12.5 }}>{it.relatedEventCount}</span></td>
                  <td><div style={{ width: 80 }}><Sparkline data={window.DATA.riskTrends["Macro Risk"].map((v) => Math.round(v * it.newsVelocityScore / 60))} w={80} h={22} color="var(--r-med)" /></div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

/* ---- Industry detail ------------------------------------------------------ */
function IndustryDetail({ id }) {
  const D = window.DATA;
  const it = D.industries.find((x) => x.industryId === id);
  if (!it) return <div className="page"><EmptyState title="Industry not found" /></div>;
  const m = dirMeta(it.direction);
  const relCompanies = D.companies.filter((c) => c.industry.toLowerCase().includes(it.industryName.toLowerCase().split(" ")[0].toLowerCase()) || it.industryName.toLowerCase().includes(c.industry.toLowerCase()));
  const relEvents = D.events.filter((e) => e.affectedIndustries.some((x) => x.toLowerCase().includes(it.industryName.toLowerCase()) || it.industryName.toLowerCase().includes(x.toLowerCase())));
  const indicators = ["Sector index performance", "Order-book / backlog trends", "Policy & regulatory updates", "News velocity vs. baseline", "Analyst revision breadth"];

  return (
    <div className="page">
      <button className="btn btn-sm btn-ghost" style={{ marginBottom: 16 }} onClick={() => Store.nav("industries")}><Icon.chevL style={{ width: 15, height: 15 }} /> Industries</button>
      <Card bodyClass="card-pad">
        <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ width: 50, height: 50, borderRadius: 12, background: `color-mix(in oklch, ${m.color} 16%, var(--surface-3))`, border: `1px solid color-mix(in oklch, ${m.color} 30%, var(--line))`, display: "grid", placeItems: "center", flex: "0 0 auto" }}><Icon.industries style={{ width: 24, height: 24, color: m.color }} /></div>
          <div style={{ flex: "1 1 240px", minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}><h1 style={{ fontSize: 23, minWidth: 0 }}>{it.industryName}</h1><DirPill d={it.direction} /></div>
            <p style={{ fontSize: 13, color: "var(--ink-2)", margin: "5px 0 0", maxWidth: 560 }}>{it.summary}</p>
          </div>
          <div style={{ display: "flex", gap: 22, flexWrap: "wrap" }}>
            <ScoreStat label="Impact" value={it.impactScore} tip />
            <ScoreStat label="Risk" value={it.riskScore} tip />
            <ScoreStat label="Opportunity" value={it.opportunityScore} tip color="var(--r-low)" />
          </div>
        </div>
      </Card>

      <div className="grid" style={{ gridTemplateColumns: "1fr 300px", marginTop: 18, alignItems: "start" }}>
        <div className="stack">
          <Card icon={Icon.flame} title="Top Related Events" eyebrow={relEvents.length + " events"}>
            <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>{relEvents.length ? relEvents.map((e) => <HotEventCard key={e.id} e={e} />) : <EmptyState title="No related events today" />}</div>
          </Card>
          <Card icon={Icon.companies} title="Affected Companies" eyebrow={relCompanies.length + " companies"} bodyClass="">
            {relCompanies.length ? <CompanyTable rows={relCompanies} /> : <div className="card-pad"><EmptyState title="No companies mapped" /></div>}
          </Card>
        </div>
        <div className="stack" style={{ position: "sticky", top: 0 }}>
          <Card icon={Icon.activity} title="News Velocity"><div style={{ marginBottom: 8 }}><Sparkline data={D.riskTrends["Macro Risk"].map((v) => Math.round(v * it.newsVelocityScore / 55))} w={244} h={50} color="var(--r-med)" /></div><Row k="Velocity score" v={it.newsVelocityScore} /></Card>
          <Card icon={Icon.target} title="Key Indicators"><div className="stack" style={{ gap: 8 }}>{indicators.map((x) => <div key={x} style={{ display: "flex", gap: 8, fontSize: 12.5, color: "var(--ink-2)" }}><Icon.target style={{ width: 13, height: 13, color: "var(--accent)", flexShrink: 0, marginTop: 1 }} />{x}</div>)}</div></Card>
          <Card icon={Icon.book} title="Historical Sensitivity"><p style={{ fontSize: 12.5, color: "var(--ink-2)", margin: 0, lineHeight: 1.5 }}>This sector has shown {it.riskScore > 65 ? "high" : "moderate"} sensitivity to similar event types historically, with impact typically peaking 1–3 weeks after the initial signal.</p></Card>
          <WatchBtn id={it.industryId} />
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { CompaniesPage, CompanyDetail, IndustriesPage, IndustryDetail });
