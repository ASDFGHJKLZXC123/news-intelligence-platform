/* SIGNAL — personal Today, Saved, Briefs, and exact report/source inspection. */
const { useState, useEffect, useRef, useMemo } = React;

const PERSONAL_PAGE_SIZE = 12;
const PERSONAL_SOURCE_PAGE_SIZE = 10;
const PERSONAL_TERMINAL_STATES = new Set(["succeeded", "partially_failed", "failed"]);
const PersonalApi = SignalApiAdapter.createPersonalApi({
  apiBase: window.SIGNAL_API_BASE,
  apiKey: window.SIGNAL_API_KEY || "",
});
window.SignalPersonalApi = PersonalApi;
window.PersonalUiCache = window.PersonalUiCache || { events: [] };

function personalIsDemo() {
  return !!(window.DATA && window.DATA.runtime && window.DATA.runtime.mode === "demo");
}

function personalErrorText(error) {
  if (!error) return "The request failed.";
  const id = error.requestId ? " Request ID: " + error.requestId + "." : "";
  return (error.message || "The request failed.") + id;
}

function personalDateTime(value, timezone) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try {
    return date.toLocaleString("en-US", {
      timeZone: timezone || undefined,
      month: "short", day: "numeric", year: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      timeZoneName: "short",
    });
  } catch (e) {
    return date.toLocaleString();
  }
}

function personalLabel(value) {
  return String(value || "")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .replace(/^./, (letter) => letter.toUpperCase());
}

function personalValue(value) {
  if (value === null || value === undefined || value === "") return "Not recorded";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.length ? value.map((item) => typeof item === "object" ? (item.code ? item.code + (item.sourceId ? " (" + item.sourceId + ")" : "") : JSON.stringify(item)) : String(item)).join(", ") : "None";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function personalTodayReadError(results) {
  for (const index of [1, 0, 2]) {
    if (results[index] && results[index].status === "rejected") return results[index].reason;
  }
  return null;
}

function PersonalError({ error, stale }) {
  if (!error) return null;
  return (
    <div className="personal-inline-error" role="alert">
      <Icon.warn aria-hidden="true" />
      <span>{stale ? "The last confirmed data remains on screen. " : ""}{personalErrorText(error)}</span>
    </div>
  );
}

function PersonalPager({ offset, limit, total, onChange, disabled }) {
  if (total <= limit && offset === 0) return null;
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + limit, total);
  return (
    <div className="personal-pager" aria-label="Results pages">
      <span>{start}–{end} of {total}</span>
      <button className="btn btn-sm" type="button" disabled={disabled || offset === 0}
        onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button>
      <button className="btn btn-sm" type="button" disabled={disabled || offset + limit >= total}
        onClick={() => onChange(offset + limit)}>Next</button>
    </div>
  );
}

function LocalAccessKey() {
  const [value, setValue] = useState("");
  const [configured, setConfigured] = useState(PersonalApi.hasApiKey());
  const apply = (event) => {
    const next = event.target.value;
    setValue(next);
    PersonalApi.setApiKey(next);
    setConfigured(!!next.trim());
  };
  const clear = () => {
    setValue("");
    PersonalApi.setApiKey("");
    setConfigured(false);
  };
  return (
    <details className="personal-access">
      <summary>Local access{configured ? " · key set for this tab" : ""}</summary>
      <div className="personal-access-body">
        <label>
          <span>API access key</span>
          <input type="password" value={value} autoComplete="off" onChange={apply}
            placeholder={configured ? "A key is already set for this tab" : "Leave blank for local mode"} />
        </label>
        {configured && <button className="btn btn-sm" type="button" onClick={clear}>Clear key</button>}
        <p>The value stays only in this browser tab and is sent only with protected actions. Provider credentials never belong here.</p>
      </div>
    </details>
  );
}

function RunStateBadge({ run, stale }) {
  if (!run) return null;
  const state = run.state || "unknown";
  const cls = state === "succeeded" ? "badge-low" : state === "failed" ? "badge-high" : state === "partially_failed" ? "badge-med" : "badge-neutral";
  return <span className={"badge " + cls}>{personalLabel(state)}{stale ? " · status stale" : ""}</span>;
}

function personalCoverageRows(run, timezone) {
  if (!run) return [];
  const coverage = run.coverage || {};
  const rows = [];
  const has = (key) => Object.prototype.hasOwnProperty.call(coverage, key);
  if (run.captureStartedAt) rows.push(["Capture started", personalDateTime(run.captureStartedAt, timezone)]);
  if (run.captureEndedAt) rows.push(["Capture ended", personalDateTime(run.captureEndedAt, timezone)]);
  [
    ["feedsConfigured", "Feeds configured"],
    ["feedsSucceeded", "Feeds succeeded"],
    ["feedsFailed", "Feeds failed"],
    ["feedsPaused", "Feeds paused"],
    ["articlesAdmitted", "Articles admitted"],
  ].forEach(([key, label]) => { if (has(key)) rows.push([label, personalValue(coverage[key])]); });
  if (has("pendingTotal") || has("pendingCapacity")) {
    const pending = has("pendingTotal") ? personalValue(coverage.pendingTotal) : "Not recorded";
    const capacity = has("pendingCapacity") ? " of " + personalValue(coverage.pendingCapacity) + " capacity" : "";
    rows.push(["Pending items", pending + capacity]);
  }
  return rows;
}

function CoverageSummary({ run, timezone }) {
  if (!run) return null;
  const rows = personalCoverageRows(run, timezone);
  return (
    <div className="personal-coverage">
      {rows.length ? rows.map(([label, value]) => <Row key={label} k={label} v={value} />)
        : <span className="muted">Capture coverage has not been recorded.</span>}
      {run.counts && <><Row k="Selected for enrichment" v={run.counts.selectedForEnrichment} /><Row k="Grouped articles" v={run.counts.grouped} /></>}
      {Object.entries(run.stageResults || {}).map(([name, item]) => <Row key={name} k={personalLabel(name)} v={personalLabel(item.status) + (item.reason ? " · " + personalLabel(item.reason) : "")} />)}
    </div>
  );
}

