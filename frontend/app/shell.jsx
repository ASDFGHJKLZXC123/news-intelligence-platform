/* SIGNAL — global store + app shell (sidebar, topbar, command palette, drawer). */
const { useState, useEffect, useRef, useMemo } = React;

/* ---- tiny pub/sub store --------------------------------------------------- */
const Store = (function () {
  let state = {
    route: { name: "dashboard", param: null },
    drawer: null,                 // { type, data }
    watch: new Set(["nvda", "wal", "semis"]),
    paletteOpen: false,
    topbarMenu: null,
    dateScope: "today",
    regionScope: "global",
    toast: null,
  };
  const subs = new Set();
  const notify = () => subs.forEach((f) => f());
  return {
    get: () => state,
    set: (patch) => { state = Object.assign({}, state, typeof patch === "function" ? patch(state) : patch); notify(); },
    subscribe: (f) => { subs.add(f); return () => subs.delete(f); },
    nav: (name, param = null) => {
      state = Object.assign({}, state, { route: { name, param }, paletteOpen: false, topbarMenu: null });
      notify();
      const m = document.querySelector(".main"); if (m) m.scrollTop = 0;
    },
    toggleWatch: (id) => {
      const w = new Set(state.watch);
      w.has(id) ? w.delete(id) : w.add(id);
      const added = w.has(id);
      state = Object.assign({}, state, { watch: w, toast: (added ? "Added to watchlist" : "Removed from watchlist") });
      notify();
      clearTimeout(Store._tt); Store._tt = setTimeout(() => { state = Object.assign({}, state, { toast: null }); notify(); }, 1900);
    },
    openDrawer: (d) => { state = Object.assign({}, state, { drawer: d, topbarMenu: null }); notify(); },
    closeDrawer: () => { state = Object.assign({}, state, { drawer: null, topbarMenu: null }); notify(); },
    flash: (msg) => { state = Object.assign({}, state, { toast: msg }); notify(); clearTimeout(Store._tt); Store._tt = setTimeout(() => { state = Object.assign({}, state, { toast: null }); notify(); }, 1900); },
  };
})();
window.Store = Store;

function useStore() {
  const [, force] = useState(0);
  useEffect(() => Store.subscribe(() => force((n) => n + 1)), []);
  return Store.get();
}
window.useStore = useStore;

/* ---- nav config ----------------------------------------------------------- */
const NAV = [
  { group: "Intelligence", items: [
    { name: "dashboard", label: "Dashboard", icon: Icon.dashboard },
    { name: "events", label: "Events", icon: Icon.events, count: () => window.DATA.events.length },
    { name: "geo", label: "Geo Intel", icon: Icon.globe },
    { name: "risk", label: "Risk Radar", icon: Icon.radar },
    { name: "industries", label: "Industries", icon: Icon.industries },
    { name: "companies", label: "Companies", icon: Icon.companies },
    { name: "historical", label: "Historical", icon: Icon.historical },
  ]},
  { group: "Workspace", items: [
    { name: "alerts", label: "Alerts", icon: Icon.alerts, count: () => window.DATA.alerts.filter((a) => a.status === "new").length, hot: true },
    { name: "watchlist", label: "Watchlist", icon: Icon.watchlist },
    { name: "reports", label: "Reports", icon: Icon.reports },
    { name: "ask", label: "Ask AI", icon: Icon.ask },
  ]},
  { group: "System", items: [
    { name: "admin", label: "Admin", icon: Icon.admin },
    { name: "settings", label: "Settings", icon: Icon.settings },
  ]},
];

/* route → nav name for active state */
function activeNav(route) {
  const map = { event: "events", industry: "industries", company: "companies", riskdetail: "risk" };
  return map[route.name] || route.name;
}

/* ---- Sidebar -------------------------------------------------------------- */
function Sidebar({ collapsed, route }) {
  const active = activeNav(route);
  const radar = window.DATA.riskRadar || [];
  // Prefer these three categories; fall back to whatever risk types exist (the
  // live risk vocabulary differs from the demo fixture), and tolerate none.
  const preferred = ["Macro Risk", "Financial Stress", "Geopolitical Risk"]
    .map((t) => radar.find((r) => r.riskType === t)).filter(Boolean);
  const top3 = (preferred.length ? preferred : radar).slice(0, 3);
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          <Icon.spark style={{ width: 15, height: 15, color: "var(--accent-ink)" }} />
        </div>
        <span className="brand-name">SIGNAL</span>
      </div>
      <nav className="nav">
        {NAV.map((g) => (
          <React.Fragment key={g.group}>
            <div className="nav-section">{g.group}</div>
            {g.items.map((it) => {
              const Ic = it.icon;
              const cnt = it.count ? it.count() : null;
              return (
                <div key={it.name} className={"nav-item" + (active === it.name ? " active" : "")}
                  onClick={() => Store.nav(it.name)} title={it.label}>
                  <Ic className="ico" />
                  <span className="label">{it.label}</span>
                  {cnt != null && cnt > 0 && (
                    <span className="badge-count" style={it.hot ? { background: "color-mix(in oklch, var(--r-high) 20%, transparent)", color: "var(--r-high)" } : null}>{cnt}</span>
                  )}
                </div>
              );
            })}
          </React.Fragment>
        ))}
      </nav>
      <div className="sb-risk">
        <div className="eyebrow" style={{ marginBottom: 2 }}>Risk Status</div>
        {top3.map((r) => (
          <div className="sb-risk-row" key={r.riskType} onClick={() => Store.nav("riskdetail", r.riskType)} style={{ cursor: "pointer" }}>
            <span className="lbl">{r.riskType}</span>
            <div className="sb-risk-bar"><i style={{ width: r.score + "%", background: levelColor(r.level) }} /></div>
            <span className="mono" style={{ fontSize: 11, color: levelColor(r.level), width: 22, textAlign: "right", fontWeight: 600 }}>{r.score}</span>
          </div>
        ))}
      </div>
    </aside>
  );
}

