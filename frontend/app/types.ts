/* ============================================================================
   SIGNAL — data schema
   TypeScript type definitions for the mock dataset. Mirrors the building plan.
   The runtime fixtures live in /data/*.json and are loaded by app/data.js.
   This file is documentation/reference (the prototype runs on plain JS), but it
   is the canonical contract a real API layer would satisfy.
   ========================================================================== */

export type RiskLevel = "low" | "medium" | "high" | "critical";
export type ImpactDirection = "positive" | "negative" | "mixed" | "unknown";
export type ChangeDirection = "up" | "down" | "flat";

/* Canonical source_type — single source of truth is docs/contracts/canonical-schema.md.
   Both EvidenceSource.sourceType and SourceStatus.sourceType reference this alias.
   Legacy frontend values map in per the contract's reconciliation table:
   article → rss (MVP default), official_statement → official, macro_data → macro_indicator. */
export type SourceType =
  | "rss"
  | "news_api"
  | "official"
  | "filing"
  | "macro_indicator"
  | "market_data"
  | "banking_data"
  | "historical_case";

/* Canonical horizon wire tokens — see docs/contracts/canonical-schema.md. */
export type Horizon = "0_6m" | "6_12m" | "12_18m" | "within_18m";

export type CrisisRating = {
  target_type: string;
  target_id: string;
  risk_type: string;
  as_of_date: string;
  probability_0_6m: number;
  probability_6_12m: number;
  probability_12_18m: number;
  probability_within_18m: number;
  risk_score: number;
  risk_level: RiskLevel;
  confidence_score: number;
  model_versions: Record<string, string>;
  top_drivers: {
    component?: string;
    signal?: string;
    name?: string;
    score?: number;
    contribution?: number;
    similarity_score?: number;
    evidence_refs?: { source_type: string; source_id: string }[];
  }[];
  evidence_refs: { source_type: string; source_id: string }[];
  what_could_escalate: string[];
  what_could_reduce_risk: string[];
};

/* ---- Dashboard ----------------------------------------------------------- */
export type DailySummary = {
  date: string;
  lastUpdatedAt: string;
  summary: string;
  keyPoints: string[];
  overallRiskLevel: RiskLevel;
  confidenceScore: number;
  modelRating?: CrisisRating;
};

export type DashboardMetric = {
  label: string;
  value: number | string;
  previousValue?: number | string;
  change?: number;
  changeDirection?: ChangeDirection;
  severity?: RiskLevel;
};

export type EventMapPoint = {
  locationName: string;
  latitude: number;
  longitude: number;
  x: number;          // normalized 0..1 position on the abstract map
  y: number;
  eventCount: number;
  maxRiskScore: number;
  dominantEventType: string;
  relatedEventIds: string[];
};

export type UpcomingTrigger = {
  id: string;
  title: string;
  expectedAt?: string;
  relatedEventId?: string;
  relatedCompanyId?: string;
  importance: RiskLevel;
  reason: string;
};

/* ---- Risk ---------------------------------------------------------------- */
export type RiskRadarItem = {
  riskType: string;
  score: number;
  level: RiskLevel;
  change24h: number;
  change7d: number;
  topDriver: string;
  confidenceScore: number;
};

export type RiskSignal = {
  name: string;
  value: string | number;
  status: "positive" | "neutral" | "watch" | "warning" | "critical";
  explanation: string;
  source: string;
  lastUpdatedAt: string;
};

export type RiskDetail = {
  riskType: string;
  score: number;
  severity: RiskLevel;
  modelRating?: CrisisRating;
  probabilityByHorizon: { horizon: Horizon; probability: number }[];
  mainDrivers: string[];
  signals: RiskSignal[];
  historicalComparisons: string[];
  leadingIndicators: string[];
  invalidationSignals: string[];
  relatedEventIds: string[];
  relatedIndustryIds: string[];
  relatedCompanyIds: string[];
};

