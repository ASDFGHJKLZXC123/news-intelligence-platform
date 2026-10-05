/* SIGNAL — global store + app shell (sidebar, topbar, command palette, drawer). */
const { useState, useEffect, useRef, useMemo } = React;

function routeFromHash(hash) {
  const rawWithQuery = String(hash || "").replace(/^#!?\/?/, "");
  const queryIndex = rawWithQuery.indexOf("?");
  const raw = queryIndex >= 0 ? rawWithQuery.slice(0, queryIndex) : rawWithQuery;
  const query = queryIndex >= 0 ? rawWithQuery.slice(queryIndex + 1) : "";
  if (!raw) return { name: "today", param: null };
  const parts = raw.split("/").filter(Boolean).map((part) => {
    try { return decodeURIComponent(part); } catch (e) { return part; }
  });
  const aliases = { root: "today", dashboard: "today", events: "today", watchlist: "saved", reports: "briefs" };
  const name = aliases[parts[0]] || parts[0];
  const route = { name, param: parts.length > 1 ? parts.slice(1).join("/") : null };
  const runMatch = query.match(/(?:^|&)run=([^&]+)/);
  if (runMatch) {
    try { route.runId = decodeURIComponent(runMatch[1]); } catch (e) { route.runId = runMatch[1]; }
  }
  return route;
}

function hashForRoute(route) {
  const base = route && route.name ? route.name : "today";
  const run = route && route.runId ? "?run=" + encodeURIComponent(route.runId) : "";
  return "#/" + encodeURIComponent(base) + (route && route.param != null ? "/" + encodeURIComponent(route.param) : "") + run;
}

/* ---- tiny pub/sub store --------------------------------------------------- */
const Store = (function () {
  let state = {
    route: routeFromHash(window.location && window.location.hash),
    drawer: null,                 // { type, data }
    paletteOpen: false,
    topbarMenu: null,
    toast: null,
    dataVersion: 0,
  };
  const subs = new Set();
  const notify = () => subs.forEach((f) => f());
  return {
    get: () => state,
    set: (patch) => { state = Object.assign({}, state, typeof patch === "function" ? patch(state) : patch); notify(); },
    subscribe: (f) => { subs.add(f); return () => subs.delete(f); },
    nav: (name, param = null, options = null) => {
      const aliases = { root: "today", dashboard: "today", events: "today", watchlist: "saved", reports: "briefs" };
      const destination = aliases[name] || name;
      const route = { name: destination, param };
      if (options && options.runId) route.runId = options.runId;
      state = Object.assign({}, state, { route, paletteOpen: false, topbarMenu: null });
      notify();
      const nextHash = hashForRoute(state.route);
      if (window.location && window.location.hash !== nextHash) window.location.hash = nextHash;
      const m = document.querySelector(".main"); if (m) m.scrollTop = 0;
    },
    syncRoute: () => {
      const route = routeFromHash(window.location && window.location.hash);
      if (route.name === state.route.name && route.param === state.route.param && route.runId === state.route.runId) return;
      state = Object.assign({}, state, { route, paletteOpen: false, topbarMenu: null, drawer: null });
      notify();
    },
    openDrawer: (d) => { state = Object.assign({}, state, { drawer: d, topbarMenu: null }); notify(); },
    closeDrawer: () => { state = Object.assign({}, state, { drawer: null, topbarMenu: null }); notify(); },
    flash: (msg) => { state = Object.assign({}, state, { toast: msg }); notify(); clearTimeout(Store._tt); Store._tt = setTimeout(() => { state = Object.assign({}, state, { toast: null }); notify(); }, 1900); },
  };
})();
window.Store = Store;
window.addEventListener("hashchange", () => Store.syncRoute());

function useStore() {
  const [, force] = useState(0);
  useEffect(() => Store.subscribe(() => force((n) => n + 1)), []);
  return Store.get();
}
window.useStore = useStore;

/* ---- nav config ----------------------------------------------------------- */
const NAV = [
  { group: "Personal desk", items: [
    { name: "today", label: "Today", icon: Icon.events, count: () => (window.PersonalUiCache && window.PersonalUiCache.events || []).length },
    { name: "saved", label: "Saved", icon: Icon.watchlist },
    { name: "briefs", label: "Briefs", icon: Icon.reports },
    { name: "settings", label: "Settings", icon: Icon.settings },
  ]},
];

/* route → nav name for active state */
function activeNav(route) {
  const map = { event: "today", brief: "briefs", dashboard: "today", events: "today", watchlist: "saved", reports: "briefs" };
  return map[route.name] || route.name;
}

/* ---- Sidebar -------------------------------------------------------------- */
function Sidebar({ collapsed, route }) {
  const active = activeNav(route);
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
        <div className="eyebrow" style={{ marginBottom: 4 }}>Personal mode</div>
        <div style={{ fontSize: 11, color: "var(--ink-3)", lineHeight: 1.45 }}>Local reading workspace</div>
      </div>
    </aside>
  );
}