/* ---- Topbar --------------------------------------------------------------- */
const DATE_SCOPES = [
  { id: "today", label: "Today", hint: "Current intelligence cycle" },
  { id: "7d", label: "7D", hint: "Signals from the last week" },
  { id: "30d", label: "30D", hint: "Thirty-day trend window" },
];
const REGION_SCOPES = [
  { id: "global", label: "Global", hint: "All monitored regions" },
  { id: "americas", label: "Americas", hint: "North and South America" },
  { id: "emea", label: "EMEA", hint: "Europe, Middle East, Africa" },
  { id: "apac", label: "APAC", hint: "Asia-Pacific coverage" },
];

function TopbarMenu({ type, options, active, onSelect }) {
  return (
    <div className="topbar-menu" role="menu" aria-label={type + " selector"}>
      {options.map((opt) => (
        <button key={opt.id} className={"topbar-menu-item" + (active === opt.id ? " active" : "")}
          role="menuitem" onClick={() => onSelect(opt)}>
          <span>
            <strong>{opt.label}</strong>
            <small>{opt.hint}</small>
          </span>
          {active === opt.id && <Icon.check style={{ width: 14, height: 14 }} />}
        </button>
      ))}
    </div>
  );
}

function Topbar({ onToggleSidebar, theme, onToggleTheme }) {
  const st = useStore();
  const activeDate = DATE_SCOPES.find((x) => x.id === st.dateScope) || DATE_SCOPES[0];
  const activeRegion = REGION_SCOPES.find((x) => x.id === st.regionScope) || REGION_SCOPES[0];
  const openSearch = () => Store.set({ paletteOpen: true, topbarMenu: null });
  const handleSearchKey = (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSearch(); }
  };
  const toggleMenu = (menu) => Store.set((s) => ({ topbarMenu: s.topbarMenu === menu ? null : menu, paletteOpen: false }));
  const selectDate = (opt) => {
    Store.set({ dateScope: opt.id, topbarMenu: null });
    Store.flash("Date scope set to " + opt.label);
  };
  const selectRegion = (opt) => {
    Store.set({ regionScope: opt.id, topbarMenu: null });
    Store.flash("Region scope set to " + opt.label);
  };
  const handleTheme = () => {
    onToggleTheme();
    Store.set({ topbarMenu: null });
    Store.flash((theme === "dark" ? "Light" : "Dark") + " mode enabled");
  };
  useEffect(() => {
    if (!st.topbarMenu) return undefined;
    const close = (e) => {
      if (!e.target.closest(".topbar-menu-wrap")) Store.set({ topbarMenu: null });
    };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [st.topbarMenu]);
  return (
    <header className="topbar">
      <button className="btn btn-icon btn-ghost" onClick={onToggleSidebar} aria-label="Toggle sidebar">
        <Icon.list />
      </button>
      <div className="search" role="button" tabIndex={0} aria-label="Open intelligence search" onClick={openSearch} onKeyDown={handleSearchKey}>
        <Icon.search style={{ width: 15, height: 15 }} />
        <span style={{ flex: 1, fontSize: 13 }}>Search events, companies, industries…</span>
        <kbd>⌘K</kbd>
      </div>
      <div style={{ flex: 1 }} />
      <div className="topbar-menu-wrap">
        <button className={"btn btn-sm btn-ghost topbar-scope" + (st.topbarMenu === "date" ? " active" : "")}
          style={{ gap: 6 }} onClick={() => toggleMenu("date")} aria-haspopup="menu" aria-expanded={st.topbarMenu === "date"}>
          <Icon.calendar style={{ width: 15, height: 15 }} /> {activeDate.label} <Icon.chevD style={{ width: 13, height: 13 }} />
        </button>
        {st.topbarMenu === "date" && <TopbarMenu type="Date scope" options={DATE_SCOPES} active={st.dateScope} onSelect={selectDate} />}
      </div>
      <div className="topbar-menu-wrap">
        <button className={"btn btn-sm btn-ghost topbar-scope" + (st.topbarMenu === "region" ? " active" : "")}
          style={{ gap: 6 }} onClick={() => toggleMenu("region")} aria-haspopup="menu" aria-expanded={st.topbarMenu === "region"}>
          <Icon.globe style={{ width: 15, height: 15 }} /> {activeRegion.label} <Icon.chevD style={{ width: 13, height: 13 }} />
        </button>
        {st.topbarMenu === "region" && <TopbarMenu type="Region scope" options={REGION_SCOPES} active={st.regionScope} onSelect={selectRegion} />}
      </div>
      <button className="btn btn-icon btn-ghost" onClick={handleTheme} aria-label="Toggle theme" title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}>
        {theme === "dark" ? <Icon.sun /> : <Icon.moon />}
      </button>
      <button className="btn btn-icon btn-ghost" style={{ position: "relative" }} onClick={() => Store.nav("alerts")} aria-label="Alerts">
        <Icon.bell />
        <span style={{ position: "absolute", top: 6, right: 6, width: 7, height: 7, borderRadius: "50%", background: "var(--r-high)", border: "1.5px solid var(--surface)" }} />
      </button>
      <div style={{ width: 30, height: 30, borderRadius: 8, background: "var(--surface-3)", border: "1px solid var(--line)", display: "grid", placeItems: "center", fontFamily: "var(--font-mono)", fontSize: 12, fontWeight: 600, color: "var(--ink-2)" }}>AK</div>
    </header>
  );
}

