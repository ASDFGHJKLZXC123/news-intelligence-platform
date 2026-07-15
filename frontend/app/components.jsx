/* SIGNAL — shared primitives + charts. Exported to window. */
const { useState, useEffect, useRef, useMemo } = React;

/* ---- level helpers -------------------------------------------------------- */
const LEVELS = { low: "low", medium: "med", high: "high", critical: "crit" };
const levelClass = (lvl) => "badge badge-" + (LEVELS[lvl] || "neutral");
function scoreLevel(s) { return s >= 80 ? "critical" : s >= 65 ? "high" : s >= 45 ? "medium" : "low"; }
function levelColor(lvl) {
  return { low: "var(--r-low)", medium: "var(--r-med)", high: "var(--r-high)", critical: "var(--r-crit)" }[lvl] || "var(--ink-3)";
}
function titleCaseLabel(value) {
  return String(value || "—").replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());
}
function pct(value) {
  return value == null ? "—" : Math.round(value * 100) + "%";
}
function ratingHorizons(rating) {
  return [
    { label: "0–6m", value: rating.probability_0_6m },
    { label: "6–12m", value: rating.probability_6_12m },
    { label: "12–18m", value: rating.probability_12_18m },
    { label: "≤18m", value: rating.probability_within_18m },
  ];
}
function dirMeta(d) {
  return {
    positive: { cls: "dir-pos", label: "Positive", color: "var(--dir-pos)" },
    negative: { cls: "dir-neg", label: "Negative", color: "var(--dir-neg)" },
    mixed: { cls: "dir-mix", label: "Mixed", color: "var(--dir-mix)" },
    unknown: { cls: "dir-unk", label: "Unknown", color: "var(--dir-unk)" },
  }[d] || { cls: "dir-unk", label: "—", color: "var(--ink-3)" };
}

/* ---- RiskBadge ------------------------------------------------------------ */
function RiskBadge({ level, label }) {
  const txt = label || (level ? level[0].toUpperCase() + level.slice(1) : "—");
  return <span className={levelClass(level)}><span className="dot" />{txt}</span>;
}

/* ---- Tooltip-wrapped score label ------------------------------------------ */
function Tip({ children, text }) {
  return <span className="tip">{children}<span className="tip-pop">{text}</span></span>;
}

const SCORE_TIPS = {
  Risk: "Risk Score combines estimated probability, impact, urgency, evidence strength, and model consensus (0–100).",
  Hotness: "Hotness Score reflects news velocity, source count, and cross-source corroboration (0–100).",
  Impact: "Impact Score estimates the magnitude of effect on the entity given current evidence (0–100).",
  Confidence: "Confidence reflects evidence coverage, source diversity, and model agreement.",
  Opportunity: "Opportunity Score estimates potential positive exposure from the event (0–100).",
};

