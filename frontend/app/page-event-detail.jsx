/* SIGNAL — Event Intelligence detail page (the core analysis view) */
const { useState, useEffect, useRef, useMemo } = React;

function SourceBadge({ id, inline }) {
  const ev = window.DATA.evidenceById[id];
  if (!ev) return null;
  const typeLabel = { rss: "Article", news_api: "Article", official: "Official", filing: "Filing", macro_indicator: "Macro", market_data: "Market", banking_data: "Banking", historical_case: "Historical", article: "Article", official_statement: "Official" }[ev.sourceType] || "Source";
  return (
    <span className="chip chip-x" onClick={(e) => { e.stopPropagation(); Store.openDrawer({ type: "evidence", data: ev }); }}
      style={{ fontSize: 11 }} title={ev.title}>
      <Icon.doc style={{ width: 12, height: 12, color: "var(--accent)" }} />
      {inline ? ev.publisher : typeLabel}
    </span>
  );
}

function RatingProbabilityGrid({ rating, compact }) {
  const col = levelColor(rating.risk_level);
  return (
    <div className="grid" style={{ gridTemplateColumns: compact ? "1fr 1fr" : "repeat(4, minmax(0, 1fr))", gap: compact ? 8 : 12 }}>
      {ratingHorizons(rating).map((h) => (
        <div key={h.label} style={{ padding: compact ? 10 : 14, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)", textAlign: "center" }}>
          <div className="mono" style={{ fontSize: compact ? 18 : 24, fontWeight: 600, color: h.label === "≤18m" ? col : "var(--ink)" }}>{pct(h.value)}</div>
          <div className="eyebrow" style={{ marginTop: 3 }}>{h.label}</div>
        </div>
      ))}
    </div>
  );
}

function RatingDriverList({ rating, limit = 4 }) {
  const drivers = (rating.top_drivers || []).slice(0, limit);
  if (!drivers.length) return null;
  return (
    <div className="stack" style={{ gap: 9 }}>
      {drivers.map((d, i) => {
        const label = d.signal || d.name || titleCaseLabel(d.component);
        const score = d.score ?? d.contribution ?? d.similarity_score;
        const refs = (d.evidence_refs || []).map((ref) => ref.source_id).filter(Boolean).slice(0, 2);
        return (
          <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
            <span style={{ width: 5, height: 5, borderRadius: "50%", background: levelColor(rating.risk_level), marginTop: 7, flexShrink: 0 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <span style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.4 }}>{label}</span>
                {score != null && <span className="mono" style={{ fontSize: 10.5, color: "var(--ink-faint)" }}>{typeof score === "number" && score <= 1 ? pct(score) : Math.round(score)}</span>}
              </div>
              {!!refs.length && <div style={{ display: "flex", gap: 5, marginTop: 5, flexWrap: "wrap" }}>{refs.map((id) => <SourceBadge key={id} id={id} inline />)}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function RatingPathList({ label, items, icon, color }) {
  if (!items || !items.length) return null;
  const Ic = icon;
  return (
    <div style={{ padding: 12, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)" }}>
      <div className="eyebrow" style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 6, color }}>
        <Ic style={{ width: 13, height: 13 }} />{label}
      </div>
      <div className="stack" style={{ gap: 5 }}>
        {items.map((item) => <div key={item} style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.45 }}>· {item}</div>)}
      </div>
    </div>
  );
}

function SidebarRatingCard({ rating }) {
  const score = Math.round(rating.risk_score);
  return (
    <Card icon={Icon.shield} title="Model Rating" eyebrow={titleCaseLabel(rating.risk_type)}>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 9 }}>
        <Gauge value={score} size={128} label="Rating" level={rating.risk_level} />
        <RiskBadge level={rating.risk_level} label={titleCaseLabel(rating.risk_level) + " · " + score + "/100"} />
        <ConfidenceBadge score={rating.confidence_score} />
      </div>
      <div style={{ marginTop: 14 }}>
        <RatingProbabilityGrid rating={rating} compact />
      </div>
    </Card>
  );
}

function FormalRatingCard({ rating }) {
  const score = Math.round(rating.risk_score);
  return (
    <Card title="Validated Crisis Rating" icon={Icon.shield} eyebrow={titleCaseLabel(rating.risk_type) + " · " + rating.as_of_date}>
      <div className="grid" style={{ gridTemplateColumns: "170px minmax(0, 1fr)", alignItems: "center" }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8 }}>
          <Gauge value={score} size={138} label="Rating" level={rating.risk_level} />
          <RiskBadge level={rating.risk_level} label={titleCaseLabel(rating.risk_level)} />
        </div>
        <div className="stack">
          <RatingProbabilityGrid rating={rating} />
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <ConfidenceBadge score={rating.confidence_score} />
            <span className="badge badge-neutral">Target {rating.target_type} · {rating.target_id}</span>
            <span className="badge badge-neutral">{Object.keys(rating.model_versions || {}).length} model versions</span>
          </div>
        </div>
      </div>
      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginTop: 16 }}>
        <div style={{ padding: 12, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)" }}>
          <div className="eyebrow" style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 6 }}>
            <Icon.bolt style={{ width: 13, height: 13, color: "var(--accent)" }} />Top Drivers
          </div>
          <RatingDriverList rating={rating} />
        </div>
        <div className="stack">
          <RatingPathList label="Could Escalate" items={rating.what_could_escalate} icon={Icon.arrowUp} color="var(--r-high)" />
          <RatingPathList label="Could Reduce Risk" items={rating.what_could_reduce_risk} icon={Icon.arrowDown} color="var(--r-low)" />
        </div>
      </div>
    </Card>
  );
}