/* ---- Topbar --------------------------------------------------------------- */
function Topbar({ onToggleSidebar, theme, onToggleTheme }) {
  useStore();
  const mode = window.DATA && window.DATA.runtime && window.DATA.runtime.mode === "demo" ? "demo" : "real";
  const openSearch = () => Store.set({ paletteOpen: true, topbarMenu: null });
  const handleSearchKey = (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSearch(); }
  };
  const selectMode = (next) => window.SignalDataController && window.SignalDataController.setMode(next);
  const handleTheme = () => {
    onToggleTheme();
    Store.set({ topbarMenu: null });
    Store.flash((theme === "dark" ? "Light" : "Dark") + " mode enabled");
  };
  return (
    <header className="topbar">
      <button className="btn btn-icon btn-ghost" onClick={onToggleSidebar} aria-label="Toggle sidebar">
        <Icon.list />
      </button>
      <div className="search" role="button" tabIndex={0} aria-label="Search stories" onClick={openSearch} onKeyDown={handleSearchKey}>
        <Icon.search style={{ width: 15, height: 15 }} />
        <span style={{ flex: 1, fontSize: 13 }}>Search stories…</span>
        <kbd>⌘K</kbd>
      </div>
      <div style={{ flex: 1 }} />
      <div className="mode-switch" role="group" aria-label="Display data mode">
        <button className={"btn btn-sm " + (mode === "real" ? "" : "btn-ghost")} type="button" aria-pressed={mode === "real"} onClick={() => selectMode("real")}>Real</button>
        <button className={"btn btn-sm " + (mode === "demo" ? "" : "btn-ghost")} type="button" aria-pressed={mode === "demo"} onClick={() => selectMode("demo")}>Demo</button>
      </div>
      <button className="btn btn-icon btn-ghost" onClick={handleTheme} aria-label="Toggle theme" title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}>
        {theme === "dark" ? <Icon.sun /> : <Icon.moon />}
      </button>
    </header>
  );
}

/* ---- Command palette ------------------------------------------------------ */
function CommandPalette() {
  const [q, setQ] = useState("");
  const inputRef = useRef(null);
  useEffect(() => { inputRef.current && inputRef.current.focus(); }, []);
  const stories = (window.PersonalUiCache && window.PersonalUiCache.events) || [];
  const ql = q.toLowerCase();
  const results = useMemo(() => {
    const r = [];
    NAV.forEach((g) => g.items.forEach((it) => { if (!q || it.label.toLowerCase().includes(ql)) r.push({ kind: "Page", label: it.label, icon: it.icon, go: () => Store.nav(it.name) }); }));
    stories.forEach((e) => { if ((e.title || "").toLowerCase().includes(ql)) r.push({ kind: "Story", label: e.title, icon: Icon.events, go: () => Store.nav("event", e.id) }); });
    return r.slice(0, 9);
  }, [q]);
  return (
    <div className="drawer-scrim" style={{ alignItems: "flex-start", display: "flex", justifyContent: "center" }} onClick={() => Store.set({ paletteOpen: false })}>
      <div onClick={(e) => e.stopPropagation()} style={{ marginTop: "12vh", width: "min(580px, 92vw)", background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 14, boxShadow: "var(--shadow-lg)", overflow: "hidden" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: "1px solid var(--line)" }}>
          <Icon.search style={{ width: 17, height: 17, color: "var(--ink-3)" }} />
          <input ref={inputRef} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search Today, Saved, Briefs, and stories…"
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

Object.assign(window, { Sidebar, Topbar, CommandPalette, Toast, NAV, routeFromHash, hashForRoute });
