/* SIGNAL — Daily Intelligence Dashboard */
const { useState, useEffect, useRef, useMemo } = React;

function GlobalEventMap({ points }) {
  return (
    <div style={{ position: "relative", borderRadius: 10, overflow: "hidden", background: "var(--bg-grid)", border: "1px solid var(--line)", aspectRatio: "2.1 / 1" }}>
      {/* simplified abstract world graticule */}
      <svg viewBox="0 0 100 50" preserveAspectRatio="none" style={{ position: "absolute", inset: 0, width: "100%", height: "100%", opacity: 0.4 }}>
        {[10, 20, 30, 40].map((y) => <line key={y} x1="0" y1={y} x2="100" y2={y} stroke="var(--line)" strokeWidth="0.2" />)}
        {[12.5, 25, 37.5, 50, 62.5, 75, 87.5].map((x) => <line key={x} x1={x} y1="0" x2={x} y2="50" stroke="var(--line)" strokeWidth="0.2" />)}
      </svg>
      {points.map((p) => {
        const lvl = scoreLevel(p.maxRiskScore);
        const col = levelColor(lvl);
        const size = 8 + p.eventCount * 1.6;
        return (
          <div key={p.locationName} className="map-pt" title={p.locationName}
            onClick={() => Store.nav("event", p.relatedEventIds[0])}
            style={{ position: "absolute", left: p.x * 100 + "%", top: p.y * 100 + "%", transform: "translate(-50%,-50%)", cursor: "pointer" }}>
            <div style={{ position: "absolute", inset: 0, width: size, height: size, borderRadius: "50%", background: col, opacity: 0.25, transform: "translate(-50%,-50%)", animation: "pulse 2.4s ease-in-out infinite" }} />
            <div style={{ width: size, height: size, borderRadius: "50%", background: col, transform: "translate(-50%,-50%)", boxShadow: `0 0 10px ${col}88`, border: "1.5px solid var(--bg)", display: "grid", placeItems: "center" }}>
              <span className="mono" style={{ fontSize: 8, color: "#04121c", fontWeight: 700 }}>{p.eventCount}</span>
            </div>
            <div style={{ position: "absolute", top: size / 2 + 2, left: "50%", transform: "translateX(-50%)", whiteSpace: "nowrap", fontSize: 9, color: "var(--ink-3)", fontFamily: "var(--font-mono)", marginTop: 2 }}>{p.locationName.split(" /")[0]}</div>
          </div>
        );
      })}
    </div>
  );
}