/* ---- ScoreStat: label + number + bar -------------------------------------- */
function ScoreStat({ label, value, suffix = "/100", color, tip, compact }) {
  const has = typeof value === "number" && isFinite(value);
  const c = color || levelColor(scoreLevel(has ? value : 0));
  return (
    <div style={{ minWidth: compact ? 0 : 84 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
        <span className="eyebrow" style={{ margin: 0 }}>{label}</span>
        {tip && <Tip text={tip || SCORE_TIPS[label]}><Icon.info style={{ width: 12, height: 12, color: "var(--ink-faint)" }} /></Tip>}
      </div>
      <div className="mono" style={{ fontSize: compact ? 16 : 19, fontWeight: 600, marginTop: 2, lineHeight: 1 }}>
        {has ? value : "—"}{has && <span style={{ fontSize: 11, color: "var(--ink-faint)", fontWeight: 400 }}>{suffix}</span>}
      </div>
      {!compact && (
        <div className="scorebar" style={{ marginTop: 6 }}>
          <i style={{ width: (has ? value : 0) + "%", background: c }} />
        </div>
      )}
    </div>
  );
}

/* ---- ConfidenceBadge ------------------------------------------------------ */
function ConfidenceBadge({ score }) {
  const label = window.DATA.conf(score);
  const cls = score >= 0.7 ? "badge-low" : score >= 0.4 ? "badge-med" : "badge-crit";
  return (
    <Tip text={SCORE_TIPS.Confidence}>
      <span className={"badge " + cls}>Conf {score.toFixed(2)} · {label}</span>
    </Tip>
  );
}

/* ---- Direction pill ------------------------------------------------------- */
function DirPill({ d }) {
  const m = dirMeta(d);
  const ic = d === "positive" ? Icon.arrowUp : d === "negative" ? Icon.arrowDown : d === "mixed" ? Icon.activity : Icon.flat;
  return <span className={"dir " + m.cls}>{ic({ style: { width: 13, height: 13 } })}{m.label}</span>;
}

/* ---- Change indicator ----------------------------------------------------- */
function Delta({ value, suffix = "" }) {
  if (value === 0 || value == null) return <span className="mono" style={{ color: "var(--ink-3)", fontSize: 12 }}>—</span>;
  const up = value > 0;
  const col = up ? "var(--r-high)" : "var(--r-low)";
  const Ic = up ? Icon.arrowUp : Icon.arrowDown;
  return (
    <span className="mono" style={{ color: col, fontSize: 12, display: "inline-flex", alignItems: "center", gap: 2, fontWeight: 600 }}>
      <Ic style={{ width: 12, height: 12 }} />{up ? "+" : ""}{value}{suffix}
    </span>
  );
}

/* ---- TimeAgo -------------------------------------------------------------- */
function timeAgo(iso) {
  const then = new Date(iso).getTime();
  const now = new Date(window.DATA.NOW).getTime();
  const s = Math.max(0, Math.round((now - then) / 1000));
  if (s < 60) return s + "s ago";
  const m = Math.round(s / 60); if (m < 60) return m + "m ago";
  const h = Math.round(m / 60); if (h < 24) return h + "h ago";
  const d = Math.round(h / 24); return d + "d ago";
}
function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

/* ============================================================================
   CHARTS — hand-built SVG
   ========================================================================== */

/* Sparkline */
function Sparkline({ data, w = 120, h = 30, color = "var(--accent)", fill = true, strokeW = 1.6 }) {
  const min = Math.min(...data), max = Math.max(...data);
  const rng = max - min || 1;
  const pts = data.map((v, i) => [(i / (data.length - 1)) * w, h - ((v - min) / rng) * (h - 4) - 2]);
  const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const area = d + ` L${w} ${h} L0 ${h} Z`;
  const id = "sg" + Math.random().toString(36).slice(2, 8);
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="kpi-spark" preserveAspectRatio="none">
      {fill && (
        <>
          <defs><linearGradient id={id} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0" stopColor={color} stopOpacity="0.22" /><stop offset="1" stopColor={color} stopOpacity="0" />
          </linearGradient></defs>
          <path d={area} fill={`url(#${id})`} />
        </>
      )}
      <path d={d} fill="none" stroke={color} strokeWidth={strokeW} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/* Radial gauge (0–100) */
function Gauge({ value, size = 132, label, sub, level }) {
  const has = typeof value === "number" && isFinite(value);
  if (!has) value = 0;
  const lvl = level || scoreLevel(value);
  const col = levelColor(lvl);
  const r = size / 2 - 12;
  const cx = size / 2, cy = size / 2;
  const start = 135, sweep = 270;
  const a0 = (start * Math.PI) / 180;
  const a1 = ((start + (value / 100) * sweep) * Math.PI) / 180;
  const polar = (a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const arc = (from, to) => {
    const [x0, y0] = polar(from), [x1, y1] = polar(to);
    const large = to - from > Math.PI ? 1 : 0;
    return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  };
  const aEnd = ((start + sweep) * Math.PI) / 180;
  return (
    <div style={{ position: "relative", width: size, height: size }}>
      <svg width={size} height={size}>
        <path d={arc(a0, aEnd)} fill="none" stroke="var(--surface-3)" strokeWidth="9" strokeLinecap="round" />
        <path d={arc(a0, a1)} fill="none" stroke={col} strokeWidth="9" strokeLinecap="round"
          style={{ filter: `drop-shadow(0 0 6px ${col}55)` }} />
      </svg>
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center" }}>
        <div className="mono" style={{ fontSize: 30, fontWeight: 600, lineHeight: 1, color: has ? col : "var(--ink-3)" }}>{has ? value : "—"}</div>
        {label && <div className="eyebrow" style={{ marginTop: 4 }}>{label}</div>}
        {sub && <div style={{ fontSize: 10.5, color: "var(--ink-3)", marginTop: 2 }}>{sub}</div>}
      </div>
    </div>
  );
}

/* Radar / spider chart */
function RadarChart({ items, size = 360, max = 100 }) {
  const cx = size / 2, cy = size / 2, r = size / 2 - 60;
  const n = items.length;
  const labelMap = {
    "Geopolitical Risk": "Geopolitical",
    "Policy / Regulatory": "Policy/Reg",
    "Supply Chain Risk": "Supply",
    "Macro Risk": "Macro",
    "Financial Stress": "Financial",
    "Industry Shock": "Ind.",
    "Company Crisis": "Co.",
  };
  const angle = (i) => (-90 + (360 / n) * i) * (Math.PI / 180);
  const pt = (i, val) => [cx + (r * val / max) * Math.cos(angle(i)), cy + (r * val / max) * Math.sin(angle(i))];
  const rings = [0.25, 0.5, 0.75, 1];
  const poly = items.map((it, i) => pt(i, it.score));
  const path = poly.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ") + " Z";
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ maxWidth: "100%", height: "auto" }}>
      {rings.map((rg, idx) => (
        <polygon key={idx}
          points={items.map((_, i) => { const p = pt(i, max * rg); return p[0] + "," + p[1]; }).join(" ")}
          fill="none" stroke="var(--line)" strokeWidth="1" opacity={idx === rings.length - 1 ? 0.9 : 0.5} />
      ))}
      {items.map((_, i) => { const p = pt(i, max); return <line key={i} x1={cx} y1={cy} x2={p[0]} y2={p[1]} stroke="var(--line)" strokeWidth="1" opacity="0.5" />; })}
      <path d={path} fill="var(--accent-soft)" stroke="var(--accent)" strokeWidth="2"
        style={{ filter: "drop-shadow(0 0 8px var(--accent-line))" }} />
      {poly.map((p, i) => <circle key={i} cx={p[0]} cy={p[1]} r="3.2" fill="var(--accent)" stroke="var(--bg)" strokeWidth="1.5" />)}
      {items.map((it, i) => {
        const lp = pt(i, max * 1.17);
        const anchor = Math.abs(lp[0] - cx) < 12 ? "middle" : lp[0] > cx ? "start" : "end";
        const col = levelColor(scoreLevel(it.score));
        return (
          <g key={"l" + i}>
            <text x={lp[0]} y={lp[1] - 4} textAnchor={anchor} fontSize="10.5" fill="var(--ink-2)"
              fontFamily="var(--font-mono)" style={{ letterSpacing: 0 }}>
              {labelMap[it.riskType] || it.riskType.replace(" Risk", "").replace(" / Regulatory", "/Reg")}
            </text>
            <text x={lp[0]} y={lp[1] + 9} textAnchor={anchor} fontSize="11" fill={col} fontFamily="var(--font-mono)" fontWeight="600">
              {it.score}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/* Multi-series trend / line chart */
function TrendChart({ series, w = 640, h = 220, max = 100, labels }) {
  const padL = 30, padB = 22, padT = 12, padR = 12;
  const iw = w - padL - padR, ih = h - padT - padB;
  const xs = (i, len) => padL + (i / (len - 1)) * iw;
  const ys = (v) => padT + ih - (v / max) * ih;
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
      {[0, 25, 50, 75, 100].map((g) => (
        <g key={g}>
          <line x1={padL} y1={ys(g)} x2={w - padR} y2={ys(g)} stroke="var(--line-soft)" strokeWidth="1" />
          <text x={padL - 6} y={ys(g) + 3} textAnchor="end" fontSize="9" fill="var(--ink-faint)" fontFamily="var(--font-mono)">{g}</text>
        </g>
      ))}
      {series.map((s, si) => {
        const d = s.data.map((v, i) => (i ? "L" : "M") + xs(i, s.data.length).toFixed(1) + " " + ys(v).toFixed(1)).join(" ");
        return <path key={si} d={d} fill="none" stroke={s.color} strokeWidth={s.bold ? 2.4 : 1.6} strokeLinejoin="round"
          opacity={s.dim ? 0.4 : 1} style={s.bold ? { filter: `drop-shadow(0 0 5px ${s.color}55)` } : null} />;
      })}
      {labels && labels.map((lb, i) => i % 6 === 0 && (
        <text key={i} x={xs(i, labels.length)} y={h - 6} textAnchor="middle" fontSize="9" fill="var(--ink-faint)" fontFamily="var(--font-mono)">{lb}</text>
      ))}
    </svg>
  );
}

/* Horizontal bar list */
function BarList({ rows, max = 100 }) {
  return (
    <div className="stack" style={{ gap: 10 }}>
      {rows.map((r, i) => {
        const has = typeof r.value === "number" && isFinite(r.value);
        return (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ width: 130, fontSize: 12.5, color: "var(--ink-2)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{r.label}</div>
          <div className="scorebar" style={{ flex: 1, height: 8 }}>
            <i style={{ width: (has ? r.value / max * 100 : 0) + "%", background: r.color || "var(--accent)" }} />
          </div>
          <div className="mono tnum" style={{ width: 34, textAlign: "right", fontSize: 12.5, fontWeight: 600 }}>{has ? r.value : "—"}</div>
        </div>
        );
      })}
    </div>
  );
}

/* Probability stacked bar (scenarios) */
function ProbBar({ scenarios }) {
  const colors = { base_case: "var(--accent)", upside_case: "var(--r-low)", downside_case: "var(--r-high)", tail_risk_case: "var(--r-crit)" };
  return (
    <div style={{ display: "flex", height: 12, borderRadius: 6, overflow: "hidden", gap: 2 }}>
      {scenarios.map((s) => (
        <Tip key={s.id} text={`${scenarioName(s.name)} — ${Math.round(s.probability * 100)}%`}>
          <div style={{ width: s.probability * 100 + "%", height: 12, background: colors[s.name], borderRadius: 3 }} />
        </Tip>
      ))}
    </div>
  );
}
function scenarioName(n) {
  return { base_case: "Base Case", upside_case: "Upside Case", downside_case: "Downside Case", tail_risk_case: "Tail Risk" }[n] || n;
}

/* Card wrapper */
function Card({ title, icon, eyebrow, action, children, className = "", bodyClass = "card-pad", noBody }) {
  const Ic = icon;
  return (
    <section className={"card " + className}>
      {(title || action) && (
        <div className="card-head">
          {Ic && <Ic className="card-title-ico" />}
          <div style={{ flex: 1 }}>
            {eyebrow && <div className="eyebrow">{eyebrow}</div>}
            {title && <h3>{title}</h3>}
          </div>
          {action}
        </div>
      )}
      {noBody ? children : <div className={bodyClass}>{children}</div>}
    </section>
  );
}

/* Empty state */
function EmptyState({ icon, title, hint, action }) {
  const Ic = icon || Icon.search;
  return (
    <div className="empty">
      <Ic className="ico" />
      <div style={{ fontSize: 14, color: "var(--ink-2)", fontWeight: 550 }}>{title}</div>
      {hint && <div style={{ fontSize: 12.5, maxWidth: 320 }}>{hint}</div>}
      {action}
    </div>
  );
}

/* ============================================================================
   DATA-QUALITY SURFACES (Stage 7, item 7)
   Pure quality/format logic lives in app/data-quality.js (window.SignalDataQuality);
   these are the thin presentational wrappers. A live block renders NO badge.
   ========================================================================== */
const DQ = window.SignalDataQuality;

/* A subtle, non-live data badge. Renders nothing for a live/unknown quality so
   healthy blocks stay quiet. `label`/`title` override the quality defaults. */
function DataQualityBadge({ quality, label, title, style }) {
  const meta = DQ.qualityMeta(quality);
  if (!meta) return null;
  const text = label || meta.label;
  const tip = title || meta.title;
  return (
    <span className={"dq-badge dq-" + meta.quality} style={style}
      role="note" aria-label={"Data status: " + text + ". " + tip} title={tip}>
      <span className="dq-dot" aria-hidden="true" />{text}
    </span>
  );
}

/* Section-scoped badge bound to one window.DATA block by name. */
function BlockBadge({ name, label, style }) {
  return <DataQualityBadge quality={DQ.blockQuality(window.DATA, name)} label={label} style={style} />;
}

/* Compact page-header status: one clearly-labelled badge summarizing a route's
   top-level blocks (or an explicit `quality`). Quiet when everything is live. */
function PageStatus({ blocks, quality, label, style }) {
  const q = quality || DQ.aggregateQuality(window.DATA, blocks || []);
  const meta = DQ.qualityMeta(q);
  if (!meta) return null;
  return (
    <div className="page-status" role="status" style={style}>
      <DataQualityBadge quality={q} label={label} />
    </div>
  );
}

/* Persistent, calm demo-data banner — the whole snapshot is bundled fixture data
   because the backend was wholly unreachable. */
function DemoBanner() {
  return (
    <div className="global-notice demo-banner" role="status" aria-live="polite">
      <Icon.warn className="notice-ico" aria-hidden="true" />
      <div className="notice-text">
        <strong>Demo data</strong>
        <span>The intelligence backend is unreachable, so SIGNAL is showing bundled fixture data. Values are illustrative, not live.</span>
      </div>
    </div>
  );
}

/* Calm, non-destructive degraded notice — the API is reachable but some blocks
   failed to load. Successful blocks stay on the page; this only explains gaps
   and surfaces any backend request IDs (labelled in plain terms). */
function DegradedNotice() {
  const [open, setOpen] = useState(false);
  const D = window.DATA;
  const details = DQ.errorSummaries(D);
  const ids = DQ.requestIds(D);
  return (
    <div className="global-notice degraded-notice" role="status" aria-live="polite">
      <div className="notice-row">
        <Icon.activity className="notice-ico" aria-hidden="true" />
        <div className="notice-text">
          <strong>Some live data is unavailable</strong>
          <span>Showing the data that loaded successfully — other sections will fill in once the backend recovers.</span>
        </div>
        {details.length > 0 && (
          <button className="btn btn-sm btn-ghost notice-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? "Hide details" : "Details"}
          </button>
        )}
      </div>
      {ids.length > 0 && (
        <div className="notice-reqids">
          {ids.length > 1 ? "Request IDs" : "Request ID"}: {ids.map((id, i) => <code key={i}>{id}</code>)}
        </div>
      )}
      {open && details.length > 0 && (
        <ul className="notice-details">
          {details.map((e, i) => (
            <li key={i}>
              <span className="notice-msg">{e.message}</span>
              {e.requestId && <span className="notice-reqid">Request ID <code>{e.requestId}</code></span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* One global surface: the demo banner (whole-snapshot demo) OR the degraded
   notice (reachable but partial) — never both, and quiet when all-live. */
function GlobalDataNotices() {
  const D = window.DATA;
  if (!D) return null;
  if (DQ.isDemo(D)) return <DemoBanner />;
  if (DQ.isDegraded(D)) return <DegradedNotice />;
  return null;
}

Object.assign(window, {
  LEVELS, levelClass, scoreLevel, levelColor, dirMeta, scenarioName,
  titleCaseLabel, pct, ratingHorizons,
  RiskBadge, Tip, SCORE_TIPS, ScoreStat, ConfidenceBadge, DirPill, Delta,
  timeAgo, fmtTime, Sparkline, Gauge, RadarChart, TrendChart, BarList, ProbBar, Card, EmptyState,
  DataQualityBadge, BlockBadge, PageStatus, DemoBanner, DegradedNotice, GlobalDataNotices,
});
