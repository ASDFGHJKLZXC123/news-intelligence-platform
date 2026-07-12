/* SIGNAL — Event List page */
const { useState, useEffect, useRef, useMemo } = React;

function EventFilters({ filters, setFilters, view, setView }) {
  const D = window.DATA;
  const allTypes = [...new Set(D.events.flatMap((e) => e.eventTypes))];
  const levels = ["low", "medium", "high", "critical"];
  const toggle = (key, val) => setFilters((f) => {
    const arr = new Set(f[key]); arr.has(val) ? arr.delete(val) : arr.add(val);
    return Object.assign({}, f, { [key]: arr });
  });
  const Group = ({ label, k, opts, render }) => (
    <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
      <span className="eyebrow">{label}</span>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {opts.map((o) => {
          const on = filters[k].has(o);
          return <span key={o} className="chip chip-x" onClick={() => toggle(k, o)}
            style={on ? { background: "var(--accent-soft)", borderColor: "var(--accent-line)", color: "var(--accent)" } : null}>
            {render ? render(o) : o}{on && <Icon.x style={{ width: 11, height: 11 }} />}</span>;
        })}
      </div>
    </div>
  );
  return (
    <Card className="" bodyClass="card-pad">
      <div style={{ display: "flex", gap: 26, flexWrap: "wrap", alignItems: "flex-start" }}>
        <Group label="Event Type" k="types" opts={allTypes} />
        <Group label="Risk Level" k="levels" opts={levels} render={(l) => l[0].toUpperCase() + l.slice(1)} />
        <div style={{ flex: 1 }} />
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          <span className="eyebrow">View</span>
          <div style={{ display: "flex", gap: 4, background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 8, padding: 3 }}>
            <button className={"btn btn-sm " + (view === "cards" ? "" : "btn-ghost")} style={{ padding: "5px 9px", background: view === "cards" ? "var(--surface-3)" : "transparent" }} onClick={() => setView("cards")}><Icon.grid style={{ width: 14, height: 14 }} /></button>
            <button className={"btn btn-sm " + (view === "table" ? "" : "btn-ghost")} style={{ padding: "5px 9px", background: view === "table" ? "var(--surface-3)" : "transparent" }} onClick={() => setView("table")}><Icon.list style={{ width: 14, height: 14 }} /></button>
          </div>
        </div>
      </div>
    </Card>
  );
}

function EventTable({ rows }) {
  const [sort, setSort] = useState({ key: "hotnessScore", dir: -1 });
  const sorted = useMemo(() => [...rows].sort((a, b) => {
    const av = a[sort.key], bv = b[sort.key];
    if (typeof av === "string") return av.localeCompare(bv) * sort.dir;
    return (av - bv) * sort.dir;
  }), [rows, sort]);
  const head = (key, label, cls) => <th className={cls} onClick={() => setSort((s) => ({ key, dir: s.key === key ? -s.dir : -1 }))}>{label}{sort.key === key && <span className="arrow">{sort.dir < 0 ? "↓" : "↑"}</span>}</th>;
  return (
    <Card bodyClass="">
      <div style={{ overflowX: "auto" }}>
        <table className="tbl">
          <thead><tr>
            {head("title", "Event")}
            <th className="no-sort">Type</th>
            {head("hotnessScore", "Hot")}
            {head("riskScore", "Risk")}
            {head("confidenceScore", "Conf")}
            <th className="no-sort">Industries</th>
            {head("sourceCount", "Src")}
            {head("firstSeenAt", "First Seen")}
          </tr></thead>
          <tbody>
            {sorted.map((e) => (
              <tr key={e.id} onClick={() => Store.nav("event", e.id)}>
                <td style={{ maxWidth: 320 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
                    <span style={{ width: 3, height: 30, borderRadius: 3, background: levelColor(e.riskLevel), flexShrink: 0 }} />
                    <span style={{ fontWeight: 550, fontSize: 13, lineHeight: 1.3 }}>{e.title}</span>
                  </div>
                </td>
                <td><span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{e.eventTypes.join(" · ")}</span></td>
                <td><HotMeter score={e.hotnessScore} /></td>
                <td><MiniBar value={e.riskScore} risk /></td>
                <td><span className="mono" style={{ fontSize: 12.5 }}>{e.confidenceScore.toFixed(2)}</span></td>
                <td><div style={{ display: "flex", gap: 4 }}>{e.affectedIndustries.slice(0, 2).map((i) => <span key={i} className="chip" style={{ fontSize: 10.5, padding: "2px 7px" }}>{i}</span>)}{e.affectedIndustries.length > 2 && <span className="muted" style={{ fontSize: 11 }}>+{e.affectedIndustries.length - 2}</span>}</div></td>
                <td><span className="mono" style={{ fontSize: 12.5 }}>{e.sourceCount}</span></td>
                <td><span className="mono" style={{ fontSize: 11.5, color: "var(--ink-faint)" }}>{timeAgo(e.firstSeenAt)}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function EventsPage() {
  const D = window.DATA;
  const [view, setView] = useState("cards");
  const [sortKey, setSortKey] = useState("hotnessScore");
  const [filters, setFilters] = useState({ types: new Set(), levels: new Set() });

  const filtered = useMemo(() => {
    let r = D.events.filter((e) => {
      if (filters.types.size && !e.eventTypes.some((t) => filters.types.has(t))) return false;
      if (filters.levels.size && !filters.levels.has(e.riskLevel)) return false;
      return true;
    });
    r.sort((a, b) => b[sortKey] - a[sortKey]);
    return r;
  }, [filters, sortKey]);

  const activeFilterCount = filters.types.size + filters.levels.size;

  return (
    <div className="page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow">Intelligence · Event Clusters</div>
          <h1 className="page-title">Events</h1>
          <div className="page-sub">{filtered.length} of {D.events.length} detected event clusters today</div>
        </div>
      </div>

      <div className="filter-toolbar events-toolbar">
        <button className="btn events-toolbar-control"><Icon.calendar style={{ width: 15, height: 15 }} /> Today</button>
        <select className="btn events-toolbar-control events-sort" value={sortKey} onChange={(e) => setSortKey(e.target.value)}>
          <option value="hotnessScore">Sort: Hotness</option>
          <option value="riskScore">Sort: Risk</option>
          <option value="confidenceScore">Sort: Confidence</option>
          <option value="sourceCount">Sort: Sources</option>
        </select>
      </div>

      <div style={{ marginBottom: 18 }}><EventFilters filters={filters} setFilters={setFilters} view={view} setView={setView} /></div>

      {filtered.length === 0 ? (
        <Card><EmptyState icon={Icon.filter} title="No events match the current filters" hint="Try clearing a filter."
          action={<button className="btn btn-sm" onClick={() => { setFilters({ types: new Set(), levels: new Set() }); }}>Reset filters</button>} /></Card>
      ) : view === "cards" ? (
        <div className="grid" style={{ gridTemplateColumns: "repeat(2, 1fr)" }}>
          {filtered.map((e) => <HotEventCard key={e.id} e={e} />)}
        </div>
      ) : (
        <EventTable rows={filtered} />
      )}
    </div>
  );
}

window.EventsPage = EventsPage;