/* ---- Industries & Companies ---------------------------------------------- */
export type IndustryHeatmapItem = {
  industryId: string;
  industryName: string;
  impactScore: number;
  riskScore: number;
  opportunityScore: number;
  relatedEventCount: number;
  direction: ImpactDirection;
  newsVelocityScore: number;   // extension used by the heatmap & industry pages
  summary: string;
};

export type CompanyImpactRow = {
  companyId: string;
  name: string;
  ticker?: string;
  cik?: string;
  secCik?: string;
  primaryCik?: string;
  exchange?: string;
  industry: string;
  country?: string;
  impactDirection: ImpactDirection;
  impactScore: number;
  riskScore: number;
  relatedEventCount: number;
  topDriver: string;
  lastUpdatedAt: string;
};

export type CompanyResearchChecklistField = {
  key: string;
  category: "financial" | "business" | "industry" | "valuation";
  label: string;
  value: string;
  available: boolean;
  source?: string | null;
  as_of?: string | null;
  confidence?: number;
  evidence_refs?: { source_type?: string | null; source_id?: string | null }[];
  stale?: boolean;
  stale_reason?: string | null;
  freshness_as_of?: string | null;
  conflict?: boolean;
  conflict_reason?: string | null;
  provider_values?: Record<string, unknown>[];
  reason?: string | null;
};

export type CompanyResearchProfile = {
  asOf: string;
  lastUpdatedAt?: string;
  identity?: {
    name?: string;
    ticker?: string | null;
    cik?: string | null;
    exchange?: string | null;
    fiscal_year_end?: string | null;
    sic?: string | null;
    sic_description?: string | null;
  };
  coverage?: {
    available: number;
    total: number;
    missing: number;
    coverage_pct: number;
    by_category?: Record<string, { available: number; total: number; missing: number }>;
  };
  financial: {
    headline: string;
    metrics: { label: string; value: string; tone?: "positive" | "warning" | "negative" | "neutral" }[];
    notes: string[];
  };
  business: {
    description: string;
    segments: { label: string; value: string; detail: string }[];
    advantages: string[];
    watchItems: string[];
  };
  industry: {
    position: string;
    growthDrivers: string[];
    risks: string[];
    outlook: string;
  };
  valuation: {
    view: string;
    price: string;
    marketCap: string;
    metrics: { label: string; value: string; tone?: "positive" | "warning" | "negative" | "neutral" }[];
    interpretation: string;
  };
  researchChecklist?: {
    financial: CompanyResearchChecklistField[];
    business: CompanyResearchChecklistField[];
    industry: CompanyResearchChecklistField[];
    valuation: CompanyResearchChecklistField[];
  };
  missingFields?: { category: string; key: string; label: string; reason?: string | null }[];
  staleFields?: { category: string; key: string; label: string; as_of?: string | null; reason?: string | null }[];
  evidenceRefs?: { source_type?: string | null; source_id?: string | null }[];
};

/* ---- Evidence ------------------------------------------------------------ */
export type EvidenceSource = {
  id: string;
  sourceType: SourceType;
  title: string;
  publisher?: string;
  url?: string;
  publishedAt?: string;
  credibilityScore?: number;
  relatedClaims: string[];
};

/* ---- Event Intelligence -------------------------------------------------- */
export type EventOverview = {
  whatHappened: string;
  whyItMatters: string;
  keyActors: string[];
  affectedIndustries: string[];
  affectedCompanies: string[];
  shortTermImplications: string[];
  mediumTermImplications: string[];
  longTermImplications: string[];
  keyUncertainties: string[];
};

export type EventTimelineItem = {
  id: string;
  timestamp: string;
  title: string;
  description: string;
  sourceIds: string[];
  importance: "low" | "medium" | "high";
};

export type RelatedIndustry = {
  industryId: string;
  name: string;
  impactDirection: ImpactDirection;
  impactScore: number;
  riskScore: number;
  opportunityScore: number;
  impactPathway: string;
  relatedCompanies: { companyId: string; name: string; ticker?: string }[];
  historicalSensitivity?: string;
  keyIndicators: string[];
  confidenceScore: number;
};

