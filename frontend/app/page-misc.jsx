/* SIGNAL — Historical, Watchlist, Reports, Ask AI, Settings */
const { useState, useEffect, useRef, useMemo } = React;

/* ---- Historical Analogies ------------------------------------------------- */
function HistoricalPage() {
  const D = window.DATA;
  const all = D.events.filter((e) => e.historicalAnalogies).flatMap((e) => e.historicalAnalogies.map((a) => ({ a, e })));
  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow">Intelligence · Pattern Matching</div>
          <h1 className="page-title">Historical Analogies</h1>
          <div className="page-sub">Today's events matched against historical precedents</div>
          <PageStatus quality="synthetic" label="Sample" />
        </div>
      </div>
      <div className="stack">
        {all.length === 0 && (
          <Card><EmptyState icon={Icon.historical} title="No historical analogies yet"
            hint="Matched historical precedents appear here as event analysis completes." /></Card>
        )}
        {all.map(({ a, e }) => (
          <Card key={a.historicalEventId} bodyClass="card-pad">
            <div style={{ display: "flex", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
              <div style={{ width: 54, textAlign: "center", flexShrink: 0 }}>
                <div className="mono" style={{ fontSize: 26, fontWeight: 600, color: "var(--accent)", lineHeight: 1 }}>{Math.round(a.similarityScore * 100)}<span style={{ fontSize: 11 }}>%</span></div>
                <div className="eyebrow" style={{ fontSize: 8.5, marginTop: 3 }}>Similar</div>
              </div>
              <div style={{ flex: "1 1 240px", minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 5, flexWrap: "wrap" }}>
                  <h3 style={{ fontSize: 16, minWidth: 0 }}>{a.title}</h3>
                  <span className="mono" style={{ fontSize: 11, color: "var(--ink-faint)", overflowWrap: "anywhere", lineHeight: 1.35 }}>{a.startDate} → {a.endDate}</span>
                </div>
                <div style={{ fontSize: 12, color: "var(--ink-3)", marginBottom: 9 }}>Matched to: <span className="chip chip-x" onClick={() => Store.nav("event", e.id)} style={{ marginLeft: 4 }}>{e.title.slice(0, 46)}…</span></div>
                <p style={{ fontSize: 13, color: "var(--ink-2)", margin: 0, lineHeight: 1.55 }}>{a.historicalOutcome}</p>
                <div style={{ display: "flex", gap: 7, flexWrap: "wrap", marginTop: 11 }}>
                  {a.similarities.slice(0, 3).map((s) => <span key={s} className="badge badge-low" style={{ fontSize: 9.5 }}>✓ {s}</span>)}
                  {a.differences.slice(0, 2).map((s) => <span key={s} className="badge badge-high" style={{ fontSize: 9.5 }}>Δ {s}</span>)}
                </div>
              </div>
              <button className="btn btn-sm" onClick={() => Store.nav("event", e.id)}>Open event <Icon.arrowRight style={{ width: 13, height: 13 }} /></button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

/* ---- Watchlist ------------------------------------------------------------ */
function WatchlistPage() {
  const D = window.DATA;
  const st = useStore();
  const typeIcon = { company: Icon.companies, industry: Icon.industries, risk: Icon.radar, keyword: Icon.search, country: Icon.globe, person: Icon.users, organization: Icon.companies };
  const groups = { company: [], industry: [], risk: [], keyword: [] };
  D.watchlist.forEach((w) => { (groups[w.itemType] = groups[w.itemType] || []).push(w); });
  const recentEvents = D.events.slice(0, 3);
  const watchAlerts = D.alerts.filter((a) => a.alertType.includes("watchlist"));
  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow">Workspace</div>
          <h1 className="page-title">Watchlist</h1>
          <div className="page-sub">{D.watchlist.length} items monitored for targeted alerts</div>
          <PageStatus blocks={["watchlist"]} />
        </div>
        <button className="btn btn-sm btn-primary"><Icon.plus style={{ width: 14, height: 14 }} /> Add item</button>
      </div>
      <div className="grid" style={{ gridTemplateColumns: "1fr 320px", alignItems: "start" }}>
        <div className="stack">
          {Object.keys(groups).map((type) => groups[type].length > 0 && (
            <Card key={type} icon={typeIcon[type]} title={{ company: "Watched Companies", industry: "Watched Industries", risk: "Watched Risks", keyword: "Watched Keywords" }[type]}
              eyebrow={groups[type].length + " watched"}>
              <div className="stack" style={{ gap: 0 }}>
                {groups[type].map((w, i) => {
                  const watchCompany = type === "company" ? D.companies.find((c) =>
                    c.companyId === w.id || c.ticker === (w.metadata && w.metadata.ticker) || c.name === w.label
                  ) : null;
                  return (
                  <div key={w.id} style={{ display: "flex", alignItems: "center", gap: 12, padding: "11px 0", borderBottom: i < groups[type].length - 1 ? "1px solid var(--line-soft)" : "none", flexWrap: "wrap" }}>
                    {type === "company" ? <CompanyLogo company={watchCompany} ticker={w.metadata && w.metadata.ticker} name={w.label} size={32} /> : <div style={{ width: 32, height: 32, borderRadius: 8, background: "var(--surface-3)", border: "1px solid var(--line)", display: "grid", placeItems: "center", flex: "0 0 auto" }}>{React.createElement(typeIcon[type], { style: { width: 15, height: 15, color: "var(--ink-2)" } })}</div>}
                    <div style={{ flex: "1 1 140px", minWidth: 0 }}>
                      <div style={{ fontSize: 13.5, fontWeight: 550, overflowWrap: "anywhere", lineHeight: 1.3 }}>{w.label}</div>
                      {w.metadata && w.metadata.ticker && <div className="ticker">{w.metadata.ticker}</div>}
                    </div>
                    <span className="badge" style={{ background: w.alertEnabled ? "var(--accent-soft)" : "var(--surface-3)", color: w.alertEnabled ? "var(--accent)" : "var(--ink-3)", borderColor: w.alertEnabled ? "var(--accent-line)" : "var(--line)", flex: "0 0 auto" }}>{w.alertEnabled ? "Alerts on" : "Alerts off"}</span>
                    <span className="mono" style={{ fontSize: 10.5, color: "var(--ink-faint)", flex: "0 0 auto" }}>since {(w.createdAt || "").slice(5) || "—"}</span>
                  </div>
                  );
                })}
              </div>
            </Card>
          ))}
          <Card icon={Icon.flame} title="Recent Watchlist Events">
            <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>{recentEvents.map((e) => <HotEventCard key={e.id} e={e} />)}</div>
          </Card>
        </div>
        <div className="stack" style={{ position: "sticky", top: 0 }}>
          <Card icon={Icon.alerts} title="Watchlist Alerts" eyebrow={watchAlerts.length + " active"}>
            <div className="stack" style={{ gap: 11 }}>
              {watchAlerts.map((a) => (
                <div key={a.id} onClick={() => Store.nav("alerts")} style={{ cursor: "pointer", paddingBottom: 10, borderBottom: "1px solid var(--line-soft)" }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 3 }}><RiskBadge level={a.severity} /><span className="mono" style={{ fontSize: 10, color: "var(--ink-faint)" }}>{timeAgo(a.createdAt)}</span></div>
                  <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.35 }}>{a.title}</div>
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

/* ---- Reports -------------------------------------------------------------- */
function ReportsPage() {
  const D = window.DATA;
  const reports = [
    { id: "r1", title: "Daily Intelligence Brief — June 5, 2026", type: "daily_brief", generatedAt: "08:30 PT", confidence: 0.71, sections: 9 },
    { id: "r2", title: "Weekly Risk Review — Wk 23", type: "weekly_risk_review", generatedAt: "Jun 2, 06:00 PT", confidence: 0.68, sections: 7 },
    { id: "r3", title: "Event Report — Semiconductor Export Controls", type: "event_report", generatedAt: "08:22 PT", confidence: 0.72, sections: 8 },
    { id: "r4", title: "Company Risk Report — NVIDIA", type: "company_report", generatedAt: "Jun 4, 14:10 PT", confidence: 0.7, sections: 6 },
    { id: "r5", title: "Industry Outlook — Semiconductors", type: "industry_report", generatedAt: "Jun 3, 09:00 PT", confidence: 0.66, sections: 6 },
  ];
  const typeLabel = { daily_brief: "Daily Brief", weekly_risk_review: "Weekly Review", event_report: "Event Report", company_report: "Company Report", industry_report: "Industry Report", custom_report: "Custom" };
  const [sel, setSel] = useState(reports[0]);
  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow">Workspace</div>
          <h1 className="page-title">Reports</h1>
          <div className="page-sub">Generated briefs and intelligence reports</div>
          <PageStatus quality="synthetic" label="Sample" />
        </div>
        <button className="btn btn-sm btn-primary"><Icon.plus style={{ width: 14, height: 14 }} /> New report</button>
      </div>
      <div className="grid" style={{ gridTemplateColumns: "360px 1fr", alignItems: "start" }}>
        <div className="stack" style={{ gap: 10 }}>
          {reports.map((r) => (
            <div key={r.id} className="card card-pad" onClick={() => setSel(r)} style={{ cursor: "pointer", borderColor: sel.id === r.id ? "var(--accent-line)" : "", background: sel.id === r.id ? "var(--accent-soft)" : "" }}>
              <div style={{ display: "flex", gap: 11 }}>
                <Icon.reports style={{ width: 18, height: 18, color: "var(--accent)", flexShrink: 0, marginTop: 1 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13.5, fontWeight: 600, lineHeight: 1.3 }}>{r.title}</div>
                  <div style={{ display: "flex", gap: 8, marginTop: 6, alignItems: "center", flexWrap: "wrap" }}>
                    <span className="badge badge-neutral" style={{ fontSize: 9 }}>{typeLabel[r.type]}</span>
                    <span className="mono" style={{ fontSize: 10.5, color: "var(--ink-faint)", overflowWrap: "anywhere" }}>{r.generatedAt}</span>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
        <Card bodyClass="card-pad">
          <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 16, paddingBottom: 16, borderBottom: "1px solid var(--line)", flexWrap: "wrap" }}>
            <div style={{ flex: "1 1 260px", minWidth: 0 }}>
              <span className="badge badge-neutral" style={{ marginBottom: 8 }}>{typeLabel[sel.type]}</span>
              <h2 style={{ fontSize: 19, marginTop: 8 }}>{sel.title}</h2>
              <div style={{ display: "flex", gap: 10, marginTop: 8, fontSize: 12, color: "var(--ink-3)", flexWrap: "wrap", alignItems: "center" }}>
                <span style={{ overflowWrap: "anywhere" }}>Generated {sel.generatedAt}</span><span>·</span><ConfidenceBadge score={sel.confidence} /><span>·</span><span>{sel.sections} sections</span>
              </div>
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <button className="btn btn-sm"><Icon.download style={{ width: 14, height: 14 }} /> PDF</button>
              <button className="btn btn-sm"><Icon.link style={{ width: 14, height: 14 }} /> Share</button>
            </div>
          </div>
          <div className="stack" style={{ gap: 16 }}>
            {["Executive Summary", "Top Events", "Risk Radar", "Industry Impact", "Company Impact", "Historical Analogies", "Forecast Scenarios", "Evidence", "Appendix"].slice(0, sel.sections).map((s, i) => (
              <div key={s}>
                <div className="eyebrow" style={{ marginBottom: 7, display: "flex", alignItems: "center", gap: 8 }}><span className="mono" style={{ color: "var(--accent)" }}>{String(i + 1).padStart(2, "0")}</span>{s}</div>
                <p style={{ fontSize: 13, color: "var(--ink-2)", margin: 0, lineHeight: 1.6 }}>
                  {i === 0 ? ((D.dailySummary && D.dailySummary.summary) || "Executive summary is generated from the day's intelligence base.") : "Section content is generated from the underlying intelligence base with full claim-to-evidence traceability. Every conclusion in this section links back to source articles, market data, and model reasoning."}
                </p>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

/* ---- Ask AI --------------------------------------------------------------- */
function AskPage() {
  const D = window.DATA;
  const [q, setQ] = useState("");
  const [conv, setConv] = useState(null); // {q, answer, streaming}
  const [streamed, setStreamed] = useState("");

  const canned = {
    semiconductor: { answer: "The dominant risk to the semiconductor industry this week is policy-driven: reports of stricter advanced-chip export controls have pushed the Policy/Regulatory risk score to 72 and the sector impact score to 86. Equipment suppliers (ASML, Applied Materials, Lam Research) carry the most direct exposure, while designers like NVIDIA face geographic-revenue concentration risk. Realized impact hinges on exemption language still to be clarified.", ev: ["ev1", "ev2", "ev3"], events: ["evt-semis"], companies: ["nvda", "asml"], inds: ["Semiconductors", "AI Infrastructure"] },
    export: { answer: "NVIDIA (impact 82), ASML (79), Applied Materials (71), and Lam Research (66) are the most affected by the latest export-control reports. Equipment makers show higher beta to the news than chip designers. TSMC carries mixed/indirect exposure as a foundry.", ev: ["ev1", "ev4", "ev5"], events: ["evt-semis"], companies: ["nvda", "asml", "amat"], inds: ["Semiconductors"] },
    recession: { answer: "Recession risk is currently medium (Macro score 58, +5 over 7 days). It is rising modestly, driven by labor-market softening and an elevated yield-curve model, but the Sahm Rule has not triggered and financial stress remains low-to-medium. Friday's labor data is the key swing factor; estimated 12-month probability sits near 28%.", ev: ["ev7"], events: ["evt-recession"], companies: [], inds: ["Banking", "Consumer Discretionary"] },
    banking: { answer: "Today's regional-banking stress most closely matches the 2023 regional-bank liquidity crisis (similarity 0.83): shared deposit-concentration risk, interest-rate pressure, and unrealized bond losses. Key difference — current buffers and the speed of regulatory response may be stronger now. News velocity is rising ahead of price, the same early-warning configuration seen in 2023.", ev: [], events: ["evt-banking"], companies: ["wal", "cma"], inds: ["Banking"] },
    default: { answer: "Based on today's intelligence base, the highest-priority signals are the semiconductor export-control story (risk 76), rising regional-banking news velocity, and Gulf shipping escalation lifting energy risk to 78. AI-infrastructure demand remains a positive divergence. The single most important monitor is Friday's labor data for the macro read.", ev: ["ev1", "ev7"], events: ["evt-semis", "evt-banking"], companies: ["nvda"], inds: ["Semiconductors", "Banking", "Energy"] },
  };
  function pick(text) {
    const t = text.toLowerCase();
    if (t.includes("export")) return canned.export;
    if (t.includes("semiconductor") || t.includes("chip")) return canned.semiconductor;
    if (t.includes("recession")) return canned.recession;
    if (t.includes("bank")) return canned.banking;
    return canned.default;
  }
  function ask(text) {
    const r = pick(text);
    setConv({ q: text, r, streaming: true });
    setStreamed("");
    let i = 0;
    const step = Math.max(3, Math.ceil(r.answer.length / 44));
    const iv = setInterval(() => {
      i += step; setStreamed(r.answer.slice(0, i));
      if (i >= r.answer.length) { clearInterval(iv); setConv((c) => ({ ...c, streaming: false })); }
    }, 18);
  }

  return (
    <div className="page" style={{ maxWidth: 880 }}>
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow" style={{ display: "flex", alignItems: "center", gap: 8 }}><Icon.spark style={{ width: 14, height: 14, color: "var(--accent)" }} /> Natural-language intelligence</div>
          <h1 className="page-title">Ask AI</h1>
          <div className="page-sub">Query the intelligence base. Every answer cites its evidence.</div>
          <PageStatus blocks={["askSuggestions"]} label="Sample" />
        </div>
      </div>

      <div className="search" style={{ maxWidth: "none", height: 50, background: "var(--surface)", marginBottom: 16, borderRadius: 12, padding: "0 16px" }}>
        <Icon.ask style={{ width: 18, height: 18, color: "var(--accent)" }} />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask about risks, companies, industries, or events…" style={{ fontSize: 15 }}
          onKeyDown={(e) => { if (e.key === "Enter" && q.trim()) ask(q); }} />
        <button className="btn btn-primary btn-sm" onClick={() => q.trim() && ask(q)}>Ask <Icon.arrowRight style={{ width: 14, height: 14 }} /></button>
      </div>

      {!conv && (
        <div>
          <div className="eyebrow" style={{ marginBottom: 10 }}>Suggested questions</div>
          <div className="stack" style={{ gap: 8 }}>
            {D.askSuggestions.map((s) => (
              <div key={s} className="card card-pad" onClick={() => { setQ(s); ask(s); }} style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 11 }}>
                <Icon.ask style={{ width: 16, height: 16, color: "var(--ink-3)" }} />
                <span style={{ fontSize: 13.5, flex: 1 }}>{s}</span>
                <Icon.arrowRight style={{ width: 15, height: 15, color: "var(--ink-faint)" }} />
              </div>
            ))}
          </div>
        </div>
      )}

      {conv && (
        <div className="stack">
          <div style={{ display: "flex", gap: 11, alignItems: "flex-start" }}>
            <div style={{ width: 30, height: 30, borderRadius: 8, background: "var(--surface-3)", border: "1px solid var(--line)", display: "grid", placeItems: "center", fontFamily: "var(--font-mono)", fontSize: 11, fontWeight: 600, flexShrink: 0 }}>AK</div>
            <div style={{ fontSize: 15, fontWeight: 600, paddingTop: 4 }}>{conv.q}</div>
          </div>
          <Card bodyClass="card-pad">
            <div style={{ display: "flex", gap: 11, alignItems: "flex-start", marginBottom: conv.streaming ? 0 : 16 }}>
              <div className="brand-mark" style={{ flexShrink: 0 }}><Icon.spark style={{ width: 14, height: 14, color: "var(--accent-ink)" }} /></div>
              <div style={{ flex: 1 }}>
                <p style={{ fontSize: 14, lineHeight: 1.65, color: "var(--ink)", margin: 0 }}>{streamed}{conv.streaming && <span style={{ borderRight: "2px solid var(--accent)", marginLeft: 1 }}>&nbsp;</span>}</p>
              </div>
            </div>
            {!conv.streaming && (
              <div style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--line)" }}>
                <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
                  {conv.r.ev.length > 0 && <div><div className="eyebrow" style={{ marginBottom: 7 }}>Evidence</div><div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{conv.r.ev.map((id) => <SourceBadge key={id} id={id} inline />)}</div></div>}
                  {conv.r.events.length > 0 && <div><div className="eyebrow" style={{ marginBottom: 7 }}>Related Events</div><div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{conv.r.events.map((id) => { const e = D.eventsById[id]; if (!e) return null; return <span key={id} className="chip chip-x" onClick={() => Store.nav("event", id)}>{e.title.slice(0, 30)}…</span>; })}</div></div>}
                  {conv.r.companies.length > 0 && <div><div className="eyebrow" style={{ marginBottom: 7 }}>Companies</div><div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{conv.r.companies.map((id) => { const c = D.companies.find((x) => x.companyId === id); if (!c) return null; return <span key={id} className="chip chip-x" onClick={() => Store.nav("company", id)}><CompanyLogo company={c} size={18} />{c.name}</span>; })}</div></div>}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 16 }}>
                  <ConfidenceBadge score={0.71} />
                  <div style={{ flex: 1 }} />
                  <button className="btn btn-sm btn-ghost" onClick={() => { setConv(null); setQ(""); }}>New question</button>
                </div>
              </div>
            )}
          </Card>
          {!conv.streaming && (
            <div>
              <div className="eyebrow" style={{ marginBottom: 8 }}>Suggested follow-ups</div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {D.askSuggestions.filter((s) => s !== conv.q).slice(0, 3).map((s) => <span key={s} className="chip chip-x" onClick={() => { setQ(s); ask(s); }}>{s}</span>)}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---- Settings ------------------------------------------------------------- */
function SettingsPage() {
  return (
    <div className="page" style={{ maxWidth: 760 }}>
      <div className="page-head"><div className="titles"><div className="eyebrow">System</div><h1 className="page-title">Settings</h1></div></div>
      <div className="stack">
        <Card icon={Icon.spark} title="Appearance" eyebrow="Theme & display">
          <p style={{ fontSize: 13, color: "var(--ink-2)", margin: 0 }}>Use the <strong>theme toggle</strong> in the top bar to switch between dark and light, or open the <strong>Tweaks</strong> panel to adjust accent color, density, and typography live.</p>
        </Card>
        <Card icon={Icon.alerts} title="Notification Preferences">
          <div className="stack" style={{ gap: 12 }}>
            {["Risk threshold crossed", "New high-risk event", "Watchlist company event", "Forecast probability changed"].map((x, i) => (
              <div key={x} style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 13 }}>{x}</span>
                <span className="badge" style={{ background: i < 3 ? "var(--accent-soft)" : "var(--surface-3)", color: i < 3 ? "var(--accent)" : "var(--ink-3)", borderColor: i < 3 ? "var(--accent-line)" : "var(--line)" }}>{i < 3 ? "On" : "Off"}</span>
              </div>
            ))}
          </div>
        </Card>
        <Card icon={Icon.globe} title="Defaults">
          <div className="stack" style={{ gap: 10 }}>
            <Row k="Default region" v="Global" />
            <Row k="Default date range" v="Today" />
            <Row k="Confidence display" v="Numeric + label" />
          </div>
        </Card>
      </div>
    </div>
  );
}

Object.assign(window, { HistoricalPage, WatchlistPage, ReportsPage, AskPage, SettingsPage });
