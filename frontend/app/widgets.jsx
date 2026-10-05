/* SIGNAL — composite widgets shared across pages. */
const { useState, useEffect, useRef, useMemo } = React;

/* Company brand mark used wherever company names appear. */
const COMPANY_LOGOS = {
  nvda: { label: "NVIDIA", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/2/21/Nvidia_logo.svg", simpleIcon: "nvidia", fallback: "NV", aspect: 1.45 },
  asml: { label: "ASML", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/6/6c/ASML_Holding_N.V._logo.svg", fallback: "AS", aspect: 1.6 },
  tsm: { label: "TSMC", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/d/df/TSMC_wordmark.svg", fallback: "TS", aspect: 1.7 },
  amd: { label: "AMD", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/7/7c/AMD_Logo.svg", simpleIcon: "amd", fallback: "AM", aspect: 1.5 },
  amat: { label: "Applied Materials", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/7/7e/Applied_Materials_Inc._Logo.svg", fallback: "AM", aspect: 1.85 },
  msft: { label: "Microsoft", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/9/96/Microsoft_logo_%282012%29.svg", fallback: "MS", aspect: 2.45 },
  avgo: { label: "Broadcom", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/5/58/Broadcom_logo_%282016-present%29.svg", simpleIcon: "broadcom", fallback: "BC", aspect: 2.7 },
  wal: { label: "Western Alliance", logoUrl: "https://upload.wikimedia.org/wikipedia/en/a/a2/Western_Alliance_Bancorporation_logo.png", fallback: "WA", aspect: 2.8 },
  cma: { label: "Comerica", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/f/f5/Comerica_logo_%282022%29.svg", fallback: "CM", aspect: 1.9 },
  xom: { label: "ExxonMobil", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/0/09/ExxonMobil_Logo.svg", fallback: "XM", aspect: 2 },
  lmt: { label: "Lockheed Martin", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/1/1d/Lockheed_Martin_logo_%282%29.svg", fallback: "LM", aspect: 2 },
  lrcx: { label: "Lam Research", logoUrl: "https://upload.wikimedia.org/wikipedia/commons/a/ac/Lam_Research_logo.svg", fallback: "LR", aspect: 1.75 },
};

function companyLogoKey(company, ticker, name) {
  const raw = (company && (company.companyId || company.ticker || company.name)) || ticker || name || "";
  const normalized = String(raw).toLowerCase().replace(/[^a-z0-9]/g, "");
  if (COMPANY_LOGOS[normalized]) return normalized;
  const byTicker = Object.keys(COMPANY_LOGOS).find((k) => {
    const candidate = window.DATA && window.DATA.companies && window.DATA.companies.find((c) => c.companyId === k);
    return candidate && candidate.ticker && candidate.ticker.toLowerCase() === normalized;
  });
  return byTicker || normalized;
}

function companyFromArgs(company, ticker, name) {
  if (company && (company.name || company.ticker || company.companyId)) return company;
  const lookup = String(ticker || name || "").toLowerCase();
  return window.DATA && window.DATA.companies && window.DATA.companies.find((c) =>
    c.companyId === lookup || (c.ticker || "").toLowerCase() === lookup || (c.name || "").toLowerCase() === lookup
  );
}

function logoCandidates(meta) {
  const urls = [];
  if (meta.logoUrl) urls.push(meta.logoUrl);
  if (meta.simpleIcon) urls.push(`https://cdn.simpleicons.org/${meta.simpleIcon}`);
  if (meta.faviconDomain) {
    urls.push(`https://www.google.com/s2/favicons?domain=${meta.faviconDomain}&sz=256`);
    urls.push(`https://www.google.com/s2/favicons?domain=${meta.faviconDomain}&sz=128`);
  }
  return [...new Set(urls)];
}

function companyLogoBoxSize(size, meta) {
  const aspect = meta.aspect || 1;
  const maxAspect = size <= 20 ? 1.5 : size <= 34 ? 2.15 : 2.25;
  return { width: Math.round(size * Math.min(aspect, maxAspect)), height: size };
}

function CompanyLogo({ company, ticker, name, size = 30 }) {
  const c = companyFromArgs(company, ticker, name);
  const key = companyLogoKey(c, ticker, name);
  const fallback = (c && (c.ticker || c.name)) || ticker || name || "—";
  const meta = COMPANY_LOGOS[key] || { label: c?.name || name || ticker || "Company", fallback: fallback.slice(0, 2).toUpperCase() };
  const candidates = useMemo(() => logoCandidates(meta), [key]);
  const [logoIndex, setLogoIndex] = useState(0);
  useEffect(() => setLogoIndex(0), [key]);
  const logoUrl = candidates[logoIndex] || null;
  const box = companyLogoBoxSize(size, meta);
  const style = {
    width: box.width,
    height: box.height,
    fontSize: Math.max(9, Math.round(size * 0.34)),
    "--logo-bg": meta.background || "#fff",
    "--logo-fallback-bg": meta.fallbackBg || "var(--surface-3)",
    "--logo-fallback-fg": meta.fallbackFg || "var(--ink-2)",
  };
  const title = (c && c.name) || meta.label || fallback;
  const onLogoError = () => setLogoIndex((i) => Math.min(i + 1, candidates.length));
  return (
    <span className={"company-logo" + (logoUrl ? "" : " no-logo")} style={style} title={title + " logo"} aria-label={title + " logo"}>
      {logoUrl && <img src={logoUrl} alt="" loading="eager" decoding="async" referrerPolicy="no-referrer"
        onError={onLogoError} />}
      <span className="company-logo-fallback">{meta.fallback || fallback.slice(0, 2).toUpperCase()}</span>
    </span>
  );
}

/* Phase 1 keeps saving visible but cannot pretend that it persisted. */
function WatchBtn({ sm }) {
  return (
    <button className={"btn watch-btn " + (sm ? "btn-sm " : "")} type="button" disabled
      title="Saving is unavailable in this version" aria-label="Save story — unavailable in this version">
      <Icon.watchlist style={{ width: sm ? 13 : 15, height: sm ? 13 : 15 }} />
      Save
    </button>
  );
}

/* Metric KPI card */
function MetricCard({ m, spark }) {
  const sev = m.severity || "low";
  const col = levelColor(sev);
  return (
    <div className="card card-pad metric-card">
      <div className="metric-head">
        <span className="eyebrow">{m.label}</span>
        {m.changeDirection && <Delta value={m.change} />}
      </div>
      <div className="metric-value-row">
        <div className="mono metric-value" style={{ color: typeof m.value === "string" ? col : "var(--ink)" }}>
          {m.value}
        </div>
        {spark && <Sparkline data={spark} w={72} h={26} color={col} />}
      </div>
      {m.previousValue != null && (
        <div style={{ fontSize: 11, color: "var(--ink-faint)" }}>prev <span className="mono">{m.previousValue}</span></div>
      )}
    </div>
  );
}

/* Hotness flame meter */
function HotMeter({ score }) {
  return (
    <span className="mono" style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, color: score >= 80 ? "var(--r-high)" : "var(--ink-2)", fontWeight: 600 }}>
      <Icon.flame style={{ width: 13, height: 13, color: score >= 80 ? "var(--r-high)" : "var(--ink-3)" }} />{score}
    </span>
  );
}

/* Hot event card (dashboard + event list card view) */
function HotEventCard({ e, rank }) {
  return (
    <div className="card" style={{ overflow: "hidden", cursor: "pointer", transition: "border-color .15s" }}
      onClick={() => Store.nav("event", e.id)}
      onMouseEnter={(ev) => (ev.currentTarget.style.borderColor = "var(--ink-faint)")}
      onMouseLeave={(ev) => (ev.currentTarget.style.borderColor = "")}>
      <div style={{ display: "flex" }}>
        <div style={{ width: 4, background: levelColor(e.riskLevel) }} />
        <div className="card-pad" style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 9, flexWrap: "wrap" }}>
            {rank && <span className="mono" style={{ fontSize: 11, color: "var(--ink-faint)", fontWeight: 600 }}>#{rank}</span>}
            <RiskBadge level={e.riskLevel} label={e.riskLevel[0].toUpperCase() + e.riskLevel.slice(1) + " Risk"} />
            <span className="eyebrow" style={{ color: "var(--ink-3)" }}>{e.eventType}</span>
            <div style={{ flex: 1 }} />
            <HotMeter score={e.hotnessScore} />
          </div>
          <h3 style={{ fontSize: 16.5, lineHeight: 1.25, marginBottom: 7, textWrap: "balance" }}>{e.title}</h3>
          <p style={{ fontSize: 13, color: "var(--ink-2)", margin: "0 0 13px", lineHeight: 1.5, textWrap: "pretty" }}>{e.summary}</p>

          <div style={{ display: "flex", gap: 18, marginBottom: 13, flexWrap: "wrap" }}>
            <ScoreStat label="Risk" value={e.riskScore} tip compact />
            <ScoreStat label="Hotness" value={e.hotnessScore} tip compact />
            <div>
              <div className="eyebrow" style={{ margin: 0 }}>Confidence</div>
              <div className="mono" style={{ fontSize: 16, fontWeight: 600, marginTop: 2 }}>{e.confidenceScore.toFixed(2)}<span style={{ fontSize: 11, color: "var(--ink-faint)" }}> · {window.DATA.conf(e.confidenceScore)}</span></div>
            </div>
          </div>

          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 11 }}>
            {e.affectedIndustries.slice(0, 4).map((i) => <span key={i} className="chip">{i}</span>)}
          </div>

          <div className="divider" style={{ margin: "0 0 11px" }} />

          <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
            <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
              {e.affectedCompanies.slice(0, 4).map((c) => {
                const m = dirMeta(c.impactDirection);
                return <span key={c.name} className="mono" style={{ fontSize: 11.5, color: m.color, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 5 }}>
                  <CompanyLogo company={c} size={18} />{c.ticker || c.name}</span>;
              })}
            </div>
            <div style={{ flex: 1 }} />
            <span style={{ fontSize: 11.5, color: "var(--ink-faint)", display: "inline-flex", alignItems: "center", gap: 5 }}>
              <Icon.doc style={{ width: 13, height: 13 }} />{e.sourceCount} sources · {timeAgo(e.firstSeenAt)}
            </span>
          </div>

          {e.whyItMatters && (
            <div style={{ marginTop: 13, padding: "10px 12px", background: "var(--accent-soft)", borderRadius: 9, border: "1px solid var(--accent-line)" }}>
              <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                <Icon.bolt style={{ width: 14, height: 14, color: "var(--accent)", flexShrink: 0, marginTop: 1 }} />
                <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.45 }}>
                  <span style={{ color: "var(--accent)", fontWeight: 600, fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: 0 }}>WHY IT MATTERS · </span>
                  {e.whyItMatters}
                </div>
              </div>
            </div>
          )}

          <div style={{ display: "flex", gap: 8, marginTop: 13 }}>
            <button className="btn btn-primary btn-sm" onClick={(ev) => { ev.stopPropagation(); Store.nav("event", e.id); }}>
              View Intelligence Report <Icon.arrowRight style={{ width: 14, height: 14 }} />
            </button>
            <WatchBtn id={e.id} sm />
          </div>
        </div>
      </div>
    </div>
  );
}

/* Risk radar snapshot row */
function RiskRow({ r, onClick }) {
  return (
    <div onClick={onClick} style={{ display: "flex", alignItems: "center", gap: 12, padding: "9px 0", cursor: "pointer", borderBottom: "1px solid var(--line-soft)" }}>
      <div style={{ width: 132, minWidth: 132 }}>
        <div style={{ fontSize: 12.5, fontWeight: 550 }}>{r.riskType}</div>
        <div style={{ fontSize: 10.5, color: "var(--ink-faint)", marginTop: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.topDriver}</div>
      </div>
      <div className="scorebar" style={{ flex: 1, height: 7 }}>
        <i style={{ width: r.score + "%", background: levelColor(r.level) }} />
      </div>
      <div className="mono tnum" style={{ width: 26, textAlign: "right", fontWeight: 600, fontSize: 13, color: levelColor(r.level) }}>{r.score}</div>
      <div style={{ width: 92, display: "flex", gap: 7, justifyContent: "flex-end" }}>
        <Tip text="24h change"><Delta value={r.change24h} /></Tip>
      </div>
      <Icon.chevR style={{ width: 14, height: 14, color: "var(--ink-faint)" }} />
    </div>
  );
}

/* Industry heatmap cell */
function HeatCell({ it }) {
  const m = dirMeta(it.direction);
  const impact = typeof it.impactScore === "number" && isFinite(it.impactScore) ? it.impactScore : null;
  const intensity = (impact == null ? 0 : impact) / 100;
  const bg = `color-mix(in oklch, ${m.color} ${Math.round(8 + intensity * 26)}%, var(--surface))`;
  return (
    <div className="heat-cell" style={{ background: bg, borderColor: `color-mix(in oklch, ${m.color} 28%, var(--line))`, justifyContent: "flex-start", gap: 10 }}
      onClick={() => Store.nav("industry", it.industryId)}>
      <div style={{ fontSize: 12.5, fontWeight: 600, lineHeight: 1.25, textWrap: "balance" }}>{it.industryName}</div>
      <div style={{ marginTop: "auto" }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
          <span className="mono" style={{ fontSize: 22, fontWeight: 600, color: m.color, lineHeight: 1 }}>{impact == null ? "—" : impact}</span>
          <span style={{ fontSize: 10, color: "var(--ink-3)" }}>impact</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 7, marginTop: 5, fontSize: 10.5, color: "var(--ink-3)" }}>
          <span className={"dir " + m.cls} style={{ fontSize: 10.5 }}>{m.label}</span>
          <span>·</span>
          <span style={{ whiteSpace: "nowrap" }}>{it.relatedEventCount} events</span>
        </div>
      </div>
    </div>
  );
}

/* Company impact table */
function CompanyTable({ rows, watchCol }) {
  const [sort, setSort] = useState({ key: "impactScore", dir: -1 });
  const st = useStore();
  const sorted = useMemo(() => {
    const s = [...rows].sort((a, b) => {
      const av = a[sort.key], bv = b[sort.key];
      if (typeof av === "string") return av.localeCompare(bv) * sort.dir;
      return (av - bv) * sort.dir;
    });
    return s;
  }, [rows, sort]);
  const head = (key, label, cls) => (
    <th className={cls} onClick={() => setSort((s) => ({ key, dir: s.key === key ? -s.dir : -1 }))}>
      {label}{sort.key === key && <span className="arrow">{sort.dir < 0 ? "↓" : "↑"}</span>}
    </th>
  );
  return (
    <div className={"company-table-wrap" + (watchCol ? " with-actions" : "")}>
      <table className="tbl company-table">
        <thead><tr>
          {head("name", "Company")}
          <th className="no-sort">Industry</th>
          {head("impactDirection", "Direction")}
          {head("impactScore", "Impact")}
          {head("riskScore", "Risk")}
          <th className="no-sort">Top Driver</th>
          {head("lastUpdatedAt", "Updated")}
          {watchCol && <th className="no-sort company-action-head"></th>}
        </tr></thead>
        <tbody>
          {sorted.map((c) => (
            <tr key={c.companyId} onClick={() => Store.nav("company", c.companyId)}>
              <td>
                <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
                  <CompanyLogo company={c} size={30} />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 13, whiteSpace: "nowrap" }}>{c.name}</div>
                    <div className="ticker">{c.ticker} · {c.exchange}</div>
                  </div>
                </div>
              </td>
              <td><span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>{c.industry}</span></td>
              <td><DirPill d={c.impactDirection} /></td>
              <td><MiniBar value={c.impactScore} /></td>
              <td><MiniBar value={c.riskScore} risk /></td>
              <td><span style={{ fontSize: 12, color: "var(--ink-2)" }}>{c.topDriver}</span></td>
              <td><span style={{ fontSize: 11.5, color: "var(--ink-faint)" }} className="mono">{timeAgo(c.lastUpdatedAt)}</span></td>
              {watchCol && <td className="company-action-cell"><WatchBtn id={c.companyId} sm /></td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TickerMark({ ticker }) {
  return <CompanyLogo ticker={ticker} size={30} />;
}

function MiniBar({ value, risk }) {
  const has = typeof value === "number" && isFinite(value);
  const col = risk ? levelColor(scoreLevel(has ? value : 0)) : "var(--accent)";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 88 }}>
      <div className="scorebar" style={{ width: 52, height: 6 }}><i style={{ width: (has ? value : 0) + "%", background: col }} /></div>
      <span className="mono tnum" style={{ fontSize: 12.5, fontWeight: 600, width: 22 }}>{has ? value : "—"}</span>
    </div>
  );
}

Object.assign(window, { WatchBtn, MetricCard, HotMeter, HotEventCard, RiskRow, HeatCell, CompanyTable, TickerMark, CompanyLogo, MiniBar });