export type RelatedCompany = {
  companyId: string;
  name: string;
  ticker?: string;
  exchange?: string;
  industry: string;
  impactDirection: ImpactDirection;
  impactScore: number;
  riskScore: number;
  exposureExplanation: string;
  evidenceSourceIds: string[];
  relatedHistoricalCaseIds: string[];
  monitoringIndicators: string[];
  confidenceScore: number;
};

export type HistoricalAnalogy = {
  historicalEventId: string;
  title: string;
  startDate?: string;
  endDate?: string;
  similarityScore: number;
  similarities: string[];
  differences: string[];
  historicalOutcome: string;
  lessonsForCurrentEvent: string[];
  analogyLimitations: string[];
  sourceIds: string[];
};

export type ForecastScenario = {
  id: string;
  name: "base_case" | "upside_case" | "downside_case" | "tail_risk_case";
  probability: number;
  timeHorizon: Horizon;
  narrative: string;
  triggers: string[];
  leadingIndicators: string[];
  expectedImpact: {
    industries: string[];
    companies: string[];
    macro?: string[];
    market?: string[];
  };
  invalidationSignals: string[];
  confidenceScore: number;
  lastUpdatedAt: string;
};

export type ModelDebate = {
  consensusPoints: string[];
  disagreementPoints: string[];
  criticNotes: string[];
  modelVotes: {
    modelName: string;
    riskLevel: RiskLevel;
    probabilityEstimate?: number;
    summary: string;
  }[];
};

export type ClaimMapEntry = { claim: string; evidenceIds: string[] };

export type EventCard = {
  id: string;
  title: string;
  summary: string;
  eventType: string;
  eventTypes: string[];
  status: "new" | "developing" | "updated" | "resolved";
  riskLevel: RiskLevel;
  hotnessScore: number;
  riskScore: number;
  confidenceScore: number;
  affectedIndustries: string[];
  affectedCompanies: { name: string; ticker?: string; impactDirection: ImpactDirection }[];
  locations: string[];
  primaryLocation?: string;
  sourceCount: number;
  articleCount: number;
  firstSeenAt: string;
  lastUpdatedAt: string;
  whyItMatters: string;
  /* deep-analysis tabs (present on fully-authored events) */
  overview?: EventOverview;
  timeline?: EventTimelineItem[];
  relatedIndustries?: RelatedIndustry[];
  relatedCompanies?: RelatedCompany[];
  historicalAnalogies?: HistoricalAnalogy[];
  forecastScenarios?: ForecastScenario[];
  evidenceSourceIds?: string[];
  claimMap?: ClaimMapEntry[];
  modelDebate?: ModelDebate;
  crisisRating?: CrisisRating;
};

/* ---- Workspace ----------------------------------------------------------- */
export type Alert = {
  id: string;
  title: string;
  message: string;
  severity: RiskLevel;
  riskScore: number;
  alertType: string;
  relatedEventId?: string;
  relatedCompanyId?: string;
  relatedIndustryId?: string;
  evidenceSourceIds: string[];
  status: "new" | "acknowledged" | "resolved";
  createdAt: string;
};

export type WatchlistItem = {
  id: string;
  itemType: "company" | "industry" | "country" | "risk" | "keyword" | "person" | "organization";
  label: string;
  metadata?: Record<string, unknown>;
  alertEnabled: boolean;
  createdAt: string;
};

/* ---- Admin / Pipeline ---------------------------------------------------- */
export type PipelineJob = {
  id: string;
  jobType: string;
  status: "queued" | "running" | "succeeded" | "failed" | "partially_failed";
  startedAt?: string | null;
  finishedAt?: string | null;
  durationMs?: number | null;
  errorMessage?: string | null;
  throughput?: string;
};

export type SourceStatus = {
  sourceId: string;
  name: string;
  sourceType: SourceType;
  status: "healthy" | "degraded" | "down";
  lastFetchedAt?: string;
  articlesFetchedToday?: number;
  errorRate?: number;
  averageLatencyMs?: number;
};