/* ---- Command palette ------------------------------------------------------ */
function CommandPalette() {
  const [q, setQ] = useState("");
  const inputRef = useRef(null);
  useEffect(() => { inputRef.current && inputRef.current.focus(); }, []);
  const D = window.DATA;
  const ql = q.toLowerCase();
  const results = useMemo(() => {
    const r = [];
    NAV.forEach((g) => g.items.forEach((it) => { if (!q || it.label.toLowerCase().includes(ql)) r.push({ kind: "Page", label: it.label, icon: it.icon, go: () => Store.nav(it.name) }); }));
    D.events.forEach((e) => { if (e.title.toLowerCase().includes(ql)) r.push({ kind: "Event", label: e.title, icon: Icon.events, go: () => Store.nav("event", e.id) }); });
    D.companies.forEach((c) => { if (c.name.toLowerCase().includes(ql) || (c.ticker || "").toLowerCase().includes(ql)) r.push({ kind: "Company", label: c.name + " · " + c.ticker, icon: Icon.companies, go: () => Store.nav("company", c.companyId) }); });
    D.industries.forEach((i) => { if (i.industryName.toLowerCase().includes(ql)) r.push({ kind: "Industry", label: i.industryName, icon: Icon.industries, go: () => Store.nav("industry", i.industryId) }); });
    return r.slice(0, 9);
  }, [q]);
  return (
    <div className="drawer-scrim" style={{ alignItems: "flex-start", display: "flex", justifyContent: "center" }} onClick={() => Store.set({ paletteOpen: false })}>
      <div onClick={(e) => e.stopPropagation()} style={{ marginTop: "12vh", width: "min(580px, 92vw)", background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 14, boxShadow: "var(--shadow-lg)", overflow: "hidden" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: "1px solid var(--line)" }}>
          <Icon.search style={{ width: 17, height: 17, color: "var(--ink-3)" }} />
          <input ref={inputRef} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search the intelligence base…"
            style={{ flex: 1, background: "none", border: "none", outline: "none", color: "var(--ink)", fontFamily: "var(--font-sans)", fontSize: 15 }}
            onKeyDown={(e) => { if (e.key === "Enter" && results[0]) results[0].go(); if (e.key === "Escape") Store.set({ paletteOpen: false }); }} />
          <kbd style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--ink-3)", border: "1px solid var(--line)", borderRadius: 5, padding: "2px 6px" }}>ESC</kbd>
        </div>
        <div style={{ maxHeight: 380, overflowY: "auto", padding: 8 }}>
          {results.length === 0 && <div style={{ padding: 24, textAlign: "center", color: "var(--ink-3)", fontSize: 13 }}>No matches.</div>}
          {results.map((r, i) => { const Ic = r.icon; return (
            <div key={i} onClick={r.go} className="nav-item" style={{ borderRadius: 9 }}>
              <Ic className="ico" />
              <span className="label" style={{ flex: 1 }}>{r.label}</span>
              <span className="chip" style={{ fontSize: 10 }}>{r.kind}</span>
            </div>
          ); })}
        </div>
      </div>
    </div>
  );
}

/* ---- Toast ---------------------------------------------------------------- */
function Toast({ msg }) {
  return (
    <div style={{ position: "fixed", bottom: 24, left: "50%", transform: "translateX(-50%)", zIndex: 200,
      background: "var(--raised)", border: "1px solid var(--line)", borderRadius: 10, padding: "10px 16px",
      boxShadow: "var(--shadow-lg)", display: "flex", alignItems: "center", gap: 9, fontSize: 13, animation: "pageIn .2s ease" }}>
      <Icon.check style={{ width: 16, height: 16, color: "var(--r-low)" }} /> {msg}
    </div>
  );
}

Object.assign(window, { Sidebar, Topbar, CommandPalette, Toast, NAV });
