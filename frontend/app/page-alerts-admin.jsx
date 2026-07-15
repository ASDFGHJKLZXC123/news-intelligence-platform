/* SIGNAL — Alerts + Admin (job/source/model monitor) */
const { useState, useEffect, useRef, useMemo } = React;

/* ---- Alerts --------------------------------------------------------------- */
function AlertCard({ a }) {
  const D = window.DATA;
  const ev = a.relatedEventId ? D.eventsById[a.relatedEventId] : null;
  return (
    <div className="card" style={{ overflow: "hidden", cursor: a.relatedEventId ? "pointer" : "default" }} onClick={() => a.relatedEventId && Store.nav("event", a.relatedEventId)}>
      <div style={{ display: "flex" }}>
        <div style={{ width: 4, background: levelColor(a.severity) }} />
        <div className="card-pad" style={{ flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 8, flexWrap: "wrap" }}>
            <RiskBadge level={a.severity} />
            <span className="badge badge-neutral" style={{ fontSize: 9.5 }}>{a.alertType}</span>
            <div style={{ flex: 1 }} />
            {a.status === "new" ? <span className="badge badge-accent"><span className="dot" /> New</span> : <span className="badge badge-neutral" style={{ textTransform: "capitalize" }}>{a.status}</span>}
            <span className="mono" style={{ fontSize: 10.5, color: "var(--ink-faint)" }}>{timeAgo(a.createdAt)}</span>
          </div>
          <div style={{ fontSize: 14.5, fontWeight: 600, marginBottom: 4 }}>{a.title}</div>
          <p style={{ fontSize: 13, color: "var(--ink-2)", margin: "0 0 11px", lineHeight: 1.5 }}>{a.message}</p>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11.5, color: "var(--ink-3)" }}><Icon.shield style={{ width: 13, height: 13 }} /> Risk <span className="mono" style={{ color: levelColor(scoreLevel(a.riskScore)), fontWeight: 600 }}>{a.riskScore}</span></span>
            {ev && <span className="chip">{ev.title.slice(0, 42)}{ev.title.length > 42 ? "…" : ""}</span>}
            {a.evidenceSourceIds && a.evidenceSourceIds.length > 0 && <div style={{ display: "flex", gap: 6 }}>{a.evidenceSourceIds.slice(0, 2).map((s) => <SourceBadge key={s} id={s} inline />)}</div>}
            <div style={{ flex: 1 }} />
            {a.status === "new" && <button className="btn btn-sm btn-ghost" onClick={(e) => { e.stopPropagation(); Store.flash("Alert acknowledged"); }}>Acknowledge</button>}
          </div>
        </div>
      </div>
    </div>
  );
}

function AlertsPage() {
  const D = window.DATA;
  const [sev, setSev] = useState("all");
  const [status, setStatus] = useState("all");
  const rows = D.alerts.filter((a) => (sev === "all" || a.severity === sev) && (status === "all" || a.status === status));
  const counts = { new: D.alerts.filter((a) => a.status === "new").length, critical: D.alerts.filter((a) => a.severity === "critical").length, high: D.alerts.filter((a) => a.severity === "high").length };
  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow" style={{ display: "flex", alignItems: "center", gap: 8 }}><span className="live-dot" style={{ background: "var(--r-high)" }} /> Workspace</div>
          <h1 className="page-title">Alerts</h1>
          <div className="page-sub">{counts.new} new · {counts.critical} critical · {counts.high} high</div>
          <PageStatus blocks={["alerts"]} />
        </div>
        <button className="btn btn-sm"><Icon.check style={{ width: 14, height: 14 }} /> Mark all read</button>
      </div>

      <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 4, background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 8, padding: 3 }}>
          {["all", "critical", "high", "medium"].map((s) => <button key={s} className="btn btn-sm" style={{ background: sev === s ? "var(--surface-3)" : "transparent", border: "none", color: sev === s ? "var(--ink)" : "var(--ink-3)", textTransform: "capitalize" }} onClick={() => setSev(s)}>{s}</button>)}
        </div>
        <div style={{ display: "flex", gap: 4, background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 8, padding: 3 }}>
          {["all", "new", "acknowledged", "resolved"].map((s) => <button key={s} className="btn btn-sm" style={{ background: status === s ? "var(--surface-3)" : "transparent", border: "none", color: status === s ? "var(--ink)" : "var(--ink-3)", textTransform: "capitalize" }} onClick={() => setStatus(s)}>{s}</button>)}
        </div>
      </div>

      {rows.length ? <div className="stack">{rows.map((a) => <AlertCard key={a.id} a={a} />)}</div> :
        <Card><EmptyState icon={Icon.alerts} title="No alerts match" hint="Add companies or industries to your watchlist to receive targeted alerts." action={<button className="btn btn-sm btn-primary" onClick={() => Store.nav("watchlist")}>Open watchlist</button>} /></Card>}
    </div>
  );
}

