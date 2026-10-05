/* SIGNAL — main app: router, theme, tweaks, drawer, mount. */
const { useState, useEffect, useRef, useMemo } = React;

/* ---- Evidence drawer ------------------------------------------------------ */
function EvidenceDrawer({ ev }) {
  const typeLabel = { rss: "News Article", news_api: "News Article", official: "Official Statement", filing: "Company Filing", macro_indicator: "Macro Indicator", market_data: "Market Data", banking_data: "Banking Data", historical_case: "Historical Case", article: "News Article", official_statement: "Official Statement" }[ev.sourceType] || "Source";
  const sourceUrl = SignalApiAdapter.safeHttpUrl(ev.url);
  const isDemo = window.DATA && window.DATA.runtime && window.DATA.runtime.mode === "demo";
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
          {isDemo && <div className="badge badge-neutral" style={{ marginBottom: 12 }}>Demo — sample data</div>}
          <h2 style={{ fontSize: 18, lineHeight: 1.3, marginBottom: 12, textWrap: "balance" }}>{ev.title}</h2>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 18 }}>
            {ev.publisher && <span className="chip"><Icon.companies style={{ width: 13, height: 13 }} /> {ev.publisher}</span>}
            {ev.publishedAt && <span className="chip"><Icon.clock style={{ width: 13, height: 13 }} /> {fmtTime(ev.publishedAt)}</span>}
            {ev.credibilityScore != null && <span className="badge badge-low">Credibility {ev.credibilityScore.toFixed(2)}</span>}
          </div>
          <div style={{ padding: 16, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)", marginBottom: 18 }}>
            <div className="eyebrow" style={{ marginBottom: 8 }}>Source Excerpt</div>
            {ev.excerpt && ev.excerpt.trim()
              ? <p style={{ fontSize: 13.5, color: "var(--ink-2)", margin: 0, lineHeight: 1.6 }}>{ev.excerpt}</p>
              : <p style={{ fontSize: 13.5, color: "var(--ink-3)", margin: 0, lineHeight: 1.6 }}>No excerpt available.</p>}
          </div>
          <div className="stack" style={{ gap: 10, marginBottom: 18 }}>
            <Row k="Source type" v={typeLabel} />
            <Row k="Publisher" v={ev.publisher} />
            <Row k="Published" v={ev.publishedAt ? fmtTime(ev.publishedAt) : "—"} />
            <Row k="Credibility" v={ev.credibilityScore != null ? ev.credibilityScore.toFixed(2) + " / 1.00" : "—"} />
            {Array.isArray(ev.relatedClaims) && <Row k="Linked claims" v={ev.relatedClaims.length} />}
          </div>
          {sourceUrl
            ? <a href={sourceUrl} target="_blank" rel="noopener noreferrer" className="btn btn-primary" style={{ width: "100%" }}><Icon.link style={{ width: 15, height: 15 }} /> Open original source</a>
            : <button className="btn" style={{ width: "100%" }} type="button" disabled><Icon.link style={{ width: 15, height: 15 }} /> Source link unavailable</button>}
          <p style={{ fontSize: 11.5, color: "var(--ink-faint)", textAlign: "center", marginTop: 10 }}>A source link does not guarantee that the publisher permits access.</p>
        </div>
      </div>
    </>
  );
}

/* ---- Router --------------------------------------------------------------- */
function Outlet({ route }) {
  switch (route.name) {
    case "today":
    case "dashboard":
    case "events": return <TodayPage />;
    case "saved":
    case "watchlist": return <SavedPage />;
    case "briefs":
    case "reports": return <BriefsPage />;
    case "event": return <PersonalEventDetail id={route.param} runId={route.runId} />;
    case "brief": return <PersonalBriefDetail id={route.param} />;
    case "settings": return <PersonalSettingsPage />;
    case "article": return <PersonalArticleDetail id={route.param} />;
    default: return <UnavailablePersonalPage route={route} />;
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

  useEffect(() => {
    const changed = () => {
      Store.closeDrawer();
      Store.set((s) => ({ dataVersion: s.dataVersion + 1, paletteOpen: false, topbarMenu: null }));
    };
    window.addEventListener("signal:data-changed", changed);
    return () => window.removeEventListener("signal:data-changed", changed);
  }, []);

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
        {window.DATA && window.DATA.runtime && window.DATA.runtime.mode === "demo" && <GlobalDataNotices />}
        <Outlet
          key={[
            window.DATA && window.DATA.runtime && window.DATA.runtime.mode,
            st.route.name,
            st.route.param || "",
            st.route.runId || "",
          ].join(":")}
          route={st.route}
        />
      </main>

      {st.paletteOpen && <CommandPalette />}
      {st.drawer && st.drawer.type === "evidence" && <EvidenceDrawer ev={st.drawer.data} />}
      {st.drawer && st.drawer.type === "reportEvidence" && <ReportEvidenceDrawer model={st.drawer.data} />}
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

/* Mount once the default real-mode read finishes, including an honest error
   snapshot when the API is unavailable. */
function mountSignal() {
  ReactDOM.createRoot(document.getElementById("root")).render(<App />);
}
if (window.DATA) mountSignal();
else window.addEventListener("signal:data-ready", mountSignal, { once: true });
