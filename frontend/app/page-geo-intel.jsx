/* SIGNAL — geospatial intelligence demo */
const { useState, useEffect, useRef, useMemo, useCallback } = React;

const GEO_MAP_MODE_KEY = "SIGNAL_GEO_MAP_MODE";
const GEO_MAP_MODES = [
  { id: "earth", label: "Earth", icon: Icon.globe },
  { id: "borders", label: "Borders", icon: Icon.grid },
];

const GEO_ADDRESS_FIXTURES = [
  {
    id: "addr-commerce-dc",
    label: "U.S. Department of Commerce",
    mapLabel: "U.S. Dept. of Commerce",
    city: "Washington, D.C.",
    address: "1401 Constitution Ave NW, Washington, DC 20230",
    latitude: 38.8943,
    longitude: -77.0327,
    riskScore: 76,
    hotnessScore: 88,
    sourceType: "Federal policy node",
    eventIds: ["evt-semis", "evt-recession", "evt-merger"],
    sectors: ["Semiconductors", "AI Infrastructure", "Policy"],
    signal: "Advanced-chip export review is clustered with semiconductor repricing and supplier exposure.",
  },
  {
    id: "addr-tsmc-hsinchu",
    label: "TSMC Hsinchu Campus",
    city: "Hsinchu, Taiwan",
    address: "No. 8, Li-Hsin Rd. 6, Hsinchu Science Park, Hsinchu, Taiwan",
    latitude: 24.7816,
    longitude: 121.0188,
    riskScore: 61,
    hotnessScore: 88,
    sourceType: "Supply chain node",
    eventIds: ["evt-semis"],
    sectors: ["Semiconductors", "Cloud Computing"],
    signal: "Foundry exposure is indirect, but advanced-node demand and tooling access are monitored.",
  },
  {
    id: "addr-asml-veldhoven",
    label: "ASML Headquarters",
    city: "Veldhoven, Netherlands",
    address: "De Run 6501, 5504 DR Veldhoven, Netherlands",
    latitude: 51.4072,
    longitude: 5.3927,
    riskScore: 72,
    hotnessScore: 88,
    sourceType: "Equipment node",
    eventIds: ["evt-semis"],
    sectors: ["Semiconductor Equipment", "Semiconductors"],
    signal: "Equipment makers show the clearest direct exposure to licensing and shipment constraints.",
  },
  {
    id: "addr-ny-fed",
    label: "New York Financial District",
    city: "New York",
    address: "33 Liberty St, New York, NY 10045",
    latitude: 40.7082,
    longitude: -74.0086,
    riskScore: 71,
    hotnessScore: 74,
    sourceType: "Financial stress node",
    eventIds: ["evt-banking"],
    sectors: ["Banking", "Real Estate", "Insurance"],
    signal: "Regional-bank coverage velocity is rising ahead of acute market stress signals.",
  },
  {
    id: "addr-hormuz",
    label: "Strait of Hormuz",
    city: "Strait of Hormuz",
    address: "Strait of Hormuz shipping lane, Oman / Iran",
    latitude: 26.5667,
    longitude: 56.25,
    riskScore: 78,
    hotnessScore: 79,
    sourceType: "Geopolitical chokepoint",
    eventIds: ["evt-energy"],
    sectors: ["Energy", "Transportation", "Defense"],
    signal: "Shipping-lane escalation is feeding crude repricing and freight-cost expectations.",
  },
  {
    id: "addr-san-jose",
    label: "San Jose AI Infrastructure Cluster",
    city: "San Jose",
    address: "170 West Tasman Dr, San Jose, CA 95134",
    latitude: 37.4084,
    longitude: -121.9539,
    riskScore: 34,
    hotnessScore: 62,
    sourceType: "Opportunity node",
    eventIds: ["evt-aiinfra"],
    sectors: ["AI Infrastructure", "Cloud Computing"],
    signal: "Positive demand signals are offsetting some supply-side semiconductor headline risk.",
  },
];