/* ---- Admin ---------------------------------------------------------------- */
function StatusDot({ status }) {
  const map = { healthy: "var(--r-low)", degraded: "var(--r-med)", down: "var(--r-crit)", succeeded: "var(--r-low)", running: "var(--accent)", queued: "var(--ink-3)", retrying: "var(--r-med)", failed: "var(--r-crit)", partially_failed: "var(--r-med)", cancelled: "var(--ink-faint)" };
  const c = map[status] || "var(--ink-3)";
  return <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 12, color: c, fontWeight: 600, fontFamily: "var(--font-mono)" }}>
    <span style={{ width: 7, height: 7, borderRadius: "50%", background: c, animation: status === "running" ? "pulse 1.4s ease-in-out infinite" : "none" }} />{status}</span>;
}

function AdminPage() {
  const D = window.DATA;
  const [tab, setTab] = useState("jobs");
  const tabs = [{ id: "jobs", label: "Job Monitor", icon: Icon.activity }, { id: "sources", label: "Sources", icon: Icon.db }, { id: "models", label: "Models", icon: Icon.cpu }];
  const jobStats = { running: D.adminJobs.filter((j) => j.status === "running").length, failed: D.adminJobs.filter((j) => j.status === "failed").length, succeeded: D.adminJobs.filter((j) => j.status === "succeeded").length };
  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow">System · Pipeline Monitor</div>
          <h1 className="page-title">Admin</h1>
          <div className="page-sub">Ingestion, processing, and model operations</div>
          <PageStatus blocks={["adminJobs", "adminSources", "adminModels"]} />
        </div>
        <button className="btn btn-sm"><Icon.refresh style={{ width: 14, height: 14 }} /> Refresh</button>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "repeat(4, 1fr)", marginBottom: 20 }}>
        {[
          { l: "Jobs Running", v: jobStats.running, c: "var(--accent)" },
          { l: "Jobs Failed", v: jobStats.failed, c: "var(--r-crit)" },
          { l: "Sources Healthy", v: D.adminSources.filter((s) => s.status === "healthy").length + "/" + D.adminSources.length, c: "var(--r-low)" },
          { l: "Articles Today", v: D.adminSources.reduce((a, s) => a + (s.articlesFetchedToday || 0), 0).toLocaleString(), c: "var(--ink)" },
        ].map((x) => (
          <div key={x.l} className="card card-pad"><div className="eyebrow">{x.l}</div><div className="mono" style={{ fontSize: 26, fontWeight: 600, color: x.c, marginTop: 6 }}>{x.v}</div></div>
        ))}
      </div>

      <div className="tabs" style={{ marginBottom: 18 }}>
        {tabs.map((t) => { const Ic = t.icon; return <div key={t.id} className={"tab" + (tab === t.id ? " active" : "")} onClick={() => setTab(t.id)}><Ic style={{ width: 14, height: 14 }} />{t.label}</div>; })}
      </div>

      {tab === "jobs" && (
        <Card bodyClass="">
          <table className="tbl">
            <thead><tr><th className="no-sort">Job</th><th className="no-sort">Status</th><th className="no-sort">Started</th><th className="no-sort">Duration</th><th className="no-sort">Throughput</th><th className="no-sort"></th></tr></thead>
            <tbody>
              {D.adminJobs.map((j) => (
                <tr key={j.id} style={{ cursor: "default" }}>
                  <td><span style={{ fontSize: 13, fontWeight: 550 }}>{j.jobType}</span>{j.errorMessage && <div style={{ fontSize: 11.5, color: "var(--r-crit)", marginTop: 3 }}>{j.errorMessage}</div>}</td>
                  <td><StatusDot status={j.status} /></td>
                  <td><span className="mono" style={{ fontSize: 12 }}>{j.startedAt || "—"}</span></td>
                  <td><span className="mono" style={{ fontSize: 12 }}>{j.durationMs ? (j.durationMs / 1000).toFixed(1) + "s" : "—"}</span></td>
                  <td><span className="mono" style={{ fontSize: 12, color: "var(--ink-2)" }}>{j.throughput}</span></td>
                  <td>{j.status === "failed" && <button className="btn btn-sm" onClick={() => Store.flash("Retry queued")}>Retry</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
      {tab === "sources" && (
        <Card bodyClass="">
          <table className="tbl">
            <thead><tr><th className="no-sort">Source</th><th className="no-sort">Type</th><th className="no-sort">Status</th><th className="no-sort">Last Fetch</th><th className="no-sort">Articles Today</th><th className="no-sort">Error Rate</th><th className="no-sort">Latency</th></tr></thead>
            <tbody>
              {D.adminSources.map((s) => (
                <tr key={s.sourceId} style={{ cursor: "default" }}>
                  <td><span style={{ fontSize: 13, fontWeight: 550 }}>{s.name}</span></td>
                  <td><span className="mono" style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{s.sourceType}</span></td>
                  <td><StatusDot status={s.status} /></td>
                  <td><span className="mono" style={{ fontSize: 12 }}>{s.lastFetchedAt || "—"}</span></td>
                  <td><span className="mono" style={{ fontSize: 12.5 }}>{SignalDataQuality.intOr(s.articlesFetchedToday)}</span></td>
                  <td><span className="mono" style={{ fontSize: 12.5, color: typeof s.errorRate === "number" ? (s.errorRate > 5 ? "var(--r-crit)" : s.errorRate > 1 ? "var(--r-med)" : "var(--ink-2)") : "var(--ink-3)" }}>{typeof s.errorRate === "number" ? s.errorRate + "%" : "—"}</span></td>
                  <td><span className="mono" style={{ fontSize: 12.5 }}>{SignalDataQuality.unitOr(s.averageLatencyMs, "ms")}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
      {tab === "models" && (
        <Card bodyClass="">
          <table className="tbl">
            <thead><tr><th className="no-sort">Model</th><th className="no-sort">Task</th><th className="no-sort">Requests</th><th className="no-sort">Success</th><th className="no-sort">Latency</th><th className="no-sort">Avg Cost</th><th className="no-sort">Schema Valid</th><th className="no-sort">Fallbacks</th></tr></thead>
            <tbody>
              {D.adminModels.map((m) => (
                <tr key={m.modelName} style={{ cursor: "default" }}>
                  <td><div style={{ fontSize: 13, fontWeight: 600 }} className="mono">{m.modelName}</div><div style={{ fontSize: 11, color: "var(--ink-faint)" }}>{m.provider}</div></td>
                  <td><span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>{m.taskType}</span></td>
                  <td><span className="mono" style={{ fontSize: 12.5 }}>{SignalDataQuality.intOr(m.requestCount)}</span></td>
                  <td><span className="mono" style={{ fontSize: 12.5, color: typeof m.successRate === "number" ? (m.successRate > 99 ? "var(--r-low)" : "var(--r-med)") : "var(--ink-3)" }}>{SignalDataQuality.pctOr(m.successRate)}</span></td>
                  <td><span className="mono" style={{ fontSize: 12.5 }}>{SignalDataQuality.unitOr(m.averageLatencyMs, "ms")}</span></td>
                  <td><span className="mono" style={{ fontSize: 12.5 }}>{SignalDataQuality.moneyOr(m.averageCostUsd, 4)}</span></td>
                  <td><div style={{ width: 80 }}><MiniBar value={typeof m.schemaValidityRate === "number" ? Math.round(m.schemaValidityRate) : null} /></div></td>
                  <td><span className="mono" style={{ fontSize: 12.5, color: typeof m.fallbackCount === "number" && m.fallbackCount > 15 ? "var(--r-med)" : "var(--ink-2)" }}>{SignalDataQuality.numOr(m.fallbackCount)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

Object.assign(window, { AlertsPage, AdminPage, StatusDot });