function UpcomingTriggers({ items }) {
  return (
    <div className="stack" style={{ gap: 0 }}>
      {items.map((t, i) => {
        const col = levelColor(t.importance === "critical" ? "critical" : t.importance === "high" ? "high" : "medium");
        return (
          <div key={t.id} onClick={() => t.relatedEventId && Store.nav("event", t.relatedEventId)}
            style={{ display: "flex", gap: 12, padding: "12px 0", borderBottom: i < items.length - 1 ? "1px solid var(--line-soft)" : "none", cursor: t.relatedEventId ? "pointer" : "default" }}>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", paddingTop: 2 }}>
              <Icon.clock style={{ width: 15, height: 15, color: col }} />
              {i < items.length - 1 && <div style={{ width: 1, flex: 1, background: "var(--line)", marginTop: 4 }} />}
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 2 }}>
                <span style={{ fontSize: 13, fontWeight: 600 }}>{t.title}</span>
                <span className={"badge badge-" + (t.importance === "critical" ? "crit" : t.importance === "high" ? "high" : "med")} style={{ fontSize: 9 }}>{t.importance}</span>
              </div>
              <div style={{ fontSize: 12, color: "var(--ink-3)", lineHeight: 1.4 }}>{t.reason}</div>
              {t.expectedAt && <div className="mono" style={{ fontSize: 10.5, color: "var(--ink-faint)", marginTop: 4 }}>{fmtTime(t.expectedAt)}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Dashboard() {
  const D = window.DATA;
  const sparks = D.riskTrends;
  const hotEvents = [...D.events].sort((a, b) => b.hotnessScore - a.hotnessScore);
  const topCompanies = [...D.companies].sort((a, b) => b.impactScore - a.impactScore).slice(0, 8);
  const overallRating = D.dailySummary.modelRating || {
    risk_score: 72,
    risk_level: D.dailySummary.overallRiskLevel,
    confidence_score: D.dailySummary.confidenceScore,
    probability_within_18m: null,
  };
  const overallScore = Math.round(overallRating.risk_score);

  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className="live-dot" /> Live · {new Date(D.dailySummary.date).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" })}
          </div>
          <h1 className="page-title">Daily Intelligence</h1>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-sm"><Icon.refresh style={{ width: 14, height: 14 }} /> Refresh</button>
          <button className="btn btn-sm"><Icon.download style={{ width: 14, height: 14 }} /> Export Brief</button>
        </div>
      </div>

      {/* Executive summary + overall risk */}
      <div className="grid" style={{ gridTemplateColumns: "1fr 280px", marginBottom: 16 }}>
        <Card icon={Icon.bolt} eyebrow={"AI Daily Brief · updated " + timeAgo(D.dailySummary.lastUpdatedAt)} title="Today's Intelligence Summary"
          action={<ConfidenceBadge score={D.dailySummary.confidenceScore} />}>
          <p style={{ fontSize: 14.5, lineHeight: 1.6, color: "var(--ink)", margin: "0 0 16px", textWrap: "pretty" }}>{D.dailySummary.summary}</p>
          <div className="eyebrow" style={{ marginBottom: 10 }}>Key Signals Today</div>
          <div className="stack" style={{ gap: 9 }}>
            {D.dailySummary.keyPoints.map((p, i) => (
              <div key={i} style={{ display: "flex", gap: 11, alignItems: "flex-start" }}>
                <span className="mono" style={{ fontSize: 11, color: "var(--accent)", fontWeight: 700, marginTop: 2, minWidth: 16 }}>{String(i + 1).padStart(2, "0")}</span>
                <span style={{ fontSize: 13.5, color: "var(--ink-2)", lineHeight: 1.5 }}>{p}</span>
              </div>
            ))}
          </div>
        </Card>
        <Card icon={Icon.shield} eyebrow="Model Rating" title="Risk Level">
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
            <Gauge value={overallScore} size={150} label="Composite" sub={overallRating.risk_type ? titleCaseLabel(overallRating.risk_type) : "weighted across 7 categories"} level={overallRating.risk_level} />
            <div style={{ textAlign: "center" }}>
              <RiskBadge level={overallRating.risk_level} label={titleCaseLabel(overallRating.risk_level) + " · " + overallScore + "/100"} />
            </div>
            {overallRating.probability_within_18m != null && (
              <div style={{ display: "flex", gap: 12, alignItems: "center", fontSize: 11.5, color: "var(--ink-3)" }}>
                <span className="mono" style={{ color: levelColor(overallRating.risk_level), fontWeight: 600 }}>{pct(overallRating.probability_within_18m)}</span>
                <span>within 18m</span>
              </div>
            )}
            <ConfidenceBadge score={overallRating.confidence_score} />
          </div>
        </Card>
      </div>

      {/* Metrics */}
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", marginBottom: 24 }}>
        {D.metrics.map((m, i) => (
          <MetricCard key={m.label} m={m} spark={i < 5 ? Object.values(sparks)[i] : null} />
        ))}
      </div>

      {/* Two-column: events | radar + triggers */}
      <div className="grid" style={{ gridTemplateColumns: "1fr 360px", alignItems: "start" }}>
        <div className="stack">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: -4 }}>
            <h2 style={{ fontSize: 15, display: "flex", alignItems: "center", gap: 8 }}><Icon.flame style={{ width: 17, height: 17, color: "var(--r-high)" }} /> Top Hot Events</h2>
            <button className="btn btn-sm btn-ghost" onClick={() => Store.nav("events")}>View all {D.events.length} <Icon.arrowRight style={{ width: 14, height: 14 }} /></button>
          </div>
          {hotEvents.slice(0, 4).map((e, i) => <HotEventCard key={e.id} e={e} rank={i + 1} />)}
        </div>

        <div className="stack" style={{ position: "sticky", top: 0 }}>
          <Card icon={Icon.radar} title="Risk Radar" eyebrow="7 categories"
            action={<button className="btn btn-sm btn-ghost" onClick={() => Store.nav("risk")}>Open</button>}>
            <div className="stack" style={{ gap: 0 }}>
              {D.riskRadar.map((r) => <RiskRow key={r.riskType} r={r} onClick={() => Store.nav("riskdetail", r.riskType)} />)}
            </div>
          </Card>

          <Card icon={Icon.clock} title="Upcoming Triggers" eyebrow="What to monitor next">
            <UpcomingTriggers items={D.upcomingTriggers} />
          </Card>
        </div>
      </div>

      {/* Industry heatmap */}
      <div style={{ marginTop: 24 }}>
        <Card icon={Icon.industries} title="Industry Heatmap" eyebrow="Impact intensity · click to drill in"
          action={<button className="btn btn-sm btn-ghost" onClick={() => Store.nav("industries")}>All industries <Icon.arrowRight style={{ width: 14, height: 14 }} /></button>}>
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))" }}>
            {D.industries.map((it) => <HeatCell key={it.industryId} it={it} />)}
          </div>
        </Card>
      </div>

      {/* Company ranking + map */}
      <div className="grid" style={{ gridTemplateColumns: "1.4fr 1fr", marginTop: 24, alignItems: "start" }}>
        <Card icon={Icon.companies} title="Company Impact Ranking" eyebrow="Most exposed today"
          action={<button className="btn btn-sm btn-ghost" onClick={() => Store.nav("companies")}>All <Icon.arrowRight style={{ width: 14, height: 14 }} /></button>} bodyClass="">
          <CompanyTable rows={topCompanies} />
        </Card>
        <Card icon={Icon.globe} title="Global Event Map" eyebrow="Concentration by location">
          <GlobalEventMap points={D.eventMap} />
          <div style={{ display: "flex", gap: 14, marginTop: 12, flexWrap: "wrap" }}>
            {[["low", "Low"], ["medium", "Medium"], ["high", "High"], ["critical", "Critical"]].map(([k, l]) => (
              <span key={k} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--ink-3)" }}>
                <span style={{ width: 8, height: 8, borderRadius: "50%", background: levelColor(k) }} />{l}
              </span>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

window.Dashboard = Dashboard;