function normGeoText(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function compactLocationId(value) {
  return String(value || "location").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function buildGeoLocations(D) {
  const mapLocations = (D.eventMap || []).map((p) => {
    const events = (p.relatedEventIds || []).map((id) => D.eventsById[id]).filter(Boolean);
    const sectors = [...new Set(events.flatMap((e) => e.affectedIndustries || []))].slice(0, 4);
    return {
      id: "map-" + compactLocationId(p.locationName),
      label: p.locationName,
      city: p.locationName,
      address: p.locationName,
      latitude: p.latitude,
      longitude: p.longitude,
      riskScore: p.maxRiskScore,
      hotnessScore: Math.max(...events.map((e) => e.hotnessScore || 0), 50),
      sourceType: p.dominantEventType + " cluster",
      eventIds: p.relatedEventIds || [],
      sectors,
      signal: events[0] ? events[0].whyItMatters : "Event cluster with active location evidence.",
    };
  });
  return [...GEO_ADDRESS_FIXTURES, ...mapLocations];
}

function resolveGeoAddress(query, locations) {
  const q = normGeoText(query);
  if (!q) return null;
  const tokens = q.split(" ").filter(Boolean);
  const ranked = locations.map((loc) => {
    const hay = normGeoText([loc.label, loc.city, loc.address, loc.sourceType, ...(loc.sectors || [])].join(" "));
    let score = hay.includes(q) ? 80 : 0;
    tokens.forEach((token) => {
      if (hay.includes(token)) score += token.length > 3 ? 10 : 4;
    });
    if (normGeoText(loc.address) === q || normGeoText(loc.city) === q) score += 120;
    return { loc, score };
  }).sort((a, b) => b.score - a.score);
  return ranked[0] && ranked[0].score >= Math.max(14, tokens.length * 5) ? ranked[0].loc : null;
}

function uniqueCompanies(events) {
  const out = new Map();
  events.forEach((event) => {
    (event.affectedCompanies || []).forEach((company) => {
      const key = company.ticker || company.name;
      if (!out.has(key)) out.set(key, company);
    });
  });
  return [...out.values()].slice(0, 7);
}

function GeoGlobePanel({ locations, selectedId, mapMode, onSelect }) {
  const hostRef = useRef(null);
  const runtimeRef = useRef(null);
  const selectRef = useRef(onSelect);
  const [missingRuntime, setMissingRuntime] = useState(false);

  useEffect(() => { selectRef.current = onSelect; }, [onSelect]);

  const mountGlobeHost = useCallback((node) => {
    hostRef.current = node;
    if (!node || runtimeRef.current) return;
    if (!window.SignalGlobe) {
      setMissingRuntime(true);
      return;
    }
    setMissingRuntime(false);
    runtimeRef.current = window.SignalGlobe.mount(node, {
      locations,
      selectedId,
      mapMode,
      onSelect: (id) => selectRef.current && selectRef.current(id),
    });
  }, [locations, selectedId, mapMode]);

  useEffect(() => {
    return () => {
      if (runtimeRef.current) runtimeRef.current.dispose();
      runtimeRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (runtimeRef.current) runtimeRef.current.setLocations(locations, selectedId);
  }, [locations, selectedId]);

  useEffect(() => {
    if (runtimeRef.current) runtimeRef.current.setMapMode(mapMode);
  }, [mapMode]);

  return (
    <div className="geo-globe-host" ref={mountGlobeHost}>
      {missingRuntime && (
        <div className="geo-map-fallback">
          <div className="eyebrow">Cesium runtime unavailable</div>
          <div className="geo-map-fallback-title">Map engine did not initialize</div>
          <p>Serve this page over http and allow the CesiumJS and local runtime scripts to load.</p>
        </div>
      )}
    </div>
  );
}

function GeoLocationRow({ loc, selected, onSelect }) {
  const level = scoreLevel(loc.riskScore || 0);
  return (
    <button className={"geo-location-row" + (selected ? " selected" : "")} onClick={() => onSelect(loc.id)}>
      <span className="geo-row-pin" style={{ background: levelColor(level) }} />
      <span style={{ minWidth: 0, flex: 1 }}>
        <span className="geo-row-title">{loc.label}</span>
        <span className="geo-row-sub">{loc.city} · {loc.sourceType}</span>
      </span>
      <span className="mono geo-row-score" style={{ color: levelColor(level) }}>{loc.riskScore}</span>
    </button>
  );
}

function GeoIntelPage() {
  const D = window.DATA;
  const locations = useMemo(() => buildGeoLocations(D), [D]);
  const [selectedId, setSelectedId] = useState("addr-commerce-dc");
  const selected = locations.find((loc) => loc.id === selectedId) || locations[0];
  const [query, setQuery] = useState(selected.address);
  const [error, setError] = useState("");
  const [ionToken, setIonToken] = useState(() => window.localStorage.getItem("SIGNAL_CESIUM_ION_TOKEN") || "");
  const [mapMode, setMapMode] = useState(() => (
    window.localStorage.getItem(GEO_MAP_MODE_KEY) === "borders" ? "borders" : "earth"
  ));

  const relatedEvents = (selected.eventIds || []).map((id) => D.eventsById[id]).filter(Boolean);
  const exposedCompanies = uniqueCompanies(relatedEvents);
  const selectedLevel = scoreLevel(selected.riskScore || 0);

  function selectLocation(id) {
    const next = locations.find((loc) => loc.id === id);
    if (!next) return;
    setSelectedId(next.id);
    setQuery(next.address);
    setError("");
  }

  function handleResolve(event) {
    event.preventDefault();
    const result = resolveGeoAddress(query, locations);
    if (!result) {
      setError("No demo geocode match for that address.");
      return;
    }
    selectLocation(result.id);
    Store.flash("Address resolved to intelligence location");
  }

  function saveIonToken() {
    const token = ionToken.trim();
    if (token) window.localStorage.setItem("SIGNAL_CESIUM_ION_TOKEN", token);
    else window.localStorage.removeItem("SIGNAL_CESIUM_ION_TOKEN");
    Store.flash(token ? "Cesium ion token saved" : "Cesium ion token removed");
    window.setTimeout(() => window.location.reload(), 500);
  }

  function chooseMapMode(nextMode) {
    setMapMode(nextMode);
    window.localStorage.setItem(GEO_MAP_MODE_KEY, nextMode);
  }

  return (
    <div className="page geo-page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Icon.globe style={{ width: 14, height: 14, color: "var(--accent)" }} /> Geospatial Intelligence
          </div>
          <h1 className="page-title">Address-to-event globe</h1>
          <div className="page-sub">Geocoded locations linked to event clusters, risk posture, sectors, and exposed entities.</div>
          <PageStatus blocks={["eventMap", "events"]} />
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-sm" onClick={() => selectLocation("addr-commerce-dc")}><Icon.target /> Center Policy Node</button>
          <button className="btn btn-sm btn-ghost" onClick={() => Store.nav("events")}><Icon.events /> Events</button>
        </div>
      </div>

      <div className="geo-workspace">
        <section className="geo-stage">
          <div className="geo-stage-top">
            <div>
              <div className="eyebrow">Focused Location</div>
              <div className="geo-stage-title">{selected.label}</div>
            </div>
            <div className="geo-stage-meta">
              <RiskBadge level={selectedLevel} label={selectedLevel + " risk"} />
              <span className="chip"><Icon.pin style={{ width: 13, height: 13 }} /> {selected.latitude.toFixed(3)}, {selected.longitude.toFixed(3)}</span>
              <div className="geo-mode-switch" aria-label="Map mode">
                {GEO_MAP_MODES.map((mode) => {
                  const ModeIcon = mode.icon;
                  return (
                    <button
                      key={mode.id}
                      type="button"
                      className={mapMode === mode.id ? "active" : ""}
                      aria-pressed={mapMode === mode.id}
                      onClick={() => chooseMapMode(mode.id)}
                    >
                      <ModeIcon style={{ width: 13, height: 13 }} />
                      <span>{mode.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
          <GeoGlobePanel locations={locations} selectedId={selected.id} mapMode={mapMode} onSelect={selectLocation} />
          <div className="geo-stage-bottom">
            <span className="chip"><Icon.layers style={{ width: 13, height: 13 }} /> {locations.length} geo signals</span>
            <span className="chip"><Icon.activity style={{ width: 13, height: 13 }} /> {relatedEvents.length} linked events</span>
            <span className="chip"><Icon.companies style={{ width: 13, height: 13 }} /> {exposedCompanies.length} exposed entities</span>
          </div>
        </section>

        <aside className="geo-side">
          <Card icon={Icon.search} title="Address Resolver" eyebrow="Demo geocoder">
            <form className="geo-resolver" onSubmit={handleResolve}>
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="1401 Constitution Ave NW, Washington, DC" />
              <button className="btn btn-primary" type="submit"><Icon.pin /> Resolve</button>
            </form>
            {error && <div className="geo-error">{error}</div>}
            <div className="geo-samples">
              {GEO_ADDRESS_FIXTURES.slice(0, 5).map((loc) => (
                <button key={loc.id} className="chip chip-x" onClick={() => selectLocation(loc.id)}>{loc.city}</button>
              ))}
            </div>
            <div className="geo-token-box">
              <div>
                <div className="eyebrow">Cesium Detail Mode</div>
                <p>Paste a Cesium ion token to enable world terrain and OSM buildings where available.</p>
              </div>
              <div className="geo-token-row">
                <input value={ionToken} onChange={(e) => setIonToken(e.target.value)} placeholder="Cesium ion access token" type="password" />
                <button className="btn btn-sm" type="button" onClick={saveIonToken}>{ionToken.trim() ? "Save" : "Clear"}</button>
              </div>
            </div>
          </Card>

          <Card icon={Icon.bolt} title="Location Brief" eyebrow={selected.sourceType}>
            <div className="geo-brief-score">
              <Gauge value={selected.riskScore} size={116} label="Risk" />
              <div>
                <div className="eyebrow">Signal</div>
                <p>{selected.signal}</p>
              </div>
            </div>
            <div className="geo-sector-list">
              {(selected.sectors || []).map((sector) => <span className="chip" key={sector}>{sector}</span>)}
            </div>
          </Card>
        </aside>
      </div>

      <div className="geo-bottom-grid">
        <Card icon={Icon.events} title="Linked Event Clusters" eyebrow="Location evidence">
          <div className="stack" style={{ gap: 10 }}>
            {relatedEvents.map((event) => (
              <div className="geo-event-item" key={event.id} onClick={() => Store.nav("event", event.id)}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div className="geo-event-title">{event.title}</div>
                  <div className="geo-event-sub">{event.eventType} · {event.sourceCount} sources · {timeAgo(event.lastUpdatedAt)}</div>
                </div>
                <ScoreStat label="Risk" value={event.riskScore} compact />
                <Icon.chevR style={{ width: 15, height: 15, color: "var(--ink-faint)" }} />
              </div>
            ))}
          </div>
        </Card>

        <Card icon={Icon.companies} title="Entity Exposure" eyebrow="Companies routed from the selected location">
          <div className="geo-company-grid">
            {exposedCompanies.map((company) => {
              const meta = dirMeta(company.impactDirection);
              return (
                <div className="geo-company" key={company.ticker || company.name}>
                  <CompanyLogo company={company} ticker={company.ticker || company.name} name={company.name} size={30} />
                  <div style={{ minWidth: 0 }}>
                    <div className="geo-company-name">{company.name}</div>
                    <div className="ticker">{company.ticker || "—"} · {meta.label}</div>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        <Card icon={Icon.db} title="Production Contract" eyebrow="Backend handoff">
          <div className="geo-contract">
            <div><span>1</span><b>POST /api/geocode</b><em>address → lat/lng + confidence</em></div>
            <div><span>2</span><b>geo join</b><em>nearest event clusters, entities, sectors</em></div>
            <div><span>3</span><b>risk route</b><em>location-aware score, evidence, alerts</em></div>
          </div>
        </Card>
      </div>

      <Card icon={Icon.globe} title="Monitored Geo Signals" eyebrow="Click a row to focus the globe" className="geo-table-card">
        <div className="geo-location-list">
          {locations.map((loc) => (
            <GeoLocationRow key={loc.id} loc={loc} selected={loc.id === selected.id} onSelect={selectLocation} />
          ))}
        </div>
      </Card>
    </div>
  );
}

window.GeoIntelPage = GeoIntelPage;
