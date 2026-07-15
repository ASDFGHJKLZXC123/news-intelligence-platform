/* SIGNAL — main app: router, theme, tweaks, drawer, mount. */
const { useState, useEffect, useRef, useMemo } = React;

/* ---- Evidence drawer ------------------------------------------------------ */
function EvidenceDrawer({ ev }) {
  const typeLabel = { rss: "News Article", news_api: "News Article", official: "Official Statement", filing: "Company Filing", macro_indicator: "Macro Indicator", market_data: "Market Data", banking_data: "Banking Data", historical_case: "Historical Case", article: "News Article", official_statement: "Official Statement" }[ev.sourceType] || "Source";
  return (
    <>
      <div className="drawer-scrim" onClick={() => Store.closeDrawer()} />
      <div className="drawer">
        <div className="drawer-head">
          <Icon.doc style={{ width: 18, height: 18, color: "var(--accent)" }} />
          <div style={{ flex: 1 }}><div className="eyebrow">Evidence Source</div><div style={{ fontSize: 14, fontWeight: 600 }}>{typeLabel}</div></div>
          <button className="btn btn-icon btn-ghost" onClick={() => Store.closeDrawer()}><Icon.x /></button>
        </div>
        <div className="drawer-body">
          <h2 style={{ fontSize: 18, lineHeight: 1.3, marginBottom: 12, textWrap: "balance" }}>{ev.title}</h2>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 18 }}>
            <span className="chip"><Icon.companies style={{ width: 13, height: 13 }} /> {ev.publisher}</span>
            {ev.publishedAt && <span className="chip"><Icon.clock style={{ width: 13, height: 13 }} /> {fmtTime(ev.publishedAt)}</span>}
            {ev.credibilityScore != null && <span className="badge badge-low">Credibility {ev.credibilityScore.toFixed(2)}</span>}
          </div>
          <div style={{ padding: 16, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)", marginBottom: 18 }}>
            <div className="eyebrow" style={{ marginBottom: 8 }}>Source Excerpt</div>
            <p style={{ fontSize: 13.5, color: "var(--ink-2)", margin: 0, lineHeight: 1.6, fontStyle: "italic" }}>
              "{ev.title}." Reporting indicates developing conditions consistent with the event cluster's analysis. This excerpt is representative; the full source is linked below for verification.
            </p>
          </div>
          <div className="stack" style={{ gap: 10, marginBottom: 18 }}>
            <Row k="Source type" v={typeLabel} />
            <Row k="Publisher" v={ev.publisher} />
            <Row k="Published" v={ev.publishedAt ? fmtTime(ev.publishedAt) : "—"} />
            <Row k="Credibility" v={ev.credibilityScore != null ? ev.credibilityScore.toFixed(2) + " / 1.00" : "—"} />
            <Row k="Linked claims" v={(ev.relatedClaims || []).length} />
          </div>
          <a href={ev.url || "#"} onClick={(e) => e.preventDefault()} className="btn btn-primary" style={{ width: "100%" }}><Icon.link style={{ width: 15, height: 15 }} /> Open original source</a>
          <p style={{ fontSize: 11.5, color: "var(--ink-faint)", textAlign: "center", marginTop: 10 }}>Demo source — link disabled in prototype</p>
        </div>
      </div>
    </>
  );
}

/* ---- Router --------------------------------------------------------------- */
function Outlet({ route }) {
  switch (route.name) {
    case "dashboard": return <Dashboard />;
    case "events": return <EventsPage />;
    case "event": return <EventDetail id={route.param} />;
    case "geo": return <GeoIntelPage />;
    case "risk": return <RiskRadarPage />;
    case "riskdetail": return <RiskDetailPage type={route.param} />;
    case "industries": return <IndustriesPage />;
    case "industry": return <IndustryDetail id={route.param} />;
    case "companies": return <CompaniesPage />;
    case "company": return <CompanyDetail id={route.param} />;
    case "historical": return <HistoricalPage />;
    case "alerts": return <AlertsPage />;
    case "watchlist": return <WatchlistPage />;
    case "reports": return <ReportsPage />;
    case "ask": return <AskPage />;
    case "admin": return <AdminPage />;
    case "settings": return <SettingsPage />;
    default: return <Dashboard />;
  }
}

/* ---- Tweaks --------------------------------------------------------------- */
const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "dark",
  "accent": "#38bdf8",
  "density": "regular",
  "font": "IBM Plex",
  "monoTickers": true
}/*EDITMODE-END*/;

const FONT_STACKS = {
  "IBM Plex": '"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif',
  "Söhne / Grotesk": '"Space Grotesk", "IBM Plex Sans", system-ui, sans-serif',
  "Geist-like": '"Manrope", "IBM Plex Sans", system-ui, sans-serif',
  "Humanist": '"Public Sans", "IBM Plex Sans", system-ui, sans-serif',
};
const DENSITY_U = { compact: 0.82, regular: 1, comfy: 1.16 };

/* ---- App ------------------------------------------------------------------ */
function App() {
  const st = useStore();
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [collapsed, setCollapsed] = useState(false);

  // apply tweaks to :root
  useEffect(() => {
    const r = document.documentElement;
    r.setAttribute("data-theme", t.theme);
    r.style.setProperty("--accent", t.accent);
    r.style.setProperty("--u", String(DENSITY_U[t.density] || 1));
    r.style.setProperty("--font-sans", FONT_STACKS[t.font] || FONT_STACKS["IBM Plex"]);
  }, [t.theme, t.accent, t.density, t.font]);

  // cmd+k
  useEffect(() => {
    const h = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); Store.set((s) => ({ paletteOpen: !s.paletteOpen })); }
      if (e.key === "Escape") { Store.set({ paletteOpen: false }); Store.closeDrawer(); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const toggleTheme = () => setTweak("theme", t.theme === "dark" ? "light" : "dark");

  return (
    <div className={"app" + (collapsed ? " sb-collapsed" : "")}>
      <Sidebar collapsed={collapsed} route={st.route} />
      <Topbar onToggleSidebar={() => setCollapsed((c) => !c)} theme={t.theme} onToggleTheme={toggleTheme} />
      <main className="main">
        <GlobalDataNotices />
        <Outlet route={st.route} />
      </main>

      {st.paletteOpen && <CommandPalette />}
      {st.drawer && st.drawer.type === "evidence" && <EvidenceDrawer ev={st.drawer.data} />}
      {st.toast && <Toast msg={st.toast} />}

      <TweaksPanel title="Tweaks">
        <TweakSection label="Theme" />
        <TweakRadio label="Mode" value={t.theme} options={["dark", "light"]} onChange={(v) => setTweak("theme", v)} />
        <TweakColor label="Accent" value={t.accent} options={["#38bdf8", "#f0922b", "#34d399", "#a78bfa", "#f43f6e"]} onChange={(v) => setTweak("accent", v)} />
        <TweakSection label="Layout" />
        <TweakRadio label="Density" value={t.density} options={["compact", "regular", "comfy"]} onChange={(v) => setTweak("density", v)} />
        <TweakSection label="Typography" />
        <TweakSelect label="Font" value={t.font} options={Object.keys(FONT_STACKS)} onChange={(v) => setTweak("font", v)} />
      </TweaksPanel>
    </div>
  );
}

/* Mount only once the formatted data fixtures (data/*.json) have loaded and
   window.DATA is assembled — see app/data.js. */
function mountSignal() {
  ReactDOM.createRoot(document.getElementById("root")).render(<App />);
}
if (window.DATA) mountSignal();
else window.addEventListener("signal:data-ready", mountSignal, { once: true });
