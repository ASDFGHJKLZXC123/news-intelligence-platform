/* SIGNAL — Risk Radar + Risk Detail pages */
const { useState, useEffect, useRef, useMemo } = React;

function RiskCategoryCard({ r }) {
  const col = levelColor(r.level);
  return (
    <div className="card card-pad" style={{ cursor: "pointer", display: "flex", flexDirection: "column", gap: 12 }}
      onClick={() => Store.nav("riskdetail", r.riskType)}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{r.riskType}</div>
          <div style={{ marginTop: 5 }}><RiskBadge level={r.level} /></div>
        </div>
        <div className="mono" style={{ fontSize: 30, fontWeight: 600, color: col, lineHeight: 1, flexShrink: 0 }}>{r.score}</div>
      </div>
      <div className="scorebar" style={{ height: 6 }}><i style={{ width: r.score + "%", background: col }} /></div>
      <div style={{ display: "flex", gap: 14 }}>
        <div><div className="eyebrow" style={{ fontSize: 9 }}>24h</div><Delta value={r.change24h} /></div>
        <div><div className="eyebrow" style={{ fontSize: 9 }}>7d</div><Delta value={r.change7d} /></div>
        <div style={{ flex: 1, textAlign: "right", alignSelf: "flex-end" }}><ConfidenceBadge score={r.confidenceScore} /></div>
      </div>
      <div style={{ paddingTop: 10, borderTop: "1px solid var(--line-soft)" }}>
        <div className="eyebrow" style={{ marginBottom: 3, fontSize: 9 }}>Top Driver</div>
        <div style={{ fontSize: 12, color: "var(--ink-2)" }}>{r.topDriver}</div>
      </div>
    </div>
  );
}

function RiskRadarPage() {
  const D = window.DATA;
  const [hl, setHl] = useState(["Geopolitical Risk", "Policy / Regulatory", "Supply Chain Risk"]);
  const trendColors = { "Geopolitical Risk": "var(--r-crit)", "Policy / Regulatory": "var(--r-high)", "Supply Chain Risk": "var(--r-med)", "Macro Risk": "var(--accent)", "Financial Stress": "var(--r-low)", "Industry Shock": "#a78bfa", "Company Crisis": "#8b93a7" };
  const series = D.riskRadar.map((r) => ({ name: r.riskType, data: D.riskTrends[r.riskType] || [], color: trendColors[r.riskType], bold: hl.includes(r.riskType), dim: !hl.includes(r.riskType) }));
  const highRiskEvents = [...D.events].filter((e) => e.riskScore >= 60).sort((a, b) => b.riskScore - a.riskScore);

  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow" style={{ display: "flex", alignItems: "center", gap: 8 }}><span className="live-dot" /> Risk Intelligence</div>
          <h1 className="page-title">Risk Radar</h1>
          <div className="page-sub">Global and thematic risk monitoring across 7 categories</div>
          <PageStatus blocks={["riskRadar", "riskTrends", "riskDetails"]} />
        </div>
        <RiskBadge level="high" label="Overall · High" />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1.1fr 1fr", marginBottom: 24, alignItems: "stretch" }}>
        <Card icon={Icon.radar} title="Risk Surface" eyebrow="All categories · 0–100">
          <div style={{ display: "flex", justifyContent: "center", padding: "10px 0" }}>
            <RadarChart items={D.riskRadar} size={380} />
          </div>
        </Card>
        <Card icon={Icon.trendUp} title="30-Day Risk Trends" eyebrow="Click legend to focus" bodyClass="card-pad">
          <TrendChart series={series} w={560} h={250} />
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}>
            {D.riskRadar.map((r) => (
              <span key={r.riskType} className="chip chip-x" onClick={() => setHl((h) => h.includes(r.riskType) ? h.filter((x) => x !== r.riskType) : [...h, r.riskType])}
                style={hl.includes(r.riskType) ? { borderColor: trendColors[r.riskType], color: "var(--ink)" } : { opacity: 0.55 }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: trendColors[r.riskType] }} />{r.riskType.replace(" Risk", "").replace(" / Regulatory", "/Reg")}
              </span>
            ))}
          </div>
        </Card>
      </div>

      <h2 style={{ fontSize: 15, marginBottom: 14, display: "flex", alignItems: "center", gap: 8 }}><Icon.shield style={{ width: 17, height: 17, color: "var(--accent)" }} /> Risk Categories</h2>
      <div className="grid" style={{ gridTemplateColumns: "repeat(4, 1fr)", marginBottom: 24 }}>
        {D.riskRadar.map((r) => <RiskCategoryCard key={r.riskType} r={r} />)}
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1.3fr 1fr", alignItems: "start" }}>
        <Card icon={Icon.flame} title="High-Risk Events" eyebrow={highRiskEvents.length + " above threshold"} bodyClass="">
          <div className="stack" style={{ gap: 0 }}>
            {highRiskEvents.map((e, i) => (
              <div key={e.id} onClick={() => Store.nav("event", e.id)} style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 18px", borderBottom: i < highRiskEvents.length - 1 ? "1px solid var(--line-soft)" : "none", cursor: "pointer" }}>
                <span style={{ width: 4, height: 32, borderRadius: 3, background: levelColor(e.riskLevel), flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 550, lineHeight: 1.3 }}>{e.title}</div>
                  <div style={{ fontSize: 11, color: "var(--ink-faint)", marginTop: 2 }}>{e.eventType}</div>
                </div>
                <MiniBar value={e.riskScore} risk />
                <Icon.chevR style={{ width: 15, height: 15, color: "var(--ink-faint)" }} />
              </div>
            ))}
          </div>
        </Card>
        <Card icon={Icon.target} title="Watch Indicators" eyebrow="Monitor next">
          <div className="stack" style={{ gap: 12 }}>
            {[
              { n: "Labor-market data", s: "Friday", st: "watch" },
              { n: "Export-control details", s: "≤72h", st: "warning" },
              { n: "Regional deposit flows", s: "Daily", st: "watch" },
              { n: "Gulf shipping status", s: "Live", st: "critical" },
              { n: "Central-bank policy", s: "Jun 12", st: "neutral" },
            ].map((x) => {
              const sc = { neutral: "var(--ink-3)", watch: "var(--r-med)", warning: "var(--r-high)", critical: "var(--r-crit)" }[x.st];
              return (
                <div key={x.n} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span style={{ width: 8, height: 8, borderRadius: "50%", background: sc, flexShrink: 0 }} />
                  <span style={{ flex: 1, fontSize: 13, color: "var(--ink-2)" }}>{x.n}</span>
                  <span className="mono" style={{ fontSize: 11.5, color: "var(--ink-faint)" }}>{x.s}</span>
                </div>
              );
            })}
          </div>
        </Card>
      </div>
    </div>
  );
}