/* ---- Tabs ---------------------------------------------------------------- */
function TabOverview({ e }) {
  const o = e.overview;
  if (!o) return <PartialNotice e={e} />;
  const Section = ({ label, items, num }) => (
    <div>
      <div className="eyebrow" style={{ marginBottom: 9 }}>{label}</div>
      <div className="stack" style={{ gap: 7 }}>
        {items.map((it, i) => (
          <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
            <span style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--accent)", marginTop: 7, flexShrink: 0 }} />
            <span style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.5 }}>{it}</span>
          </div>
        ))}
      </div>
    </div>
  );
  return (
    <div className="stack">
      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Card icon={Icon.info} title="What Happened">
          <p style={{ fontSize: 13.5, lineHeight: 1.6, color: "var(--ink-2)", margin: 0, textWrap: "pretty" }}>{o.whatHappened}</p>
        </Card>
        <Card icon={Icon.bolt} title="Why It Matters">
          <p style={{ fontSize: 13.5, lineHeight: 1.6, color: "var(--ink-2)", margin: 0, textWrap: "pretty" }}>{o.whyItMatters}</p>
        </Card>
      </div>
      <Card title="Key Actors" icon={Icon.users}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>{o.keyActors.map((a) => <span key={a} className="chip">{a}</span>)}</div>
      </Card>
      <Card title="Implications by Horizon" icon={Icon.activity}>
        <div className="grid" style={{ gridTemplateColumns: "repeat(3, 1fr)" }}>
          <div style={{ padding: 14, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)" }}><Section label="Short Term" items={o.shortTermImplications} /></div>
          <div style={{ padding: 14, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)" }}><Section label="Medium Term" items={o.mediumTermImplications} /></div>
          <div style={{ padding: 14, background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)" }}><Section label="Long Term" items={o.longTermImplications} /></div>
        </div>
      </Card>
      <Card title="Key Uncertainties" icon={Icon.warn}>
        <Section label="" items={o.keyUncertainties} />
      </Card>
    </div>
  );
}

function TabTimeline({ e }) {
  if (!e.timeline) return <PartialNotice e={e} />;
  return (
    <Card title="Event Development" icon={Icon.clock}>
      <div style={{ position: "relative" }}>
        {e.timeline.map((t, i) => {
          const col = levelColor(t.importance === "high" ? "high" : t.importance === "medium" ? "medium" : "low");
          return (
            <div key={t.id} style={{ display: "flex", gap: 16, paddingBottom: i < e.timeline.length - 1 ? 22 : 0 }}>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                <div style={{ width: 13, height: 13, borderRadius: "50%", background: col, border: "3px solid var(--surface)", boxShadow: `0 0 0 1px ${col}`, flexShrink: 0, zIndex: 1 }} />
                {i < e.timeline.length - 1 && <div style={{ width: 2, flex: 1, background: "var(--line)", marginTop: 2 }} />}
              </div>
              <div style={{ flex: 1, paddingBottom: 4 }}>
                <div className="mono" style={{ fontSize: 11, color: "var(--ink-faint)", marginBottom: 3 }}>{fmtTime(t.timestamp)}</div>
                <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 3 }}>{t.title}</div>
                <div style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.5 }}>{t.description}</div>
                {t.sourceIds.length > 0 && <div style={{ display: "flex", gap: 6, marginTop: 8 }}>{t.sourceIds.map((s) => <SourceBadge key={s} id={s} inline />)}</div>}
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function TabIndustries({ e }) {
  if (!e.relatedIndustries) return <PartialNotice e={e} />;
  return (
    <div className="stack">
      {e.relatedIndustries.map((ind) => {
        const m = dirMeta(ind.impactDirection);
        return (
          <Card key={ind.industryId} bodyClass="card-pad">
            <div style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 14 }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
                  <h3 style={{ fontSize: 16 }}>{ind.name}</h3>
                  <DirPill d={ind.impactDirection} />
                </div>
                <ConfidenceBadge score={ind.confidenceScore} />
              </div>
              <div style={{ display: "flex", gap: 18 }}>
                <ScoreStat label="Impact" value={ind.impactScore} tip compact />
                <ScoreStat label="Risk" value={ind.riskScore} tip compact />
                <ScoreStat label="Opportunity" value={ind.opportunityScore} tip compact color="var(--r-low)" />
              </div>
            </div>
            <div style={{ padding: "12px 14px", background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)", marginBottom: 14 }}>
              <div className="eyebrow" style={{ marginBottom: 5 }}>Impact Pathway</div>
              <p style={{ fontSize: 13, color: "var(--ink-2)", margin: 0, lineHeight: 1.55 }}>{ind.impactPathway}</p>
            </div>
            <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
              <div>
                <div className="eyebrow" style={{ marginBottom: 8 }}>Related Companies</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {ind.relatedCompanies.map((c) => (
                    <span key={c.companyId} className="chip chip-x" onClick={() => Store.nav("company", c.companyId)}>
                      <CompanyLogo company={c} size={18} />{c.name} <span className="ticker">{c.ticker}</span>
                    </span>
                  ))}
                </div>
                {ind.historicalSensitivity && <div style={{ marginTop: 12 }}><div className="eyebrow" style={{ marginBottom: 5 }}>Historical Sensitivity</div><p style={{ fontSize: 12.5, color: "var(--ink-3)", margin: 0, lineHeight: 1.5 }}>{ind.historicalSensitivity}</p></div>}
              </div>
              <div>
                <div className="eyebrow" style={{ marginBottom: 8 }}>Key Monitoring Indicators</div>
                <div className="stack" style={{ gap: 6 }}>
                  {ind.keyIndicators.map((k) => <div key={k} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5, color: "var(--ink-2)" }}><Icon.target style={{ width: 13, height: 13, color: "var(--accent)" }} />{k}</div>)}
                </div>
              </div>
            </div>
          </Card>
        );
      })}
    </div>
  );
}

function TabCompanies({ e }) {
  if (!e.relatedCompanies) return <PartialNotice e={e} />;
  return (
    <div className="stack">
      {e.relatedCompanies.map((c) => (
        <Card key={c.companyId} bodyClass="card-pad">
          <div style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 14 }}>
            <CompanyLogo company={c} size={34} />
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <h3 style={{ fontSize: 16 }}>{c.name}</h3>
                <span className="ticker">{c.ticker} · {c.exchange}</span>
                <DirPill d={c.impactDirection} />
              </div>
              <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 2 }}>{c.industry}</div>
            </div>
            <div style={{ display: "flex", gap: 18 }}>
              <ScoreStat label="Impact" value={c.impactScore} tip compact />
              <ScoreStat label="Risk" value={c.riskScore} tip compact />
            </div>
            <WatchBtn id={c.companyId} sm />
          </div>
          <div style={{ padding: "12px 14px", background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)", marginBottom: 14 }}>
            <div className="eyebrow" style={{ marginBottom: 5 }}>Exposure</div>
            <p style={{ fontSize: 13, color: "var(--ink-2)", margin: 0, lineHeight: 1.55 }}>{c.exposureExplanation}</p>
          </div>
          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <div>
              <div className="eyebrow" style={{ marginBottom: 8 }}>Evidence</div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {c.evidenceSourceIds.map((s) => <SourceBadge key={s} id={s} inline />)}
                {c.evidenceSourceIds.length === 0 && <span className="muted" style={{ fontSize: 12 }}>Proxy & internal signals</span>}
              </div>
            </div>
            <div>
              <div className="eyebrow" style={{ marginBottom: 8 }}>Monitoring Indicators</div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{c.monitoringIndicators.map((k) => <span key={k} className="chip" style={{ fontSize: 11 }}>{k}</span>)}</div>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

function TabHistorical({ e }) {
  if (!e.historicalAnalogies) return <PartialNotice e={e} />;
  return (
    <div className="stack">
      {e.historicalAnalogies.map((a) => (
        <Card key={a.historicalEventId} bodyClass="card-pad">
          <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
            <Icon.historical style={{ width: 22, height: 22, color: "var(--accent)" }} />
            <div style={{ flex: 1 }}>
              <h3 style={{ fontSize: 16 }}>{a.title}</h3>
              <div className="mono" style={{ fontSize: 11.5, color: "var(--ink-faint)", marginTop: 2 }}>{a.startDate} → {a.endDate}</div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div className="eyebrow">Similarity</div>
              <div className="mono" style={{ fontSize: 24, fontWeight: 600, color: "var(--accent)", lineHeight: 1 }}>{(a.similarityScore * 100).toFixed(0)}<span style={{ fontSize: 12, color: "var(--ink-faint)" }}>%</span></div>
            </div>
          </div>
          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 14 }}>
            <div style={{ padding: 14, background: "color-mix(in oklch, var(--r-low) 8%, var(--surface-2))", borderRadius: 10, border: "1px solid color-mix(in oklch, var(--r-low) 22%, var(--line))" }}>
              <div className="eyebrow" style={{ color: "var(--r-low)", marginBottom: 8 }}>✓ Similarities</div>
              <div className="stack" style={{ gap: 6 }}>{a.similarities.map((s) => <div key={s} style={{ fontSize: 12.5, color: "var(--ink-2)", display: "flex", gap: 7 }}><span>·</span>{s}</div>)}</div>
            </div>
            <div style={{ padding: 14, background: "color-mix(in oklch, var(--r-high) 8%, var(--surface-2))", borderRadius: 10, border: "1px solid color-mix(in oklch, var(--r-high) 22%, var(--line))" }}>
              <div className="eyebrow" style={{ color: "var(--r-high)", marginBottom: 8 }}>Δ Differences</div>
              <div className="stack" style={{ gap: 6 }}>{a.differences.map((s) => <div key={s} style={{ fontSize: 12.5, color: "var(--ink-2)", display: "flex", gap: 7 }}><span>·</span>{s}</div>)}</div>
            </div>
          </div>
          <div style={{ marginBottom: 14 }}>
            <div className="eyebrow" style={{ marginBottom: 6 }}>Historical Outcome</div>
            <p style={{ fontSize: 13, color: "var(--ink-2)", margin: 0, lineHeight: 1.55 }}>{a.historicalOutcome}</p>
          </div>
          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <div>
              <div className="eyebrow" style={{ marginBottom: 6 }}>Lessons for Current Event</div>
              <div className="stack" style={{ gap: 5 }}>{a.lessonsForCurrentEvent.map((l) => <div key={l} style={{ fontSize: 12.5, color: "var(--ink-2)", display: "flex", gap: 7 }}><Icon.check style={{ width: 13, height: 13, color: "var(--r-low)", flexShrink: 0, marginTop: 1 }} />{l}</div>)}</div>
            </div>
            <div style={{ padding: 12, background: "var(--surface-2)", borderRadius: 10, border: "1px dashed var(--line)" }}>
              <div className="eyebrow" style={{ marginBottom: 6, color: "var(--r-crit)" }}>Why This Analogy May Be Wrong</div>
              <div className="stack" style={{ gap: 5 }}>{a.analogyLimitations.map((l) => <div key={l} style={{ fontSize: 12.5, color: "var(--ink-3)", display: "flex", gap: 7 }}><span>·</span>{l}</div>)}</div>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

function TabForecast({ e }) {
  if (!e.forecastScenarios) return <PartialNotice e={e} />;
  const colors = { base_case: "var(--accent)", upside_case: "var(--r-low)", downside_case: "var(--r-high)", tail_risk_case: "var(--r-crit)" };
  return (
    <div className="stack">
      {e.crisisRating && <FormalRatingCard rating={e.crisisRating} />}
      <Card title="Scenario Probability Distribution" icon={Icon.scale} eyebrow="Probabilistic — not deterministic">
        <ProbBar scenarios={e.forecastScenarios} />
        <div style={{ display: "flex", gap: 16, marginTop: 12, flexWrap: "wrap" }}>
          {e.forecastScenarios.map((s) => (
            <span key={s.id} style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 12 }}>
              <span style={{ width: 9, height: 9, borderRadius: 3, background: colors[s.name] }} />
              <span style={{ color: "var(--ink-2)" }}>{scenarioName(s.name)}</span>
              <span className="mono" style={{ fontWeight: 600 }}>{Math.round(s.probability * 100)}%</span>
            </span>
          ))}
        </div>
      </Card>
      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        {e.forecastScenarios.map((s) => (
          <Card key={s.id} bodyClass="card-pad" className="">
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
              <span style={{ width: 4, height: 30, borderRadius: 3, background: colors[s.name] }} />
              <div style={{ flex: 1 }}>
                <h3 style={{ fontSize: 15 }}>{scenarioName(s.name)}</h3>
                <div style={{ fontSize: 11.5, color: "var(--ink-faint)" }}>{s.timeHorizon}</div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div className="mono" style={{ fontSize: 26, fontWeight: 600, color: colors[s.name], lineHeight: 1 }}>{Math.round(s.probability * 100)}<span style={{ fontSize: 13, color: "var(--ink-faint)" }}>%</span></div>
              </div>
            </div>
            <p style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.55, margin: "0 0 14px" }}>{s.narrative}</p>
            <div className="stack" style={{ gap: 12 }}>
              <ListBlock label="Triggers" icon={Icon.bolt} items={s.triggers} />
              <ListBlock label="Leading Indicators" icon={Icon.activity} items={s.leadingIndicators} />
              <div style={{ padding: 11, background: "color-mix(in oklch, var(--r-crit) 7%, var(--surface-2))", borderRadius: 9, border: "1px solid color-mix(in oklch, var(--r-crit) 20%, var(--line))" }}>
                <div className="eyebrow" style={{ marginBottom: 7, color: "var(--r-crit)" }}>⊘ Invalidation Signals</div>
                <div className="stack" style={{ gap: 4 }}>{s.invalidationSignals.map((x) => <div key={x} style={{ fontSize: 12, color: "var(--ink-2)" }}>· {x}</div>)}</div>
              </div>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 13, paddingTop: 11, borderTop: "1px solid var(--line)" }}>
              <ConfidenceBadge score={s.confidenceScore} />
              <span className="mono" style={{ fontSize: 10.5, color: "var(--ink-faint)" }}>updated {timeAgo(s.lastUpdatedAt)}</span>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

function ListBlock({ label, icon, items }) {
  const Ic = icon;
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 7, display: "flex", alignItems: "center", gap: 6 }}><Ic style={{ width: 13, height: 13, color: "var(--accent)" }} />{label}</div>
      <div className="stack" style={{ gap: 4 }}>{items.map((x) => <div key={x} style={{ fontSize: 12.5, color: "var(--ink-2)" }}>· {x}</div>)}</div>
    </div>
  );
}

function TabEvidence({ e }) {
  if (!e.claimMap) return <PartialNotice e={e} />;
  const groups = {};
  (e.evidenceSourceIds || []).forEach((id) => { const ev = window.DATA.evidenceById[id]; if (!ev) return; (groups[ev.sourceType] = groups[ev.sourceType] || []).push(ev); });
  const typeLabels = { rss: "Source Articles", news_api: "Source Articles", official: "Official Sources", filing: "Company Filings", macro_indicator: "Macro Indicators", market_data: "Market Data", banking_data: "Banking Data", historical_case: "Historical References", article: "Source Articles", official_statement: "Official Sources" };
  return (
    <div className="stack">
      <Card title="Claim-to-Evidence Map" icon={Icon.link} eyebrow="Every AI claim is traceable">
        <div className="stack" style={{ gap: 10 }}>
          {e.claimMap.map((c, i) => (
            <div key={i} style={{ display: "flex", gap: 14, alignItems: "center", padding: "11px 13px", background: "var(--surface-2)", borderRadius: 10, border: "1px solid var(--line)" }}>
              <Icon.quote style={{ width: 16, height: 16, color: "var(--accent)", flexShrink: 0 }} />
              <span style={{ flex: 1, fontSize: 13, color: "var(--ink)", fontWeight: 500 }}>{c.claim}</span>
              <div style={{ display: "flex", gap: 6 }}>{c.evidenceIds.length ? c.evidenceIds.map((id) => <SourceBadge key={id} id={id} />) : <span className="muted" style={{ fontSize: 11.5 }}>internal signal</span>}</div>
            </div>
          ))}
        </div>
      </Card>
      {Object.keys(groups).map((type) => (
        <Card key={type} title={typeLabels[type] || type} icon={Icon.doc} eyebrow={groups[type].length + " sources"}>
          <div className="stack" style={{ gap: 0 }}>
            {groups[type].map((ev, i) => (
              <div key={ev.id} onClick={() => Store.openDrawer({ type: "evidence", data: ev })}
                style={{ display: "flex", gap: 12, alignItems: "center", padding: "12px 0", borderBottom: i < groups[type].length - 1 ? "1px solid var(--line-soft)" : "none", cursor: "pointer" }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 550 }}>{ev.title}</div>
                  <div style={{ fontSize: 11.5, color: "var(--ink-faint)", marginTop: 2 }}>{ev.publisher} · {ev.publishedAt ? fmtTime(ev.publishedAt) : "—"}</div>
                </div>
                {ev.credibilityScore != null && <Tip text="Source credibility score"><span className="badge badge-neutral">cred {ev.credibilityScore.toFixed(2)}</span></Tip>}
                <Icon.chevR style={{ width: 15, height: 15, color: "var(--ink-faint)" }} />
              </div>
            ))}
          </div>
        </Card>
      ))}
    </div>
  );
}

function TabDebate({ e }) {
  if (!e.modelDebate) return <PartialNotice e={e} />;
  const d = e.modelDebate;
  return (
    <div className="stack">
      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Card title="Model Consensus" icon={Icon.check} eyebrow="Where models agree">
          <div className="stack" style={{ gap: 8 }}>{d.consensusPoints.map((p, i) => <div key={i} style={{ display: "flex", gap: 9, fontSize: 13, color: "var(--ink-2)", lineHeight: 1.5 }}><Icon.check style={{ width: 14, height: 14, color: "var(--r-low)", flexShrink: 0, marginTop: 2 }} />{p}</div>)}</div>
        </Card>
        <Card title="Disagreements" icon={Icon.activity} eyebrow="Where models diverge">
          <div className="stack" style={{ gap: 8 }}>{d.disagreementPoints.map((p, i) => <div key={i} style={{ display: "flex", gap: 9, fontSize: 13, color: "var(--ink-2)", lineHeight: 1.5 }}><Icon.activity style={{ width: 14, height: 14, color: "var(--r-med)", flexShrink: 0, marginTop: 2 }} />{p}</div>)}</div>
        </Card>
      </div>
      <Card title="Model Votes" icon={Icon.cpu} eyebrow={d.modelVotes.length + " models"} bodyClass="">
        <table className="tbl">
          <thead><tr><th className="no-sort">Model</th><th className="no-sort">Risk Read</th><th className="no-sort">Prob.</th><th className="no-sort">Summary</th></tr></thead>
          <tbody>
            {d.modelVotes.map((v) => (
              <tr key={v.modelName} style={{ cursor: "default" }}>
                <td><span className="mono" style={{ fontSize: 12.5, fontWeight: 600 }}>{v.modelName}</span></td>
                <td><RiskBadge level={v.riskLevel} /></td>
                <td><span className="mono" style={{ fontSize: 13 }}>{v.probabilityEstimate != null ? Math.round(v.probabilityEstimate * 100) + "%" : "—"}</span></td>
                <td><span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>{v.summary}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      {e.crisisRating && (
        <Card title="Rating Model Versions" icon={Icon.layers} eyebrow="Prediction contract">
          <div style={{ display: "flex", gap: 7, flexWrap: "wrap" }}>
            {Object.entries(e.crisisRating.model_versions || {}).map(([k, v]) => (
              <span key={k} className="chip" style={{ fontSize: 11 }}>{titleCaseLabel(k)} · <span className="mono">{v}</span></span>
            ))}
          </div>
        </Card>
      )}
      <Card title="Critic Notes" icon={Icon.warn} eyebrow="Adversarial review" className="">
        <div style={{ padding: 14, background: "color-mix(in oklch, var(--r-crit) 6%, var(--surface-2))", borderRadius: 10, border: "1px solid color-mix(in oklch, var(--r-crit) 20%, var(--line))" }}>
          <div className="stack" style={{ gap: 8 }}>{d.criticNotes.map((p, i) => <div key={i} style={{ display: "flex", gap: 9, fontSize: 13, color: "var(--ink-2)", lineHeight: 1.5 }}><Icon.warn style={{ width: 14, height: 14, color: "var(--r-crit)", flexShrink: 0, marginTop: 2 }} />{p}</div>)}</div>
        </div>
      </Card>
    </div>
  );
}

function PartialNotice({ e }) {
  return <Card><EmptyState icon={Icon.layers} title="Deep analysis is still generating for this event"
    hint="This event cluster has been detected and scored. The full intelligence breakdown for this tab is being assembled by the pipeline and will populate shortly." /></Card>;
}

/* ---- Header + sidebar + page --------------------------------------------- */
const EVENT_TABS = [
  { id: "overview", label: "Overview", icon: Icon.info, C: TabOverview, need: "overview" },
  { id: "timeline", label: "Timeline", icon: Icon.clock, C: TabTimeline, need: "timeline" },
  { id: "industries", label: "Related Industries", icon: Icon.industries, C: TabIndustries, need: "relatedIndustries", count: (e) => e.relatedIndustries && e.relatedIndustries.length },
  { id: "companies", label: "Related Companies", icon: Icon.companies, C: TabCompanies, need: "relatedCompanies", count: (e) => e.relatedCompanies && e.relatedCompanies.length },
  { id: "historical", label: "Historical Analogies", icon: Icon.historical, C: TabHistorical, need: "historicalAnalogies", count: (e) => e.historicalAnalogies && e.historicalAnalogies.length },
  { id: "forecast", label: "Forecast Scenarios", icon: Icon.scale, C: TabForecast, need: "forecastScenarios", count: (e) => e.forecastScenarios && e.forecastScenarios.length },
  { id: "evidence", label: "Evidence", icon: Icon.doc, C: TabEvidence, need: "claimMap" },
  { id: "debate", label: "Model Debate", icon: Icon.cpu, C: TabDebate, need: "modelDebate" },
];

function EventDetail({ id }) {
  const e = window.DATA.eventsById[id];
  const [tab, setTab] = useState("overview");
  if (!e) return <div className="page"><EmptyState title="Event not found" /></div>;
  const Active = EVENT_TABS.find((t) => t.id === tab).C;
  const relAlerts = window.DATA.alerts.filter((a) => a.relatedEventId === id);

  return (
    <div className="page">
      <button className="btn btn-sm btn-ghost" style={{ marginBottom: 16 }} onClick={() => Store.nav("events")}><Icon.chevL style={{ width: 15, height: 15 }} /> Events</button>

      {/* Header */}
      <Card className="" bodyClass="card-pad" >
        <div style={{ display: "flex", gap: 8, marginBottom: 10, flexWrap: "wrap", alignItems: "center" }}>
          <RiskBadge level={e.riskLevel} label={e.riskLevel[0].toUpperCase() + e.riskLevel.slice(1) + " Risk"} />
          <span className="badge badge-neutral">{e.status}</span>
          {e.eventTypes.map((t) => <span key={t} className="eyebrow" style={{ color: "var(--ink-3)" }}>{t}</span>)}
        </div>
        <h1 style={{ fontSize: 24, lineHeight: 1.2, marginBottom: 12, textWrap: "balance", maxWidth: 800 }}>{e.title}</h1>
        <p style={{ fontSize: 14.5, color: "var(--ink-2)", lineHeight: 1.55, maxWidth: 820, margin: "0 0 18px", textWrap: "pretty" }}>{e.summary}</p>
        <div style={{ display: "flex", gap: 28, alignItems: "center", flexWrap: "wrap" }}>
          <ScoreStat label="Risk" value={e.riskScore} tip />
          <ScoreStat label="Hotness" value={e.hotnessScore} tip />
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 5 }}><span className="eyebrow" style={{ margin: 0 }}>Confidence</span></div>
            <div className="mono" style={{ fontSize: 19, fontWeight: 600, marginTop: 2 }}>{e.confidenceScore.toFixed(2)} <span style={{ fontSize: 12, color: "var(--ink-faint)" }}>{window.DATA.conf(e.confidenceScore)}</span></div>
          </div>
          <div style={{ borderLeft: "1px solid var(--line)", paddingLeft: 28, display: "flex", gap: 24, fontSize: 12, color: "var(--ink-3)" }}>
            <div><div className="eyebrow">Location</div><div style={{ marginTop: 3, display: "flex", alignItems: "center", gap: 5 }}><Icon.pin style={{ width: 13, height: 13 }} />{e.primaryLocation}</div></div>
            <div><div className="eyebrow">Sources</div><div className="mono" style={{ marginTop: 3 }}>{e.sourceCount} · {e.articleCount} articles</div></div>
            <div><div className="eyebrow">First Seen</div><div className="mono" style={{ marginTop: 3 }}>{timeAgo(e.firstSeenAt)}</div></div>
          </div>
          <div style={{ flex: 1 }} />
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-sm"><Icon.alerts style={{ width: 14, height: 14 }} /> Create Alert</button>
            <WatchBtn id={e.id} />
          </div>
        </div>
      </Card>

      <div className="grid" style={{ gridTemplateColumns: "1fr 300px", marginTop: 18, alignItems: "start" }}>
        <div>
          <div className="tabs" style={{ marginBottom: 18 }}>
            {EVENT_TABS.map((t) => {
              const Ic = t.icon; const cnt = t.count ? t.count(e) : null;
              return <div key={t.id} className={"tab" + (tab === t.id ? " active" : "")} onClick={() => setTab(t.id)}>
                <Ic style={{ width: 14, height: 14 }} />{t.label}{cnt != null && <span className="tab-count">{cnt}</span>}</div>;
            })}
          </div>
          <Active e={e} />
        </div>

        {/* Right sidebar */}
        <div className="stack" style={{ position: "sticky", top: 0 }}>
          {e.crisisRating ? (
            <SidebarRatingCard rating={e.crisisRating} />
          ) : (
            <Card icon={Icon.shield} title="Risk Score">
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8 }}>
                <Gauge value={e.riskScore} size={128} label="Risk" />
                <RiskBadge level={e.riskLevel} label={e.riskLevel + " risk"} />
              </div>
            </Card>
          )}
          <Card icon={Icon.doc} title="Source Summary">
            <div className="stack" style={{ gap: 10 }}>
              <Row k="Sources" v={e.sourceCount} />
              <Row k="Articles" v={e.articleCount} />
              <Row k="Confidence" v={window.DATA.conf(e.confidenceScore) + " · " + e.confidenceScore.toFixed(2)} />
              <Row k="Last updated" v={timeAgo(e.lastUpdatedAt)} />
            </div>
          </Card>
          <Card icon={Icon.alerts} title="Related Alerts" eyebrow={relAlerts.length + " active"}>
            {relAlerts.length === 0 ? <span className="muted" style={{ fontSize: 12.5 }}>No alerts for this event.</span> :
              <div className="stack" style={{ gap: 10 }}>
                {relAlerts.slice(0, 3).map((a) => (
                  <div key={a.id} onClick={() => Store.nav("alerts")} style={{ cursor: "pointer", paddingBottom: 9, borderBottom: "1px solid var(--line-soft)" }}>
                    <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 3 }}><RiskBadge level={a.severity} /><span className="mono" style={{ fontSize: 10, color: "var(--ink-faint)" }}>{timeAgo(a.createdAt)}</span></div>
                    <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.35 }}>{a.title}</div>
                  </div>
                ))}
              </div>}
          </Card>
        </div>
      </div>
    </div>
  );
}

function Row({ k, v }) {
  return <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, fontSize: 12.5 }}><span style={{ color: "var(--ink-3)", whiteSpace: "nowrap" }}>{k}</span><span className="mono" style={{ fontWeight: 600, whiteSpace: "nowrap", textAlign: "right" }}>{v}</span></div>;
}

window.EventDetail = EventDetail;
window.SourceBadge = SourceBadge;
window.Row = Row;