function WorkspaceSetup({ model, onChanged, showReadiness = true }) {
  const [pending, setPending] = useState("");
  const [error, setError] = useState(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  if (!model) return null;

  const mutate = async (kind, work) => {
    if (pending || personalIsDemo()) return;
    setPending(kind);
    setError(null);
    try {
      await work();
      if (alive.current && !personalIsDemo()) await onChanged();
    } catch (err) {
      if (alive.current && !personalIsDemo()) setError(err);
    } finally {
      if (alive.current && !personalIsDemo()) setPending("");
    }
  };

  if (model.setupRequired || !model.workspace) {
    return (
      <Card title="Set up this local workspace" icon={Icon.settings}>
        <p className="personal-copy">Initialize local storage. Setup creates a reserved local owner when none exists, reuses the sole existing owner, or asks you to choose when ownership is ambiguous. Feeds, topics, processing routes, and spending allowances still require explicit configuration.</p>
        <div className="personal-actions">
          <button className="btn btn-primary btn-sm" type="button" disabled={!!pending}
            onClick={() => mutate("setup", () => PersonalApi.setupWorkspace())}>
            {pending === "setup" ? "Initializing…" : "Initialize workspace"}
          </button>
        </div>
        <PersonalError error={error} />
        <LocalAccessKey />
      </Card>
    );
  }

  if (model.workspace.ownerSelectionRequired) {
    const choices = model.ownerChoices || [];
    return (
      <Card title="Choose the local owner" icon={Icon.admin} eyebrow="Required before saving or updating">
        <p className="personal-copy">Choose explicitly. Saved-item counts are context only and never select an owner automatically.</p>
        {choices.length ? <div className="personal-owner-list">
          {choices.map((choice) => (
            <div className="personal-owner" key={choice.id}>
              <div><strong>{choice.label || choice.id}</strong><small>{choice.legacySavedCount || 0} legacy saved items</small></div>
              <button className="btn btn-sm" type="button" disabled={!!pending}
                onClick={() => mutate(choice.id, () => PersonalApi.bindOwner(choice.id))}>
                {pending === choice.id ? "Binding…" : "Use this owner"}
              </button>
            </div>
          ))}
        </div> : <p className="muted">No existing owner choices were returned. The local setup needs server-side attention.</p>}
        <PersonalError error={error} />
        <LocalAccessKey />
      </Card>
    );
  }

  if (showReadiness && !(model.actions && model.actions.canStart)) {
    const reasonLabels = {
      owner_selection_required: "Choose the local owner.", profile_missing: "The initial profile is missing.",
      feeds_not_configured: "No feeds are selected.", model_route_not_configured: "No compatible processing route is configured.",
      live_spending_not_authorized: "Live processing has no authorized allowance.", legacy_writer_active: "The legacy writer is active.",
      execution_route_unavailable: "The configured processing route is unavailable on this server.",
      personal_writer_inactive: "Personal processing mode is not active.",
    };
    const reasons = model.readiness && model.readiness.reasons || [];
    return (
      <Card title="Initial news configuration is incomplete" icon={Icon.info}>
        <p className="personal-copy">Choose feeds and collection limits in Settings. Retained RSS articles remain readable when optional AI processing is disabled or unavailable.</p>
        <button className="btn btn-sm" type="button" onClick={() => Store.nav("settings")}>Open settings</button>
        {reasons.length > 0 && <ul className="personal-reason-list">{reasons.map((reason) => <li key={reason}>{reasonLabels[reason] || personalLabel(reason)}</li>)}</ul>}
        <LocalAccessKey />
      </Card>
    );
  }
  return null;
}

function DemoStoryCard({ story }) {
  return (
    <article className="card card-pad personal-story" onClick={() => Store.nav("event", story.id)} tabIndex={0}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") Store.nav("event", story.id); }}>
      <div className="eyebrow">Sample grouped story</div>
      <h2>{story.title}</h2>
      <p>{story.summary || "No summary is available."}</p>
      <div className="personal-story-meta"><span>{story.sourceCount || 0} sources</span><span>{story.articleCount || 0} articles</span>{story.lastUpdatedAt && <span>Sample timestamp {fmtTime(story.lastUpdatedAt)}</span>}</div>
    </article>
  );
}

function DemoTodayPage() {
  const data = window.DATA;
  const refresh = () => window.SignalDataController && window.SignalDataController.refresh();
  return (
    <div className="page personal-page">
      <div className="page-head">
        <div className="titles"><div className="eyebrow">Personal news research desk</div><h1 className="page-title">Today</h1><div className="page-sub">Read-only sample with authored timestamps.</div></div>
        <button className="btn btn-sm" type="button" onClick={refresh} disabled={data.runtime && data.runtime.loading}><Icon.refresh /> Refresh sample</button>
      </div>
      {data.events.length ? <div className="grid personal-story-grid">{data.events.map((story) => <DemoStoryCard key={story.id} story={story} />)}</div>
        : <Card><EmptyState icon={Icon.events} title="No sample stories" hint="The read-only demonstration contains no stories." /></Card>}
      <Card className="personal-demo-action"><EmptyState icon={Icon.bolt} title="Updates are disabled in the read-only demo" hint="Switch to Real to read stored personal results or request an update." /></Card>
    </div>
  );
}

function PersonalStoryCard({ story, runId, pending, canSave, onToggle }) {
  const open = () => Store.nav("event", story.id, runId ? { runId } : undefined);
  return (
    <article className="card card-pad personal-story" onClick={open} tabIndex={0}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") open(); }}>
      <div className="personal-card-row">
        <div className="personal-story-body">
          <div className="personal-card-labels"><span className="eyebrow">Grouped story</span>{story.status === "incomplete" && <span className="badge badge-med">Incomplete</span>}</div>
          <h2>{story.title || "Untitled story"}</h2>
          <p>{story.summary || "No summary is available."}</p>
          <div className="personal-story-meta">
            <span>{story.sourceCount || 0} feed sources</span><span>{story.articleCount || 0} articles</span>
            <span>{story.newestPublicationAt ? "Latest known publication " + personalDateTime(story.newestPublicationAt) : "Publication time unknown"}</span>
          </div>
        </div>
        <button className={"btn btn-sm " + (story.saved ? "btn-primary" : "")} type="button"
          disabled={!canSave || pending} aria-pressed={!!story.saved}
          title={!canSave ? "Choose the local owner before saving" : undefined}
          onClick={(event) => { event.stopPropagation(); onToggle(story); }}>
          <Icon.watchlist />{pending ? "Saving…" : story.saved ? "Saved" : "Save"}
        </button>
      </div>
    </article>
  );
}

function NewerRunNotice({ run, stale, timezone, onRetry, retryPending }) {
  if (!run) return null;
  const active = run.state === "queued" || run.state === "running";
  const failed = run.state === "failed" || run.state === "partially_failed";
  return (
    <div className={"personal-run-notice " + (failed ? "is-error" : "")} role="status">
      <div>
        <div className="personal-card-labels"><strong>{active ? "A newer update is in progress" : failed ? "A newer update did not finish cleanly" : "Newer update status"}</strong><RunStateBadge run={run} stale={stale} /></div>
        <p>{run.localDate ? "Run date " + run.localDate + ". " : ""}{active ? "The readable result below remains available while this run works." : failed ? personalValue(run.error && (run.error.message || run.error.code)) : ""}</p>
        {run.updatedAt && <small>Last status read {personalDateTime(run.updatedAt, timezone)}</small>}
      </div>
      {failed && run.retryEligible && <button className="btn btn-sm" type="button" disabled={retryPending} onClick={() => onRetry(run.id)}>{retryPending ? "Retrying…" : "Retry this run"}</button>}
    </div>
  );
}

function personalGroupingEmptyState(run) {
  const grouping = run && run.stageResults && run.stageResults.grouping;
  if (!grouping || !["disabled", "blocked"].includes(grouping.status)) return null;
  return {
    title: grouping.status === "disabled" ? "Grouping is disabled" : "Grouped stories unavailable",
    hint: (grouping.status === "disabled" ? "AI grouping was disabled for this update." : "AI grouping could not run for this update.") + " Any retained RSS articles remain readable above.",
  };
}

function RealTodayPage() {
  const [workspace, setWorkspace] = useState(null);
  const [page, setPage] = useState({ items: [], total: 0, displayedRun: null, newerRun: null });
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [savedOnly, setSavedOnly] = useState(false);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState(null);
  const [pendingSave, setPendingSave] = useState("");
  const [pendingRun, setPendingRun] = useState("");
  const [runStatus, setRunStatus] = useState(null);
  const [runStatusStale, setRunStatusStale] = useState(false);
  const [runNotice, setRunNotice] = useState(null);
  const mounted = useRef(true);
  const loadGeneration = useRef(0);
  const poller = useRef(null);
  const requestState = useRef({ selectedRunId: "", query: "", savedOnly: false, offset: 0 });
  const visiblePage = useRef(page);
  requestState.current = { selectedRunId, query, savedOnly, offset };
  visiblePage.current = page;

  useEffect(() => () => {
    mounted.current = false;
    loadGeneration.current += 1;
    if (poller.current) poller.current.stop();
  }, []);

  const publishEvents = (items) => {
    window.PersonalUiCache.events = items;
    Store.set((state) => ({ dataVersion: state.dataVersion + 1 }));
  };

  const load = async (options) => {
    options = options || {};
    const token = ++loadGeneration.current;
    const requested = requestState.current;
    if (options.initial) setLoading(true);
    setError(null);
    const results = await Promise.allSettled([
      PersonalApi.workspace(),
      PersonalApi.listEvents({ runId: requested.selectedRunId || undefined, query: requested.query, savedOnly: requested.savedOnly, limit: PERSONAL_PAGE_SIZE, offset: requested.offset }),
      PersonalApi.listRuns({ limit: 100, offset: 0 }),
    ]);
    if (!mounted.current || personalIsDemo() || token !== loadGeneration.current) return;
    if (results[0].status === "fulfilled") setWorkspace(results[0].value);
    if (results[2].status === "fulfilled") setRuns(results[2].value.items || []);
    if (results[1].status === "fulfilled") {
      setPage(results[1].value);
      setStale(false);
      publishEvents(results[1].value.items || []);
      const latest = results[1].value.newerRun || results[1].value.displayedRun || (results[0].status === "fulfilled" && results[0].value.latestRun);
      if (latest) setRunStatus(latest);
    } else {
      setStale(visiblePage.current.items.length > 0 || !!visiblePage.current.displayedRun);
    }
    setError(personalTodayReadError(results));
    setLoading(false);
  };

  useEffect(() => { load({ initial: true }); }, [selectedRunId, query, savedOnly, offset]);

  useEffect(() => {
    const focus = () => {
      load();
      if (poller.current) poller.current.refresh();
    };
    window.addEventListener("focus", focus);
    return () => window.removeEventListener("focus", focus);
  }, [selectedRunId, query, savedOnly, offset]);

  useEffect(() => {
    if (!runStatus || PERSONAL_TERMINAL_STATES.has(runStatus.state)) return undefined;
    const nextPoller = SignalApiAdapter.createPersonalStatusPoller({
      readRun: (id) => PersonalApi.run(id),
      interval: 2000,
      onUpdate: (run) => { if (mounted.current && !personalIsDemo()) { setRunStatus(run); setRunStatusStale(false); } },
      onStale: () => { if (mounted.current && !personalIsDemo()) setRunStatusStale(true); },
      onTerminal: (run) => { if (mounted.current && !personalIsDemo()) { setRunStatus(run); load(); } },
    });
    poller.current = nextPoller;
    nextPoller.start(runStatus.id, false);
    return () => nextPoller.stop();
  }, [runStatus && runStatus.id, runStatus && runStatus.state]);

  const applyFilter = (event) => {
    event.preventDefault();
    setOffset(0);
    setQuery(queryDraft.trim());
  };

  const resetFilters = () => {
    setQueryDraft(""); setQuery(""); setSavedOnly(false); setOffset(0);
  };

  const toggleSave = async (story) => {
    if (pendingSave || !(workspace && workspace.actions && workspace.actions.canSave)) return;
    setPendingSave(story.id); setError(null);
    try {
      if (story.saved) await PersonalApi.unsaveEvent(story.id); else await PersonalApi.saveEvent(story.id);
      if (mounted.current && !personalIsDemo()) {
        setPage((current) => ({ ...current, items: current.items.map((item) => item.id === story.id ? { ...item, saved: !story.saved } : item) }));
        Store.flash(story.saved ? "Removed from Saved" : "Saved");
        await load();
      }
    } catch (err) {
      if (mounted.current && !personalIsDemo()) setError(err);
    } finally {
      if (mounted.current && !personalIsDemo()) setPendingSave("");
    }
  };

  const beginRun = async (retryId) => {
    if (pendingRun) return;
    setPendingRun(retryId || "start"); setError(null); setRunNotice(null);
    try {
      const result = retryId ? await PersonalApi.retryRun(retryId) : await PersonalApi.startRun();
      if (mounted.current && !personalIsDemo()) {
        setRunStatus(result.run);
        setRunStatusStale(false);
        if (result.alreadyProcessed) setRunNotice({ id: result.run.id, localDate: result.run.localDate });
        await load();
      }
    } catch (err) {
      if (mounted.current && !personalIsDemo()) setError(err);
    } finally {
      if (mounted.current && !personalIsDemo()) setPendingRun("");
    }
  };

  const displayed = page.displayedRun;
  const groupingEmptyState = personalGroupingEmptyState(displayed);
  const newer = page.newerRun || (runStatus && displayed && runStatus.id !== displayed.id ? runStatus : null);
  const displayedNeedsRetry = displayed && (displayed.state === "failed" || displayed.state === "partially_failed") && displayed.retryEligible;
  const timezone = workspace && workspace.workspace && workspace.workspace.timezone;
  const canStart = !!(workspace && workspace.actions && workspace.actions.canStart);
  const canSave = !!(workspace && workspace.actions && workspace.actions.canSave);
  const lastSuccess = page.lastSuccessfulUpdateAt || (workspace && workspace.lastSuccessfulUpdateAt) || null;
  const active = runStatus && (runStatus.state === "queued" || runStatus.state === "running");
  const workspaceReadable = !!workspace;

  return (
    <div className="page personal-page">
      <div className="page-head">
        <div className="titles">
          <div className="eyebrow">Personal news research desk</div><h1 className="page-title">Today</h1>
          <div className="page-sub">{displayed ? "Readable results for " + displayed.localDate + (timezone ? " · " + timezone : "") : "Stored results from your selected feeds."}</div>
        </div>
        <div className="personal-actions">
          <button className="btn btn-sm" type="button" onClick={() => load()} disabled={loading}><Icon.refresh /> {loading ? "Reading…" : "Refresh display"}</button>
          <button className="btn btn-primary btn-sm" type="button" onClick={() => beginRun()} disabled={!canStart || !!pendingRun || active}
            title={!workspaceReadable ? "Cannot read workspace or update status" : !canStart ? "Finish the local owner and feed configuration first" : undefined}>
            <Icon.bolt />{pendingRun === "start" ? "Requesting…" : active ? "Update in progress" : "Request today’s update"}
          </button>
        </div>
      </div>

      <PersonalError error={error} stale={stale} />
      {runNotice && <div className="personal-published-note" role="status"><Icon.check /><span><strong>Already processed.</strong> Showing the stored update{runNotice.localDate ? " for " + runNotice.localDate : ""}. <small>Run {runNotice.id}</small></span></div>}
      <WorkspaceSetup model={workspace} onChanged={() => load()} />
      <NewerRunNotice run={newer} stale={runStatusStale} timezone={timezone} onRetry={beginRun} retryPending={!!pendingRun} />
      {workspace && workspace.workspace && !savedOnly && <PersonalRawArticles timezone={timezone} runId={selectedRunId} query={query} refreshKey={runStatus && runStatus.updatedAt} />}

      {displayed && <div className="grid personal-run-grid">
        <Card title="Displayed update" icon={Icon.activity} eyebrow={displayed.localDate}>
          <div className="personal-card-labels"><RunStateBadge run={displayed} /><span className="muted">Attempt {displayed.attempt || 1} of {displayed.maxAttempts || 3}</span></div>
          <div className="stack personal-status-rows"><Row k="Last successful update" v={personalDateTime(lastSuccess, timezone)} /><Row k="Run ID" v={displayed.id} />{displayed.profileRevision && <Row k="Settings revision" v={displayed.profileRevision} />}{displayed.scopesFrozenAt && <Row k="Article selection frozen" v={personalDateTime(displayed.scopesFrozenAt, timezone)} />}</div>
          {displayedNeedsRetry && <button className="btn btn-sm" type="button" disabled={!!pendingRun} onClick={() => beginRun(displayed.id)}>{pendingRun ? "Retrying…" : "Retry this run"}</button>}
        </Card>
        <Card title="Capture coverage" icon={Icon.link}><CoverageSummary run={displayed} timezone={timezone} /></Card>
      </div>}

      <form className="personal-filter" onSubmit={applyFilter}>
        <label><span>Search stored stories</span><input value={queryDraft} maxLength={160} onChange={(event) => setQueryDraft(event.target.value)} placeholder="Title or summary" /></label>
        <label><span>Update</span><select value={selectedRunId} onChange={(event) => { setSelectedRunId(event.target.value); setOffset(0); }}>
          <option value="">Latest readable result</option>
          {runs.map((run) => <option key={run.id} value={run.id}>{run.localDate} · {personalLabel(run.state)} · attempt {run.attempt}</option>)}
        </select></label>
        <label className="personal-checkbox"><input type="checkbox" checked={savedOnly} onChange={(event) => { setSavedOnly(event.target.checked); setOffset(0); }} /><span>Saved only</span></label>
        <div className="personal-actions"><button className="btn btn-sm" type="submit">Apply</button><button className="btn btn-sm btn-ghost" type="button" onClick={resetFilters}>Reset</button></div>
      </form>

      {loading && !page.items.length ? <Card><EmptyState icon={Icon.refresh} title="Reading stored results" /></Card>
        : error && !displayed ? <Card><EmptyState icon={Icon.warn} title="Stored results are unavailable" hint="The workspace and update status could not be confirmed. Retry the display after the local API is available." action={<button className="btn btn-sm" type="button" onClick={() => load({ initial: true })}>Retry display</button>} /></Card>
        : !displayed ? <Card><EmptyState icon={Icon.events} title="No grouped update yet" hint={canStart ? "Request an update to collect RSS articles from the selected feeds. Grouping and briefs depend on AI readiness." : "Initialize and configure the workspace, then request today’s update."} /></Card>
        : page.total === 0 && groupingEmptyState ? <Card><EmptyState icon={Icon.info} title={groupingEmptyState.title} hint={groupingEmptyState.hint} /></Card>
        : page.total === 0 && !query && !savedOnly ? <Card><EmptyState icon={Icon.check} title="No stories matched this update" hint="The stored update is readable and its capture coverage is shown above. A complete capture failure is reported as a failure, not a quiet result." /></Card>
        : page.total === 0 ? <Card><EmptyState icon={Icon.search} title="No stories match these filters" hint="Reset the filters or choose another update." /></Card>
        : <div className="stack">
          <div className="personal-section-head"><div><div className="eyebrow">Stored results</div><h2>{page.total} grouped {page.total === 1 ? "story" : "stories"}</h2></div>{stale && <span className="badge badge-med">Stale display</span>}</div>
          <div className="grid personal-story-grid">{page.items.map((story) => <PersonalStoryCard key={story.id} story={story} runId={displayed.id} pending={pendingSave === story.id} canSave={canSave} onToggle={toggleSave} />)}</div>
          <PersonalPager offset={page.offset || offset} limit={page.limit || PERSONAL_PAGE_SIZE} total={page.total || 0} onChange={setOffset} disabled={loading} />
        </div>}
      <LocalAccessKey />
    </div>
  );
}

function TodayPage() { return personalIsDemo() ? <DemoTodayPage /> : <RealTodayPage />; }

function SavedItem({ item, pending, onRemove }) {
  const available = item.available !== false && item.event;
  return (
    <article className="card card-pad personal-saved-item">
      <div className="personal-card-row">
        <div>
          <div className="personal-card-labels"><span className="eyebrow">Saved story</span>{!available && <span className="badge badge-med">Target unavailable</span>}</div>
          <h2>{available ? item.event.title : item.label || "Unavailable saved story"}</h2>
          <p>{available ? item.event.summary || "No summary is available." : "The original event pointer cannot be loaded. Its saved label and entry identity have been preserved."}</p>
          <small>Saved {personalDateTime(item.savedAt)}</small>
        </div>
        <div className="personal-actions">
          {available && <button className="btn btn-sm" type="button" onClick={() => Store.nav("event", item.eventId)}>Open</button>}
          <button className="btn btn-sm" type="button" disabled={pending} onClick={() => onRemove(item)}>{pending ? "Removing…" : "Remove"}</button>
        </div>
      </div>
    </article>
  );
}

function LegacySavedList({ page, error, offset, onOffset }) {
  return (
    <Card title="Legacy saved items" icon={Icon.historical} eyebrow="Read-only · all owners and item types">
      <p className="personal-copy">These records remain in their original owner and item-type groups. Personal saving does not reassign or convert them.</p>
      <PersonalError error={error} />
      {(page.items || []).length ? <div className="personal-legacy-list">{page.items.map((item) => <div className="personal-legacy-row" key={item.id}>
        <div><strong>{item.label || item.itemId || "Unlabeled item"}</strong><small>Owner {item.userId} · {personalLabel(item.itemType)} · target {item.itemId}</small></div>
        <span>{personalDateTime(item.createdAt)}</span>
      </div>)}</div> : !error && <span className="muted">No legacy saved items.</span>}
      <PersonalPager offset={page.offset || offset} limit={page.limit || PERSONAL_PAGE_SIZE} total={page.total || 0} onChange={onOffset} />
    </Card>
  );
}

function RealSavedPage() {
  const [workspace, setWorkspace] = useState(null);
  const [page, setPage] = useState({ items: [], total: 0 });
  const [legacy, setLegacy] = useState({ items: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [legacyOffset, setLegacyOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [legacyError, setLegacyError] = useState(null);
  const [pending, setPending] = useState("");
  const generation = useRef(0);
  const mounted = useRef(true);
  const requestState = useRef({ offset: 0, legacyOffset: 0 });
  requestState.current = { offset, legacyOffset };
  useEffect(() => () => { mounted.current = false; generation.current += 1; }, []);

  const load = async () => {
    const token = ++generation.current; setLoading(true); setError(null); setLegacyError(null);
    const requested = requestState.current;
    const results = await Promise.allSettled([
      PersonalApi.workspace(), PersonalApi.listSaved({ limit: PERSONAL_PAGE_SIZE, offset: requested.offset }),
      PersonalApi.listLegacySaved({ limit: PERSONAL_PAGE_SIZE, offset: requested.legacyOffset }),
    ]);
    if (!mounted.current || personalIsDemo() || token !== generation.current) return;
    if (results[0].status === "fulfilled") setWorkspace(results[0].value);
    if (results[1].status === "fulfilled") setPage(results[1].value); else setError(results[1].reason);
    if (results[2].status === "fulfilled") setLegacy(results[2].value); else setLegacyError(results[2].reason);
    setLoading(false);
  };
  useEffect(() => { load(); }, [offset, legacyOffset]);

  const remove = async (item) => {
    if (pending) return;
    setPending(item.id); setError(null);
    try {
      if (item.available !== false && item.eventId) await PersonalApi.unsaveEvent(item.eventId);
      else await PersonalApi.unsaveEntry(item.id);
      if (mounted.current && !personalIsDemo()) { Store.flash("Removed from Saved"); await load(); }
    } catch (err) { if (mounted.current && !personalIsDemo()) setError(err); }
    finally { if (mounted.current && !personalIsDemo()) setPending(""); }
  };

  return (
    <div className="page personal-page">
      <div className="page-head"><div className="titles"><div className="eyebrow">Personal news research desk</div><h1 className="page-title">Saved</h1><div className="page-sub">Stories saved for your local profile.</div></div><button className="btn btn-sm" type="button" onClick={load} disabled={loading}><Icon.refresh /> Refresh display</button></div>
      <WorkspaceSetup model={workspace} onChanged={load} showReadiness={false} />
      <PersonalError error={error} stale={(page.items || []).length > 0} />
      {loading && !(page.items || []).length ? <Card><EmptyState icon={Icon.refresh} title="Reading saved stories" /></Card>
        : (page.items || []).length ? <div className="stack">{page.items.map((item) => <SavedItem key={item.id} item={item} pending={pending === item.id} onRemove={remove} />)}<PersonalPager offset={page.offset || offset} limit={page.limit || PERSONAL_PAGE_SIZE} total={page.total || 0} onChange={setOffset} disabled={loading} /></div>
        : !error && <Card><EmptyState icon={Icon.watchlist} title="No personal saved stories" hint="Save a story from Today or from its source detail." /></Card>}
      <LegacySavedList page={legacy} error={legacyError} offset={legacyOffset} onOffset={setLegacyOffset} />
      <LocalAccessKey />
    </div>
  );
}

function SavedPage() {
  if (!personalIsDemo()) return <RealSavedPage />;
  return <div className="page personal-page"><div className="page-head"><div className="titles"><div className="eyebrow">Personal news research desk</div><h1 className="page-title">Saved</h1><div className="page-sub">The demonstration is read-only.</div></div></div><Card><EmptyState icon={Icon.watchlist} title="Saved stories are unavailable in the demo" hint="Switch to Real to read the bound owner's persistent saves." /></Card></div>;
}

function BriefStatusBadge({ status }) {
  const cls = status === "published" ? "badge-low" : status === "failed" ? "badge-high" : "badge-neutral";
  return <span className={"badge " + cls}>{personalLabel(status || "unknown")}</span>;
}

function RealBriefsPage() {
  const [page, setPage] = useState({ items: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const generation = useRef(0);
  useEffect(() => { const token = ++generation.current; setLoading(true); PersonalApi.listBriefs({ limit: PERSONAL_PAGE_SIZE, offset }).then((result) => {
    if (token === generation.current && !personalIsDemo()) { setPage(result); setError(null); setLoading(false); }
  }).catch((err) => { if (token === generation.current && !personalIsDemo()) { setError(err); setLoading(false); } }); return () => { generation.current += 1; }; }, [offset, refreshToken]);
  const published = (page.items || []).find((item) => item.status === "published");
  return (
    <div className="page personal-page">
      <div className="page-head"><div className="titles"><div className="eyebrow">Personal news research desk</div><h1 className="page-title">Briefs</h1><div className="page-sub">Stored personal reports by date and immutable version.</div></div><button className="btn btn-sm" type="button" disabled={loading} onClick={() => setRefreshToken((value) => value + 1)}><Icon.refresh /> Refresh display</button></div>
      <PersonalError error={error} stale={(page.items || []).length > 0} />
      {published && <div className="personal-published-note"><Icon.check /><span>Published brief on this page: <button type="button" onClick={() => Store.nav("brief", published.id)}>{published.title || published.briefDate}</button>. Failed versions remain visible without replacing published content.</span></div>}
      {loading && !(page.items || []).length ? <Card><EmptyState icon={Icon.refresh} title="Reading brief history" /></Card>
        : error && !(page.items || []).length ? <Card><EmptyState icon={Icon.warn} title="Brief history is unavailable" hint="No report history was substituted." action={<button className="btn btn-sm" type="button" onClick={() => setRefreshToken((value) => value + 1)}>Retry briefs</button>} /></Card>
        : !(page.items || []).length && !error ? <Card><EmptyState icon={Icon.reports} title="No personal briefs yet" hint="A completed daily update can publish a cited brief, including a healthy quiet-day report." /></Card>
        : <div className="stack">{(page.items || []).map((brief) => <article className="card card-pad personal-brief-row" key={brief.id}>
          <div><div className="personal-card-labels"><span className="eyebrow">{brief.briefDate || "Date unavailable"} · version {brief.version || 1}</span><BriefStatusBadge status={brief.status} /></div><h2>{brief.title || "Personal daily brief"}</h2><p>Report {brief.id}</p>{brief.error && <small className="personal-error-text">{personalValue(brief.error.message || brief.error.code || brief.error)}</small>}</div>
          {brief.status === "published" ? <button className="btn btn-sm" type="button" onClick={() => Store.nav("brief", brief.id)}>Open this version</button> : <button className="btn btn-sm" type="button" disabled>Not available to read or export</button>}
        </article>)}<PersonalPager offset={page.offset || offset} limit={page.limit || PERSONAL_PAGE_SIZE} total={page.total || 0} onChange={setOffset} disabled={loading} /></div>}
    </div>
  );
}

function BriefsPage() {
  if (!personalIsDemo()) return <RealBriefsPage />;
  return <div className="page personal-page"><div className="page-head"><div className="titles"><div className="eyebrow">Personal news research desk</div><h1 className="page-title">Briefs</h1><div className="page-sub">The demonstration is read-only.</div></div></div><Card><EmptyState icon={Icon.reports} title="Personal briefs are unavailable in the demo" hint="Switch to Real to read exact stored report versions and exports." /></Card></div>;
}

function sourceForDrawer(source) {
  const relatedClaims = Array.isArray(source.claimIds) ? source.claimIds : source.claimId ? [source.claimId] : undefined;
  return {
    id: source.evidenceItemId || source.articleId || source.id,
    sourceType: source.sourceType || "rss", title: source.title || "Untitled source",
    publisher: source.publisher, url: source.url, publishedAt: source.publishedAt,
    excerpt: source.snippet || source.excerpt, credibilityScore: source.credibility,
    relatedClaims, reportId: source.reportId,
  };
}

function PersonalSourceRow({ source }) {
  let scope = "Current event coverage";
  if (source.qualifiesForBrief === true) scope = "Used in this brief";
  else if (source.inRunScope === true) scope = "Included in this update; not selected for the brief";
  else if (source.inRunScope === false) scope = "Additional current coverage";
  return (
    <button className="personal-source-row" type="button" onClick={() => Store.openDrawer({ type: "evidence", data: sourceForDrawer(source) })}>
      <span><strong>{source.title || "Untitled source"}</strong><small>{source.publisher || "Publisher unspecified"}{source.publishedAt ? " · " + personalDateTime(source.publishedAt) : " · publication time unknown"}</small><small>{scope}</small></span><Icon.arrowRight />
    </button>
  );
}

function RealPersonalEventDetail({ id, runId }) {
  const [detail, setDetail] = useState(null);
  const [sources, setSources] = useState({ items: [], total: 0 });
  const [sourceOffset, setSourceOffset] = useState(0);
  const [detailError, setDetailError] = useState(null);
  const [sourceError, setSourceError] = useState(null);
  const [pending, setPending] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const generation = useRef(0);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; generation.current += 1; }, []);
  useEffect(() => {
    const token = ++generation.current; setDetailError(null); setSourceError(null);
    PersonalApi.eventDetail(id, runId).then((body) => { if (token === generation.current && !personalIsDemo()) setDetail(body.event); }).catch((err) => { if (token === generation.current && !personalIsDemo()) setDetailError(err); });
    PersonalApi.eventSources(id, { runId, limit: PERSONAL_SOURCE_PAGE_SIZE, offset: sourceOffset }).then((body) => { if (token === generation.current && !personalIsDemo()) setSources(body); }).catch((err) => { if (token === generation.current && !personalIsDemo()) setSourceError(err); });
    return () => { generation.current += 1; };
  }, [id, runId, sourceOffset, refreshToken]);

  const toggle = async () => {
    if (!detail || pending) return;
    const token = generation.current;
    const wasSaved = !!detail.saved;
    setPending(true); setDetailError(null);
    try {
      if (wasSaved) await PersonalApi.unsaveEvent(id); else await PersonalApi.saveEvent(id);
      if (mounted.current && token === generation.current && !personalIsDemo()) { setDetail((current) => ({ ...current, saved: !wasSaved })); Store.flash(wasSaved ? "Removed from Saved" : "Saved"); }
    } catch (err) { if (mounted.current && token === generation.current && !personalIsDemo()) setDetailError(err); }
    finally { if (mounted.current && token === generation.current && !personalIsDemo()) setPending(false); }
  };

  const hasUpdateScope = !!(runId || (detail && detail.runId));

  return (
    <div className="page personal-page">
      <div className="personal-actions"><button className="btn btn-sm btn-ghost" type="button" onClick={() => Store.nav(runId ? "today" : "saved")}><Icon.chevL /> {runId ? "Today" : "Saved"}</button><button className="btn btn-sm" type="button" onClick={() => setRefreshToken((value) => value + 1)}><Icon.refresh /> Refresh detail</button></div>
      <PersonalError error={detailError} stale={!!detail} />
      {!detail && !detailError ? <Card style={{ marginTop: 16 }}><EmptyState icon={Icon.refresh} title="Reading story detail" /></Card>
        : !detail ? <Card style={{ marginTop: 16 }}><EmptyState icon={Icon.events} title="Story unavailable" hint="This story could not be loaded for the requested workspace or update." action={<button className="btn btn-sm" type="button" onClick={() => setRefreshToken((value) => value + 1)}>Retry story</button>} /></Card>
        : <>
          <div className="personal-detail-head"><div><div className="personal-card-labels"><span className="eyebrow">Grouped story</span>{runId && <span className="badge badge-neutral">Viewed in update {runId}</span>}</div><h1 className="page-title">{detail.title || "Untitled story"}</h1><p>{detail.summary || "No summary is available."}</p></div><button className={"btn btn-sm " + (detail.saved ? "btn-primary" : "")} type="button" aria-pressed={!!detail.saved} disabled={pending} onClick={toggle}><Icon.watchlist />{pending ? "Saving…" : detail.saved ? "Saved" : "Save story"}</button></div>
          <div className="grid personal-detail-grid">
            <Card title="Recorded status" icon={Icon.activity}><div className="stack personal-status-rows"><Row k="All current articles" v={detail.membershipTotal || 0} />{hasUpdateScope && <Row k="Articles considered in this update" v={(detail.inScopeArticleIds || []).length} />}<Row k="Updated" v={personalDateTime(detail.updatedAt)} /><Row k="Event type" v={detail.eventType || "Not recorded"} /><Row k="Region" v={[detail.country, detail.region].filter(Boolean).join(" · ") || "Not recorded"} /></div></Card>
            <Card title="Article and source detail" icon={Icon.link} eyebrow={(sources.total || 0) + " recorded sources"}>
              <p className="personal-copy">{hasUpdateScope ? "Sources used in the brief, other articles included in this update, and additional current coverage are labeled separately." : "These sources show current coverage for this saved story. No daily update is selected."}</p>
              <PersonalError error={sourceError} stale={(sources.items || []).length > 0} />
              {(sources.items || []).length ? <div className="stack">{sources.items.map((source) => <PersonalSourceRow key={source.articleId || source.id} source={source} />)}<PersonalPager offset={sources.offset || sourceOffset} limit={sources.limit || PERSONAL_SOURCE_PAGE_SIZE} total={sources.total || 0} onChange={setSourceOffset} /></div>
                : sourceError ? <button className="btn btn-sm" type="button" onClick={() => setRefreshToken((value) => value + 1)}>Retry sources</button>
                : <EmptyState icon={Icon.doc} title="No source rows are available" hint="The story detail remains readable, but no article source was returned." />}
            </Card>
          </div>
        </>}
      <LocalAccessKey />
    </div>
  );
}

function DemoPersonalEventDetail({ id }) {
  const story = window.DATA.eventsById[id];
  if (!story) return <div className="page personal-page"><button className="btn btn-sm btn-ghost" onClick={() => Store.nav("today")}><Icon.chevL /> Today</button><Card style={{ marginTop: 16 }}><EmptyState icon={Icon.events} title="Sample story unavailable" /></Card></div>;
  const evidence = (story.evidenceSourceIds || []).map((sourceId) => window.DATA.evidenceById[sourceId]).filter(Boolean);
  return <div className="page personal-page"><button className="btn btn-sm btn-ghost" onClick={() => Store.nav("today")}><Icon.chevL /> Today</button><div className="personal-detail-head"><div><div className="eyebrow">Sample grouped story</div><h1 className="page-title">{story.title}</h1><p>{story.summary || "No summary is available."}</p></div><button className="btn btn-sm" disabled>Saving is disabled in the demo</button></div><Card title="Sample evidence" icon={Icon.link}>{evidence.length ? <div className="stack">{evidence.map((source) => <PersonalSourceRow key={source.id} source={source} />)}</div> : <EmptyState icon={Icon.doc} title="No sample evidence" />}</Card></div>;
}

function PersonalEventDetail({ id, runId }) { return personalIsDemo() ? <DemoPersonalEventDetail id={id} /> : <RealPersonalEventDetail id={id} runId={runId} />; }

function briefSectionText(section) {
  if (!section) return [];
  const value = section.content !== undefined ? section.content : section.body !== undefined ? section.body : section.summary;
  if (Array.isArray(value)) return value.map((item) => typeof item === "string" ? item : JSON.stringify(item));
  return value ? [String(value)] : [];
}

function PersonalBriefDetail({ id }) {
  const [model, setModel] = useState(null);
  const [error, setError] = useState(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const mounted = useRef(true);
  const generation = useRef(0);
  const mode = window.DATA && window.DATA.runtime && window.DATA.runtime.mode;
  useEffect(() => () => { mounted.current = false; generation.current += 1; }, []);
  useEffect(() => {
    const token = ++generation.current;
    setModel(null); setError(null);
    if (personalIsDemo()) return undefined;
    PersonalApi.brief(id).then((body) => { if (mounted.current && token === generation.current && !personalIsDemo()) setModel(body); }).catch((err) => { if (mounted.current && token === generation.current && !personalIsDemo()) setError(err); });
    return () => { generation.current += 1; };
  }, [id, refreshToken, mode]);
  if (personalIsDemo()) return <div className="page personal-page"><Card><EmptyState icon={Icon.reports} title="Stored briefs are unavailable in the demo" /></Card></div>;
  if (error && !model) return <div className="page personal-page"><button className="btn btn-sm btn-ghost" onClick={() => Store.nav("briefs")}><Icon.chevL /> Briefs</button><PersonalError error={error} /><Card><EmptyState icon={Icon.reports} title="This exact brief version is unavailable" hint="No other date or version was substituted." action={<button className="btn btn-sm" type="button" onClick={() => setRefreshToken((value) => value + 1)}>Retry this version</button>} /></Card></div>;
  if (!model) return <div className="page personal-page"><Card><EmptyState icon={Icon.refresh} title="Reading exact brief version" /></Card></div>;
  const report = model.report || {};
  const snapshot = model.snapshot || {};
  const citations = model.citations || report.citations || [];
  const published = report.status === "published";
  const openCitation = async (citation) => {
    const claimId = citation.claimId || citation.id;
    if (!claimId) return;
    const reportId = report.id;
    const token = generation.current;
    try {
      const body = await PersonalApi.claimEvidence(reportId, claimId);
      if (mounted.current && token === generation.current && !personalIsDemo()) Store.openDrawer({ type: "reportEvidence", data: { reportId, claimId, claim: citation.text || citation.claim || citation.title, items: body.evidence || body.items || [] } });
    } catch (err) { if (mounted.current && token === generation.current && !personalIsDemo()) Store.openDrawer({ type: "reportEvidence", data: { reportId, claimId, claim: citation.text || citation.claim || citation.title, error: err, items: [], retry: () => openCitation(citation) } }); }
  };
  return (
    <div className="page personal-page">
      <div className="personal-actions"><button className="btn btn-sm btn-ghost" onClick={() => Store.nav("briefs")}><Icon.chevL /> Briefs</button><button className="btn btn-sm" type="button" onClick={() => setRefreshToken((value) => value + 1)}><Icon.refresh /> Refresh this version</button></div>
      <div className="personal-detail-head"><div><div className="personal-card-labels"><span className="eyebrow">{report.briefDate} · version {report.version}</span><BriefStatusBadge status={report.status} /></div><h1 className="page-title">{report.title || "Personal daily brief"}</h1><p>Exact report {report.id}</p></div>{published && <div className="personal-actions"><a className="btn btn-sm" href={PersonalApi.reportExportUrl(report.id, "md")} target="_blank" rel="noopener noreferrer"><Icon.download /> Markdown</a><a className="btn btn-sm" href={PersonalApi.reportExportUrl(report.id, "pdf")} target="_blank" rel="noopener noreferrer"><Icon.download /> PDF</a></div>}</div>
      <PersonalError error={error} stale={!!model} />
      {!published && <PersonalError error={{ message: "This version is " + personalLabel(report.status) + " and has no published content or exports." }} />}
      <div className="grid personal-run-grid"><Card title="Input snapshot" icon={Icon.activity}><div className="stack personal-status-rows"><Row k="Snapshot ID" v={snapshot.id || "Not recorded"} /><Row k="Selected stories" v={(snapshot.selectedEventIds || []).length} /><Row k="Created" v={personalDateTime(report.createdAt)} /></div></Card><Card title="Capture coverage" icon={Icon.link}><CoverageSummary run={{ coverage: snapshot.coverage || {}, captureStartedAt: snapshot.captureStartedAt, captureEndedAt: snapshot.captureEndedAt }} /></Card></div>
      {published && <div className="stack personal-brief-sections">{(report.sections || []).map((section, index) => <Card key={section.id || index} title={section.title || "Brief section"} icon={Icon.doc}>{briefSectionText(section).length ? briefSectionText(section).map((text, line) => <p className="personal-brief-copy" key={line}>{text}</p>) : <span className="muted">No section text is available.</span>}</Card>)}</div>}
      {published && <Card title="Citations" icon={Icon.link} eyebrow={citations.length + " report-scoped claims"}>{citations.length ? <div className="personal-citations">{citations.map((citation, index) => <button className="personal-source-row" key={citation.claimId || citation.id || index} onClick={() => openCitation(citation)}><span><strong>{citation.text || citation.claim || citation.title || "Cited claim"}</strong><small>Open evidence from report {report.id}</small></span><Icon.arrowRight /></button>)}</div> : <span className="muted">No citations were returned for this report.</span>}</Card>}
    </div>
  );
}

function ReportEvidenceDrawer({ model }) {
  const items = model.items || [];
  return <><div className="drawer-scrim" onClick={() => Store.closeDrawer()} /><div className="drawer"><div className="drawer-head"><Icon.link /><div style={{ flex: 1 }}><div className="eyebrow">Report-scoped evidence</div><div>{model.claim || "Cited claim"}</div></div><button className="btn btn-icon btn-ghost" onClick={() => Store.closeDrawer()}><Icon.x /></button></div><div className="drawer-body"><p className="personal-copy">Evidence for claim {model.claimId} in exact report {model.reportId}.</p><PersonalError error={model.error} />{model.error && model.retry && <button className="btn btn-sm" type="button" onClick={model.retry}>Retry evidence</button>}{items.length ? <div className="stack">{items.map((item, index) => { const source = sourceForDrawer(item); const url = SignalApiAdapter.safeHttpUrl(source.url); return <div className="personal-evidence-card" key={source.id || index}><strong>{source.title}</strong><small>{source.publisher || "Publisher unspecified"}{source.publishedAt ? " · " + personalDateTime(source.publishedAt) : ""}</small><p>{source.excerpt || "No excerpt available."}</p>{url ? <a className="btn btn-sm" href={url} target="_blank" rel="noopener noreferrer">Open original source</a> : <button className="btn btn-sm" disabled>Source link unavailable</button>}</div>; })}</div> : !model.error && <EmptyState icon={Icon.doc} title="No evidence rows returned" />}</div></div></>;
}

function PersonalArticleCard({ item, timezone, detail = false }) {
  const url = SignalApiAdapter.safeHttpUrl(item.url);
  return <article className="card card-pad personal-story">
    <div className="personal-card-labels"><span className="eyebrow">Retained RSS article</span><span className="badge badge-neutral">{personalLabel(item.enrichmentState || "raw")}</span>{item.fromBacklog && <span className="badge badge-neutral">From existing backlog</span>}</div>
    <h2>{item.title || "Untitled article"}</h2>
    <div className="personal-story-meta"><span>{item.sourceName || "Source unspecified"}</span><span>{item.publishedAt ? "Published " + personalDateTime(item.publishedAt, timezone) : "Publication time unknown"}</span></div>
    <p>{item.snippet || "No RSS snippet was supplied."}</p>
    {item.truncated && <small className="muted">The retained RSS text was shortened to its storage limit.</small>}
    {detail && <div className="stack personal-status-rows"><Row k="Captured" v={personalDateTime(item.capturedAt, timezone)} /><Row k="Admitted" v={personalDateTime(item.admittedAt, timezone)} /></div>}
    {item.error && <p className="personal-error-text">Processing: {item.error}</p>}
    <div className="personal-actions">
      {!detail && <button className="btn btn-sm" type="button" onClick={() => Store.nav("article", item.id)}>Read retained article</button>}
      {item.eventId && <button className="btn btn-sm" type="button" onClick={() => Store.nav("event", item.eventId)}>Open grouped story</button>}
      {url && <a className="btn btn-sm" href={url} target="_blank" rel="noopener noreferrer">Open original source</a>}
    </div>
  </article>;
}

function PersonalRawArticles({ timezone, runId, query, refreshKey }) {
  const [page, setPage] = useState({ items: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);
  useEffect(() => { setOffset(0); setPage({ items: [], total: 0 }); }, [runId, query]);
  useEffect(() => {
    const token = ++generation.current; setLoading(true);
    PersonalApi.listArticles({ ungrouped: true, interestOnly: true, runId: runId || undefined, query, limit: PERSONAL_PAGE_SIZE, offset }).then((body) => {
      if (token === generation.current && !personalIsDemo()) { setPage(body); setError(null); }
    }).catch((err) => { if (token === generation.current && !personalIsDemo()) setError(err); })
      .finally(() => { if (token === generation.current) setLoading(false); });
    return () => { generation.current += 1; };
  }, [runId, query, offset, refreshKey, refresh]);
  return <section className="stack" aria-label="Retained RSS articles">
    <div className="personal-section-head"><div><div className="eyebrow">Readable without AI</div><h2>Retained RSS articles{!error && !loading ? " · " + page.total : ""}</h2></div><button className="btn btn-sm" type="button" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>Refresh articles</button></div>
    <p className="personal-copy">Admitted articles awaiting grouping stay readable here. Grouped articles appear in their story and remain accessible by their original article link.</p>
    <PersonalError error={error} stale={page.items.length > 0} />
    {page.items.length ? <><div className="grid personal-story-grid">{page.items.map((item) => <PersonalArticleCard key={item.id} item={item} timezone={timezone} />)}</div><PersonalPager offset={offset} limit={PERSONAL_PAGE_SIZE} total={page.total} onChange={setOffset} disabled={loading} /></>
      : !error && <Card><EmptyState icon={Icon.doc} title={loading ? "Reading retained articles" : "No ungrouped articles match this view"} hint="Collection and admission are separate from completed AI processing." /></Card>}
  </section>;
}

function PersonalArticleDetail({ id }) {
  const [item, setItem] = useState(null);
  const [error, setError] = useState(null);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let alive = true;
    setItem(null); setError(null);
    if (!personalIsDemo()) PersonalApi.article(id).then((body) => { if (alive) setItem(body.article); }).catch((err) => { if (alive) setError(err); });
    return () => { alive = false; };
  }, [id, refresh]);
  if (personalIsDemo()) return <UnavailablePersonalPage route={{ name: "article" }} />;
  return <div className="page personal-page"><div className="page-head"><h1 className="page-title">Retained article</h1><button className="btn btn-sm" type="button" onClick={() => Store.nav("today")}>Today</button></div><PersonalError error={error} />{item ? <PersonalArticleCard item={item} detail /> : <Card><EmptyState icon={Icon.doc} title={error ? "This article is unavailable" : "Reading article"} action={error && <button className="btn btn-sm" type="button" onClick={() => setRefresh((value) => value + 1)}>Retry article</button>} /></Card>}</div>;
}

function personalSettingsDraft(model) {
  const profile = model.profile || {};
  return { ...model.settings, timezone: model.timezone, selectedSourceIds: profile.selectedSourceIds || [], includeText: (profile.includePhrases || []).join("\n"), excludeText: (profile.excludePhrases || []).join("\n"), executionProfile: profile.executionProfile || "raw" };
}

function personalSnakeValues(value) {
  if (Array.isArray(value)) return value.map(personalSnakeValues);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key.replace(/[A-Z]/g, (letter) => "_" + letter.toLowerCase()), personalSnakeValues(item)]));
}

function personalSettingsBody(draft) {
  const { includeText, excludeText, ...values } = draft;
  return personalSnakeValues({ ...values,
    includePhrases: includeText.split("\n").map((value) => value.trim()).filter(Boolean),
    excludePhrases: excludeText.split("\n").map((value) => value.trim()).filter(Boolean),
    monthlyAllowanceUsd: values.monthlyAllowanceUsd === "" ? null : values.monthlyAllowanceUsd,
    runAllowanceUsd: values.runAllowanceUsd === "" ? null : values.runAllowanceUsd,
  });
}

function PersonalSpending({ model, timezone }) {
  return <Card title="Recorded application spending" icon={Icon.activity}>
    <div className="personal-card-labels"><span className="badge badge-neutral">{personalLabel(model.status)}</span><span>{model.accountingPeriod} · {model.accountingTimezone || "UTC"} accounting period</span></div>
    <div className="stack personal-status-rows"><Row k="Permitted this month" v={"$" + model.monthlyAllowanceUsd} /><Row k="Finalized cost" v={"$" + model.finalizedUsd} /><Row k="Reserved this month" v={"$" + model.reservedUsd} /><Row k="Unresolved, all periods" v={"$" + model.unresolvedUsd} /><Row k="Remaining this month" v={"$" + model.remainingUsd} /><Row k="Next UTC reset, local time" v={personalDateTime(model.nextResetAt, timezone)} /></div>
    {!!model.unreconciledLegacyCount && <p className="personal-error-text">{model.unreconciledLegacyCount} prior application usage records need reconciliation before paid work can be enabled.</p>}
    <p className="personal-copy">This ledger covers requests through this application. Uncertain dispatches keep their reservation until receipt evidence resolves them.</p>
  </Card>;
}

function PersonalBacklog({ timezone, refreshKey }) {
  const [page, setPage] = useState(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState(null);
  useEffect(() => {
    let alive = true;
    PersonalApi.backlog({ limit: PERSONAL_PAGE_SIZE, offset }).then((body) => { if (alive) { setPage(body); setError(null); } }).catch((err) => { if (alive) setError(err); });
    return () => { alive = false; };
  }, [offset, refreshKey]);
  return <Card title="Retained pending candidates" icon={Icon.doc}>
    <PersonalError error={error} stale={!!page} />
    {page && <><p className="personal-copy">{page.total} retained pending · {page.eligibleTotal} eligible · {page.disabledSourceTotal} from disabled sources. Pending content is retained before admission; these records have not completed enrichment.</p><div className="stack">{page.items.map((item) => <div key={item.id} className="personal-legacy-row"><div><strong>{item.title}</strong><small>{item.sourceName} · {item.publishedAt ? "Published " + personalDateTime(item.publishedAt, timezone) : "Publication time unknown"}</small><p>{item.snippet || "No RSS snippet supplied."}</p><small>Captured {personalDateTime(item.capturedAt, timezone)}</small></div><span className="badge badge-neutral">{item.eligible ? "Eligible" : personalLabel(item.disabledReason || "source_disabled")}</span></div>)}</div><PersonalPager offset={offset} limit={PERSONAL_PAGE_SIZE} total={page.total} onChange={setOffset} /></>}
  </Card>;
}

function PersonalCaptureReceipts({ receipts, sources, timezone }) {
  if (!receipts || !receipts.length) return <p className="muted">No feed capture receipts are available yet.</p>;
  return <details className="personal-access"><summary>Latest run’s feed captures</summary><div className="stack">{receipts.map((receipt) => {
    const details = receipt.details || {};
    const source = (sources || []).find((item) => item.id === receipt.sourceId);
    return <div key={receipt.id} className="personal-evidence-card"><strong>{source ? source.name : "RSS feed"} · {personalLabel(receipt.status)}</strong><small>Attempt {receipt.attempt} · {personalDateTime(receipt.startedAt, timezone)} → {personalDateTime(receipt.endedAt, timezone)}</small>{details.entriesObserved != null && <Row k="Observed in capture" v={details.entriesObserved} />}{details.retained != null && <Row k="Retained candidates" v={details.retained} />}{(details.boundsReached || []).length > 0 && <p>Capture limit: {details.boundsReached.map(personalLabel).join(", ")}. Records beyond the observed capture are not counted.</p>}{details.totalEntriesKnown === false && <small>Complete remote feed total is unknown.</small>}</div>;
  })}</div></details>;
}

function PersonalRouteEditor({ route, onChange }) {
  if (route && route.mode === "offline_fixture") return <p className="personal-copy">A labeled offline fixture is configured. It uses deterministic inputs with no provider spending. Its route remains unchanged when saving these settings.</p>;
  const current = route || {};
  const change = (role, key, value) => onChange({ ...current, mode: "live", [role]: { ...(current[role] || {}), [key]: value } });
  const fields = [
    ["provider", "Provider"], ["model", "Model"], ["modelVersion", "Model version"],
    ["priceRevision", "Verified price revision"], ["priceSourceUrl", "Official pricing URL"],
    ["inputUsdPerMillionTokens", "Input USD per million tokens"], ["outputUsdPerMillionTokens", "Output USD per million tokens"],
    ["maxInputTokens", "Maximum input tokens", true], ["maxOutputTokens", "Maximum output tokens", true], ["deadlineSeconds", "Request deadline in seconds", true],
  ];
  return <details className="personal-access"><summary>Provider models, prices, and request bounds</summary><p className="personal-copy">Enter both complete routes using verified official pricing. No fallback is enabled. Provider credentials stay in the server environment.</p>{["generation", "embedding"].map((role) => <fieldset key={role} style={{ border: "1px solid var(--line)", padding: 16, margin: "12px 0" }}><legend>{role === "generation" ? "Brief generation and checking" : "Article embeddings"}</legend><div className="personal-filter">{fields.map(([key, label, numeric]) => <label key={key}><span>{label}</span><input type={numeric ? "number" : "text"} min={numeric ? 0 : undefined} value={current[role] && current[role][key] !== undefined ? current[role][key] : ""} onChange={(event) => change(role, key, numeric ? Number(event.target.value) : event.target.value)} /></label>)}</div></fieldset>)}</details>;
}

function PersonalSourceDraftForm({ onAdded }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const add = async (event) => {
    event.preventDefault();
    if (pending || personalIsDemo()) return;
    setPending(true); setError(null);
    try {
      const body = await PersonalApi.addSource({ name: name.trim(), feed_url: url.trim(), source_type: "rss" });
      if (alive.current && !personalIsDemo()) { onAdded(body.source); setName(""); setUrl(""); }
    } catch (err) { if (alive.current && !personalIsDemo()) setError(err); }
    finally { if (alive.current) setPending(false); }
  };
  return <Card title="Add an RSS source" icon={Icon.link}><form onSubmit={add} className="personal-filter personal-route-fields"><label><span>Source name</span><input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label><label><span>RSS URL</span><input required type="url" maxLength={2048} value={url} onChange={(event) => setUrl(event.target.value)} /></label><button className="btn btn-sm" type="submit" disabled={pending}>{pending ? "Adding…" : "Add draft source"}</button></form><p className="personal-copy">Adding retains the address without fetching or enabling it. Select its checkbox and save settings to enable collection.</p><PersonalError error={error} /></Card>;
}

function RealPersonalSettingsPage() {
  const [model, setModel] = useState(null);
  const [draft, setDraft] = useState(null);
  const [spending, setSpending] = useState(null);
  const [collection, setCollection] = useState(null);
  const [error, setError] = useState(null);
  const [spendingError, setSpendingError] = useState(null);
  const [collectionError, setCollectionError] = useState(null);
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);
  useEffect(() => {
    const token = ++generation.current;
    PersonalApi.settings().then((body) => { if (token === generation.current) { setModel(body); setDraft(personalSettingsDraft(body)); setError(null); } }).catch((err) => { if (token === generation.current) setError(err); });
    PersonalApi.spending().then((body) => { if (token === generation.current) { setSpending(body); setSpendingError(null); } }).catch((err) => { if (token === generation.current) setSpendingError(err); });
    PersonalApi.collection().then((body) => { if (token === generation.current) { setCollection(body); setCollectionError(null); } }).catch((err) => { if (token === generation.current) setCollectionError(err); });
    return () => { generation.current += 1; };
  }, [refresh]);
  const change = (key, value) => { setDraft((current) => ({ ...current, [key]: value, ...(key === "executionProfile" && value === "raw" ? { aiEnabled: false } : {}) })); setNotice(""); };
  const save = async (event) => {
    event.preventDefault();
    if (pending || personalIsDemo()) return;
    const token = generation.current;
    setPending(true); setError(null); setNotice("");
    try {
      const body = await PersonalApi.updateSettings(personalSettingsBody(draft));
      if (token === generation.current && !personalIsDemo()) { setModel(body); setDraft(personalSettingsDraft(body)); setNotice("Settings saved. Source, interest, route, and run-limit changes apply to the next run. Lower daily and spending ceilings apply to new admissions and requests immediately."); setRefresh((value) => value + 1); }
    } catch (err) { if (token === generation.current && !personalIsDemo()) setError(err); }
    finally { if (token === generation.current) setPending(false); }
  };
  return <div className="page personal-page"><div className="page-head"><div><div className="eyebrow">Personal news research desk</div><h1 className="page-title">Settings & limits</h1><p className="page-sub">Explicit sources, separate collection and enrichment bounds, and recorded application spending.</p></div><button className="btn btn-sm" type="button" disabled={pending} onClick={() => setRefresh((value) => value + 1)}>Refresh settings</button></div>
    <PersonalError error={error} stale={!!model} />
    {notice && <p className="personal-published-note" role="status">{notice}</p>}
    {!model && !error && <Card><EmptyState icon={Icon.settings} title="Reading settings" /></Card>}
    {model && draft && <><Card title="Feature readiness" icon={Icon.activity}><div className="stack">{Object.entries(model.features || {}).map(([name, item]) => <Row key={name} k={personalLabel(name)} v={personalLabel(item.state) + (item.reason ? " · " + personalLabel(item.reason) : "")} />)}</div></Card>
      {model.savedSuggestion && <Card title="Saved preferences · not applied" icon={Icon.info}><p className="personal-copy">{model.savedSuggestion.note}</p><ul>{(model.savedSuggestion.sources || []).map((source) => <li key={source.feedUrl}>{source.name}</li>)}</ul><p className="personal-copy">Preferred generation: {model.savedSuggestion.modelRoute && model.savedSuggestion.modelRoute.generation && model.savedSuggestion.modelRoute.generation.model}. Preferred embeddings: {model.savedSuggestion.modelRoute && model.savedSuggestion.modelRoute.embedding && model.savedSuggestion.modelRoute.embedding.model}. No ongoing allowance has been supplied.</p></Card>}
      <PersonalSourceDraftForm onAdded={(source) => setModel((current) => ({ ...current, availableSources: [...current.availableSources.filter((item) => item.id !== source.id), source] }))} />
      <form onSubmit={save} className="stack personal-settings-form"><Card title="Sources & interests" icon={Icon.link}>
        <p className="personal-copy">Select up to {draft.maxEnabledFeeds} enabled feeds. Removing a selection preserves its pending records and admitted articles.</p>
        <div className="stack">{model.availableSources.map((source) => <label className="personal-checkbox" key={source.id}><input type="checkbox" disabled={pending || (!source.active && !source.draft)} checked={draft.selectedSourceIds.includes(source.id)} onChange={(event) => change("selectedSourceIds", event.target.checked ? [...draft.selectedSourceIds, source.id] : draft.selectedSourceIds.filter((id) => id !== source.id))} /><span>{source.name}{source.draft ? " · draft, not enabled" : !source.active ? " · source inactive" : ""}<small style={{ display: "block", overflowWrap: "anywhere" }}>{source.feedUrl}</small></span></label>)}</div>
        {!model.availableSources.length && <p className="muted">No RSS sources are configured in this workspace.</p>}
        <div className="personal-filter"><label><span>Include phrases · one per line</span><textarea value={draft.includeText} rows={3} onChange={(event) => change("includeText", event.target.value)} /></label><label><span>Exclude phrases · one per line</span><textarea value={draft.excludeText} rows={3} onChange={(event) => change("excludeText", event.target.value)} /></label></div><p className="personal-copy">Empty inclusion means every selected-feed article matches. Exclusions win. Interest filters change Today and brief selection; capture and admission retain all eligible selected-feed items.</p>
      </Card><Card title="Collection & processing limits" icon={Icon.settings}><div className="personal-filter">
        <label><span>Workspace timezone</span><input value={draft.timezone} disabled={!model.timezoneEditable} onChange={(event) => change("timezone", event.target.value)} /></label>
        {[["dailyArticleLimit", "New articles per local day"], ["runArticleLimit", "New articles per run"], ["enrichmentArticleLimit", "Articles selected for enrichment per run"], ["maxEnabledFeeds", "Maximum selected feeds"]].map(([key, label]) => <label key={key}><span>{label}</span><input type="number" min={key === "maxEnabledFeeds" ? 1 : 0} max={key === "maxEnabledFeeds" ? 100 : 100000} value={draft[key]} onChange={(event) => change(key, Number(event.target.value))} /></label>)}
        <label><span>Processing profile</span><select value={draft.executionProfile} onChange={(event) => change("executionProfile", event.target.value)}><option value="raw">RSS reading only</option><option value="assisted">RSS with optional AI grouping and briefs</option></select></label></div>
        <p className="personal-copy">Timezone is locked after the first run. Existing runs retain their selected inputs and original ceilings; raising limits does not enlarge them.</p>
      </Card><Card title="Optional paid processing" icon={Icon.bolt}>
        <label className="personal-checkbox"><input type="checkbox" disabled={draft.executionProfile === "raw"} checked={draft.aiEnabled} onChange={(event) => change("aiEnabled", event.target.checked)} /><span>Explicitly enable paid AI processing within my configured allowance (requires the assisted profile)</span></label>
        <div className="personal-filter"><label><span>Permitted monthly USD allowance</span><input inputMode="decimal" value={draft.monthlyAllowanceUsd || ""} placeholder="No allowance supplied" onChange={(event) => change("monthlyAllowanceUsd", event.target.value)} /></label><label><span>Optional lower per-run USD ceiling</span><input inputMode="decimal" value={draft.runAllowanceUsd || ""} placeholder="Defaults to monthly allowance" onChange={(event) => change("runAllowanceUsd", event.target.value)} /></label></div>
        <p className="personal-copy">Disabled AI has a zero spending ceiling. Missing provider configuration or allowance blocks paid calls while RSS remains readable.</p>
        <PersonalRouteEditor route={draft.modelRoute} onChange={(value) => change("modelRoute", value)} />
      </Card><div className="personal-actions"><button className="btn btn-primary" type="submit" disabled={pending}>{pending ? "Saving…" : "Save settings"}</button><span className="muted">Current profile revision {model.profile && model.profile.revision}</span></div></form>
    </>}
    <PersonalError error={spendingError} stale={!!spending} />{spending && <PersonalSpending model={spending} timezone={model && model.timezone} />}
    <PersonalError error={collectionError} stale={!!collection} />{collection && <Card title="Recorded collection" icon={Icon.doc}><Row k="Observed candidates" v={collection.observedCandidates} /><Row k="Pending candidates" v={collection.pendingCandidates} /><Row k="Admitted articles" v={collection.admittedArticles} /><Row k="Grouped articles" v={collection.groupedArticles} /><p className="personal-copy">These are retained records, not estimated feed totals or completed-brief counts.</p><PersonalCaptureReceipts receipts={collection.receipts} sources={model && model.availableSources} timezone={model && model.timezone} /></Card>}
    {model && <PersonalBacklog timezone={model.timezone} refreshKey={refresh} />}<LocalAccessKey />
  </div>;
}

function PersonalSettingsPage() {
  return personalIsDemo() ? <div className="page personal-page"><Card><EmptyState icon={Icon.settings} title="Settings are unavailable in the read-only demo" hint="Switch to Real to inspect or explicitly update persistent personal settings." /></Card></div> : <RealPersonalSettingsPage />;
}

function UnavailablePersonalPage({ route }) {
  const labels = { geo: "Risk maps", risk: "Risk forecasts", riskdetail: "Risk forecasts", industries: "Industry research", industry: "Industry research", companies: "Company research", company: "Company research", historical: "Historical analogies", alerts: "External alerts", ask: "Ask AI", admin: "Administration", settings: "Settings" };
  const label = labels[route.name] || "This destination";
  return <div className="page personal-page"><Card><EmptyState icon={Icon.info} title="Unavailable in personal mode" hint={label + " is outside the current personal workflow."} action={<button className="btn btn-sm" onClick={() => Store.nav("today")}>Return to Today</button>} /></Card></div>;
}

Object.assign(window, { TodayPage, SavedPage, BriefsPage, PersonalEventDetail, PersonalBriefDetail, PersonalArticleDetail, PersonalSettingsPage, ReportEvidenceDrawer, UnavailablePersonalPage, LocalAccessKey, personalDateTime });