export type ModelUsageStats = {
  provider: string;
  modelName: string;
  taskType: string;
  requestCount: number;
  successRate: number;
  averageLatencyMs: number;
  averageCostUsd: number;
  schemaValidityRate: number;
  fallbackCount: number;
};

/* ---- Root document (data/signal-mock-data.json + data/events.json) ------- */
export type SignalDataset = {
  meta: { now: string };
  dashboard: {
    dailySummary: DailySummary;
    metrics: DashboardMetric[];
    upcomingTriggers: UpcomingTrigger[];
    eventMap: EventMapPoint[];
  };
  risk: {
    riskRadar: RiskRadarItem[];
    riskDetails: Record<string, RiskDetail>;
  };
  industries: IndustryHeatmapItem[];
  companies: CompanyImpactRow[];
  evidence: EvidenceSource[];
  events: EventCard[];
  alerts: Alert[];
  watchlist: WatchlistItem[];
  admin: { jobs: PipelineJob[]; sources: SourceStatus[]; models: ModelUsageStats[] };
  ask: { suggestions: string[] };
};

/* ---- Adapter runtime contract (frontend/app/api-adapter.js) --------------
   These types describe what the api-adapter attaches on top of the fixture
   schema above; they document the adapter output for item 6B (loader) and
   item 7 (badges/banner). They add metadata only — the existing product shapes
   are unchanged. Every block above is additionally tagged at runtime with a
   NON-enumerable `dataQuality` marker (so arrays/maps keep their behavior and
   key iteration); the same information is mirrored in the `blockQuality`
   registry, which pages read for primitives (NOW/conf are `derived`). */

export type DataQuality = "live" | "partial" | "synthetic" | "empty";

/** A preserved backend request failure (adapter-contract error envelope). */
export type AdapterApiError = {
  endpoint: string;
  code: string;                 // body error.code, else http_<status> / network_error / truncated
  message: string;
  requestId: string | null;     // body error.request_id, else X-Request-ID header
};

/** One entry in the blockQuality registry. Content blocks carry `quality`;
    NOW/conf are `derived`; failed/truncated blocks carry the extra detail. */
export type AdapterBlockQuality = {
  quality?: DataQuality;
  kind?: "derived";
  error?: AdapterApiError;
  truncated?: boolean;
  total?: number;
  shown?: number;
};

/** Snapshot-level runtime metadata. `mode` is "api" when the backend was
    reachable (block-by-block merge) or "demo" for the full fixture fallback. */
export type AdapterRuntime = {
  mode: "api" | "demo";
  apiBase: string;
  now: string;
  degraded: boolean;
  reason?: string;
  truncated?: { endpoint: string; total: number; shown: number }[];
};

/** The flattened, page-facing `window.DATA` the adapter assembles. Preserves
    the retired data.js field surface exactly and appends adapter metadata. */
export type SignalWindowData = {
  NOW: string;
  conf: (score: number) => "High" | "Medium" | "Low";
  dailySummary: DailySummary | null;
  metrics: DashboardMetric[];
  upcomingTriggers: UpcomingTrigger[];
  eventMap: EventMapPoint[];
  riskRadar: RiskRadarItem[];
  riskDetails: Record<string, RiskDetail>;
  industries: IndustryHeatmapItem[];
  companies: CompanyImpactRow[];
  companyProfiles: Record<string, CompanyResearchProfile>;
  evidence: EvidenceSource[];
  evidenceById: Record<string, EvidenceSource>;
  events: EventCard[];
  eventsById: Record<string, EventCard>;
  alerts: Alert[];
  watchlist: WatchlistItem[];
  adminJobs: PipelineJob[];
  adminSources: SourceStatus[];
  adminModels: ModelUsageStats[];
  askSuggestions: string[];
  riskTrends: Record<string, number[]>;
  /* adapter metadata (item 7) */
  blockQuality: Record<string, AdapterBlockQuality>;
  runtime: AdapterRuntime;
  apiErrors: AdapterApiError[];
};