/* ---- Risk Detail ---------------------------------------------------------- */
function RiskDetailPage({ type }) {
  const D = window.DATA;
  const r = D.riskRadar.find((x) => x.riskType === type) || D.riskRadar[0];
  if (!r) return (
    <div className="page">
      <button className="btn btn-sm btn-ghost" style={{ marginBottom: 16 }} onClick={() => Store.nav("risk")}><Icon.chevL style={{ width: 15, height: 15 }} /> Risk Radar</button>
      <Card><EmptyState icon={Icon.radar} title="No risk categories available" hint="Live risk-radar data has not loaded yet." /></Card>
    </div>
  );
  const detail = D.riskDetails[type] || D.riskDetails[r.riskType] || {};
  const horizons = Array.isArray(detail.probabilityByHorizon) ? detail.probabilityByHorizon : [];
  const lastHorizon = horizons.length ? horizons[horizons.length - 1] : null;
  const signals = Array.isArray(detail.signals) ? detail.signals : [];
  const mainDrivers = Array.isArray(detail.mainDrivers) ? detail.mainDrivers : [];
  const leadingIndicators = Array.isArray(detail.leadingIndicators) ? detail.leadingIndicators : [];
  const invalidationSignals = Array.isArray(detail.invalidationSignals) ? detail.invalidationSignals : [];
  const col = levelColor(r.level);
  const statusColor = { positive: "var(--r-low)", neutral: "var(--ink-3)", watch: "var(--r-med)", warning: "var(--r-high)", critical: "var(--r-crit)" };

  return (
    <div className="page">
      <button className="btn btn-sm btn-ghost" style={{ marginBottom: 16 }} onClick={() => Store.nav("risk")}><Icon.chevL style={{ width: 15, height: 15 }} /> Risk Radar</button>

      <div className="grid" style={{ gridTemplateColumns: "1fr 300px", alignItems: "start" }}>
        <div className="stack">
          <Card bodyClass="card-pad">
            <div style={{ display: "flex", gap: 20, alignItems: "center" }}>
              <Gauge value={r.score} size={140} label="Score" sub={r.level + " severity"} />
              <div style={{ flex: 1 }}>
                <div className="eyebrow">Risk Category</div>
                <h1 style={{ fontSize: 24, margin: "4px 0 10px" }}>{type}</h1>
                <p style={{ fontSize: 13.5, color: "var(--ink-2)", lineHeight: 1.55, margin: "0 0 14px", maxWidth: 480 }}>
                  Composite score across leading indicators, market signals, and news velocity. {lastHorizon ? `Estimated ${Math.round(lastHorizon.probability * 100)}% probability over ${SignalDataQuality.horizonLabel(lastHorizon.horizon)}.` : ""}
                </p>
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
                  <RiskBadge level={r.level} /><ConfidenceBadge score={r.confidenceScore} />
                  <span className="badge badge-neutral">24h <Delta value={r.change24h} /></span>
                  <BlockBadge name="riskDetails" />
                </div>
              </div>
            </div>
          </Card>

          {horizons.length > 0 && (
            <Card icon={Icon.scale} title="Estimated Probability by Horizon">
              <div className="grid" style={{ gridTemplateColumns: "repeat(" + horizons.length + ", 1fr)" }}>
                {horizons.map((p) => (
                  <div key={p.horizon} style={{ textAlign: "center", padding: 14, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)" }}>
                    <div className="mono" style={{ fontSize: 28, fontWeight: 600, color: col }}>{Math.round(p.probability * 100)}%</div>
                    <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 3 }}>over {SignalDataQuality.horizonLabel(p.horizon)}</div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card icon={Icon.activity} title="Signal Breakdown" bodyClass="">
            {signals.length === 0 ? (
              <div className="card-pad"><EmptyState icon={Icon.activity} title="No live signal breakdown" hint="Per-signal detail is not available for this risk category yet." /></div>
            ) : (
            <table className="tbl">
              <thead><tr><th className="no-sort">Signal</th><th className="no-sort">Value</th><th className="no-sort">Status</th><th className="no-sort">Explanation</th><th className="no-sort">Updated</th></tr></thead>
              <tbody>
                {signals.map((s) => (
                  <tr key={s.name} style={{ cursor: "default" }}>
                    <td><span style={{ fontSize: 13, fontWeight: 550 }}>{s.name}</span></td>
                    <td><span className="mono" style={{ fontSize: 12.5 }}>{s.value}</span></td>
                    <td><span className="badge" style={{ color: statusColor[s.status], background: `color-mix(in oklch, ${statusColor[s.status]} 14%, transparent)`, borderColor: `color-mix(in oklch, ${statusColor[s.status]} 30%, transparent)` }}>{s.status}</span></td>
                    <td><span style={{ fontSize: 12, color: "var(--ink-2)" }}>{s.explanation}</span></td>
                    <td><span className="mono" style={{ fontSize: 11, color: "var(--ink-faint)" }}>{timeAgo(s.lastUpdatedAt)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
            )}
          </Card>

          <Card icon={Icon.trendUp} title="30-Day Trend">
            <TrendChart series={[{ name: type, data: D.riskTrends[type] || D.riskTrends[r.riskType] || [], color: col, bold: true }]} w={640} h={200} />
          </Card>
        </div>

        <div className="stack" style={{ position: "sticky", top: 0 }}>
          <Card icon={Icon.bolt} title="Main Drivers">
            {mainDrivers.length ? <div className="stack" style={{ gap: 8 }}>{mainDrivers.map((d) => <div key={d} style={{ display: "flex", gap: 9, fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.45 }}><span style={{ width: 5, height: 5, borderRadius: "50%", background: col, marginTop: 6, flexShrink: 0 }} />{d}</div>)}</div> : <span className="muted" style={{ fontSize: 12.5 }}>No drivers recorded.</span>}
          </Card>
          <Card icon={Icon.target} title="Leading Indicators">
            {leadingIndicators.length ? <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{leadingIndicators.map((d) => <span key={d} className="chip" style={{ fontSize: 11 }}>{d}</span>)}</div> : <span className="muted" style={{ fontSize: 12.5 }}>No leading indicators available.</span>}
          </Card>
          <Card icon={Icon.shield} title="Invalidation Signals">
            {invalidationSignals.length ? <div className="stack" style={{ gap: 7 }}>{invalidationSignals.map((d) => <div key={d} style={{ display: "flex", gap: 8, fontSize: 12.5, color: "var(--ink-2)" }}><Icon.check style={{ width: 13, height: 13, color: "var(--r-low)", flexShrink: 0, marginTop: 1 }} />{d}</div>)}</div> : <span className="muted" style={{ fontSize: 12.5 }}>No invalidation signals available.</span>}
          </Card>
          <WatchBtn id={"risk-" + type} />
        </div>
      </div>
    </div>
  );
}

window.RiskRadarPage = RiskRadarPage;
window.RiskDetailPage = RiskDetailPage;
