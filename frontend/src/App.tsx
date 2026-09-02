import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiClientError } from "./api";
import type {
  AssetDetail,
  AssetSummary,
  HealthResponse,
  Overview,
  Page,
  QualityIssue,
  QualityStatus,
  SearchResponse,
  SearchResult,
  SqlQueryResponse,
  SqlSchemaResponse,
  TablePreview,
  Task,
  TextPreview,
} from "./types";

const STAGE_LABELS: Record<string, string> = {
  queued: "排队中",
  scan: "扫描",
  extract: "提取",
  clean: "清洗",
  profile: "画像",
  catalog: "目录",
  completed: "完成",
  failed: "失败",
};

const EMPTY_OVERVIEW: Overview = {
  files: 0,
  supported: 0,
  unsupported: 0,
  failed: 0,
  deferred: 0,
  tableAssets: 0,
  textAssets: 0,
  textChunks: 0,
  ready: 0,
  needsReview: 0,
  unusable: 0,
  qualityIssues: 0,
  openQualityIssues: 0,
  semanticPending: 0,
  semanticEnriched: 0,
  formats: {},
};

function number(value: number | null | undefined): string {
  return value == null ? "—" : new Intl.NumberFormat("zh-CN").format(value);
}

function errorText(error: unknown): string {
  if (error instanceof ApiClientError) return error.message;
  return error instanceof Error ? error.message : "本地服务暂时无法完成请求";
}

function qualityLabel(status: QualityStatus): string {
  return { ready: "Ready", needs_review: "Needs review", unusable: "Unusable" }[status] ?? status;
}

function typeLabel(type: "table" | "text"): string {
  return type === "table" ? "表格" : "文本";
}

function dimensions(asset: AssetSummary): string {
  if (asset.assetType === "table") return `${number(asset.rows)} × ${number(asset.columns)}`;
  return `${number(asset.chars)} 字符 · ${number(asset.chunks)} chunks`;
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

function App() {
  const [page, setPage] = useState<Page>("overview");
  const [overview, setOverview] = useState<Overview>(EMPTY_OVERVIEW);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [catalogOffset, setCatalogOffset] = useState(0);
  const [catalogType, setCatalogType] = useState<"" | "table" | "text">("");
  const [catalogQuality, setCatalogQuality] = useState<"" | QualityStatus>("");
  const [catalogFormat, setCatalogFormat] = useState("");
  const [catalogQuery, setCatalogQuery] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchTotal, setSearchTotal] = useState(0);
  const [searchOffset, setSearchOffset] = useState(0);
  const [searchType, setSearchType] = useState<"all" | "table" | "text">("all");
  const [searchQuality, setSearchQuality] = useState<"" | QualityStatus>("");
  const [searchFormat, setSearchFormat] = useState("");
  const [searchMatch, setSearchMatch] = useState<"all" | "phrase">("all");
  const [searchSubmitted, setSearchSubmitted] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  const [queryAssets, setQueryAssets] = useState<AssetSummary[]>([]);
  const [querySelectedIds, setQuerySelectedIds] = useState<string[]>([]);
  const [querySchema, setQuerySchema] = useState<SqlSchemaResponse | null>(null);
  const [querySql, setQuerySql] = useState("SELECT * FROM t1 LIMIT 20");
  const [queryResult, setQueryResult] = useState<SqlQueryResponse | null>(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState("");
  const [issues, setIssues] = useState<QualityIssue[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selected, setSelected] = useState<AssetDetail | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailTab, setDetailTab] = useState<"data" | "profile" | "quality" | "source" | "semantic">("data");
  const [tableLayer, setTableLayer] = useState<"raw" | "normalized">("normalized");
  const [tableOffset, setTableOffset] = useState(0);
  const [tablePreview, setTablePreview] = useState<TablePreview | null>(null);
  const [textPreview, setTextPreview] = useState<TextPreview | null>(null);
  const [taskSource, setTaskSource] = useState("");
  const [showProcess, setShowProcess] = useState(false);
  const [showSemanticConfirm, setShowSemanticConfirm] = useState(false);
  const [semanticEnriching, setSemanticEnriching] = useState(false);
  const [semanticNotice, setSemanticNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const refreshOverview = useCallback(async () => {
    try {
      setOverview(await api.overview());
    } catch (cause) {
      setError(errorText(cause));
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch (cause) {
      setError(errorText(cause));
    }
  }, []);

  const refreshCatalog = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: "25", offset: String(catalogOffset) });
      if (catalogType) params.set("type", catalogType);
      if (catalogQuality) params.set("quality", catalogQuality);
      if (catalogFormat) params.set("format", catalogFormat);
      if (catalogQuery.trim()) params.set("q", catalogQuery.trim());
      const result = await api.catalog(params);
      setAssets(result.items);
      setCatalogTotal(result.pagination.total);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setLoading(false);
    }
  }, [catalogFormat, catalogOffset, catalogQuality, catalogQuery, catalogType]);

  const refreshIssues = useCallback(async () => {
    try {
      const params = new URLSearchParams({ status: "open", limit: "100", offset: "0" });
      setIssues((await api.quality(params)).items);
    } catch (cause) {
      setError(errorText(cause));
    }
  }, []);

  const refreshTasks = useCallback(async () => {
    try {
      setTasks((await api.tasks()).items);
    } catch (cause) {
      setError(errorText(cause));
    }
  }, []);

  const refreshSearch = useCallback(async (offset = 0, value = searchQuery) => {
    if (!value.trim()) return;
    setSearchLoading(true);
    try {
      const params = new URLSearchParams({ q: value, type: searchType, match: searchMatch, limit: "30", offset: String(offset) });
      if (searchQuality) params.set("quality", searchQuality);
      if (searchFormat) params.set("format", searchFormat);
      const result: SearchResponse = await api.search(params);
      setSearchResults(result.results);
      setSearchTotal(result.total);
      setSearchOffset(result.offset);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setSearchLoading(false);
    }
  }, [searchFormat, searchMatch, searchQuality, searchQuery, searchType]);

  const refreshQueryAssets = useCallback(async () => {
    try {
      const result = await api.catalog(new URLSearchParams({ type: "table", limit: "100", offset: "0" }));
      setQueryAssets(result.items);
    } catch (cause) {
      setQueryError(errorText(cause));
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
    void refreshOverview();
    void refreshCatalog();
    void refreshIssues();
    void refreshTasks();
  }, [refreshCatalog, refreshHealth, refreshIssues, refreshOverview, refreshTasks]);

  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    setSelected(null);
    setTablePreview(null);
    setTextPreview(null);
    void api.asset(selectedId).then((value) => {
      if (active) setSelected(value);
    }).catch((cause) => {
      if (active) setError(errorText(cause));
    });
    return () => {
      active = false;
    };
  }, [selectedId]);

  useEffect(() => {
    if (!selected || selected.assetType !== "table" || detailTab !== "data") return;
    void api.tablePreview(selected.assetId, tableLayer, 20, tableOffset).then(setTablePreview).catch((cause) => setError(errorText(cause)));
  }, [detailTab, selected, tableLayer, tableOffset]);

  useEffect(() => {
    if (!selected || selected.assetType !== "text" || detailTab !== "data") return;
    void api.textPreview(selected.assetId, 8_000, 0).then(setTextPreview).catch((cause) => setError(errorText(cause)));
  }, [detailTab, selected]);

  useEffect(() => {
    const activeTasks = tasks.filter((task) => task.status === "queued" || task.status === "running");
    if (!activeTasks.length) return;
    const timer = window.setInterval(() => {
      void refreshTasks();
      void refreshOverview();
    }, 1_000);
    return () => window.clearInterval(timer);
  }, [refreshOverview, refreshTasks, tasks]);

  useEffect(() => {
    if (page === "query") void refreshQueryAssets();
  }, [page, refreshQueryAssets]);

  useEffect(() => {
    if (page !== "search" || !searchSubmitted || !searchQuery.trim()) return;
    setSearchOffset(0);
    void refreshSearch(0);
  }, [page, refreshSearch, searchQuery, searchSubmitted]);

  useEffect(() => {
    if (page !== "query" || !querySelectedIds.length) {
      setQuerySchema(null);
      return;
    }
    setQueryLoading(true);
    void api.querySchema(querySelectedIds)
      .then(setQuerySchema)
      .catch((cause) => setQueryError(errorText(cause)))
      .finally(() => setQueryLoading(false));
  }, [page, querySelectedIds]);

  const openAsset = (assetId: string) => {
    setSelectedId(assetId);
    setDetailTab("data");
    setSemanticNotice("");
    setPage("detail");
  };

  const openSearchResult = (result: SearchResult) => {
    openAsset(result.assetId);
  };

  const submitSearch = () => {
    const value = searchInput.trim();
    setSearchQuery(value);
    setSearchOffset(0);
    setSearchSubmitted(true);
    if (value) void refreshSearch(0, value);
    else {
      setSearchResults([]);
      setSearchTotal(0);
    }
  };

  const toggleQueryAsset = (assetId: string) => {
    setQueryResult(null);
    setQuerySelectedIds((current) => current.includes(assetId) ? current.filter((value) => value !== assetId) : [...current, assetId].slice(0, 8));
  };

  const runSql = async () => {
    setQueryError("");
    setQueryLoading(true);
    try {
      setQueryResult(await api.querySql(querySelectedIds, querySql));
    } catch (cause) {
      setQueryError(errorText(cause));
      setQueryResult(null);
    } finally {
      setQueryLoading(false);
    }
  };

  const startProcess = async () => {
    setError("");
    try {
      const result = await api.process(taskSource.trim());
      setShowProcess(false);
      setTaskSource("");
      setPage("tasks");
      setTasks((current) => [result.task, ...current.filter((item) => item.taskId !== result.taskId)]);
      void refreshOverview();
    } catch (cause) {
      setError(errorText(cause));
    }
  };

  const updateIssue = async (issue: QualityIssue, status: QualityIssue["status"]) => {
    try {
      await api.updateQuality(issue.issue_id, status);
      await Promise.all([refreshIssues(), refreshOverview()]);
      if (selectedId) {
        const detail = await api.asset(selectedId);
        setSelected(detail);
      }
    } catch (cause) {
      setError(errorText(cause));
    }
  };

  const requestSemanticEnrichment = () => {
    if (!selected || !health?.llm.configured || semanticEnriching) return;
    setSemanticNotice("");
    setShowSemanticConfirm(true);
  };

  const confirmSemanticEnrichment = async () => {
    if (!selectedId) return;
    setShowSemanticConfirm(false);
    setSemanticEnriching(true);
    setError("");
    try {
      const result = await api.semanticEnrich(selectedId);
      setSelected(result.asset);
      setSemanticNotice(result.reused ? "已使用现有 AI 整理结果" : "AI 整理完成");
      await Promise.all([refreshOverview(), refreshCatalog()]);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setSemanticEnriching(false);
    }
  };

  const resetCatalog = (next: () => void) => {
    setCatalogOffset(0);
    next();
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark">重</div>
        <div className="brand-copy">
          <div className="brand-name">ChongZu</div>
          <div className="brand-subtitle">科研资料工作台</div>
        </div>
        <nav className="main-nav" aria-label="主导航">
          <NavButton active={page === "overview"} onClick={() => setPage("overview")}>概览</NavButton>
          <NavButton active={page === "catalog" || page === "detail"} onClick={() => setPage("catalog")}>数据目录</NavButton>
          <NavButton active={page === "search"} onClick={() => setPage("search")}>数据检索</NavButton>
          <NavButton active={page === "query"} onClick={() => setPage("query")}>数据查询</NavButton>
          <NavButton active={page === "quality"} onClick={() => setPage("quality")}>质量检查{overview.openQualityIssues ? <span className="nav-count">{overview.openQualityIssues}</span> : null}</NavButton>
          <NavButton active={page === "tasks"} onClick={() => setPage("tasks")}>处理任务</NavButton>
        </nav>
        <div className="topbar-status"><span className="status-dot" />本地离线模式</div>
      </header>

      <main className="main-content">
        {error ? <div className="error-banner" role="alert"><span>{error}</span><button onClick={() => setError("")}>关闭</button></div> : null}
        {page === "overview" ? <OverviewPage overview={overview} health={health} tasks={tasks} onProcess={() => setShowProcess(true)} onNavigate={setPage} onOpenAsset={openAsset} /> : null}
        {page === "catalog" ? <CatalogPage assets={assets} total={catalogTotal} offset={catalogOffset} loading={loading} type={catalogType} quality={catalogQuality} format={catalogFormat} query={catalogQuery} formats={overview.formats} onType={(value) => resetCatalog(() => setCatalogType(value))} onQuality={(value) => resetCatalog(() => setCatalogQuality(value))} onFormat={(value) => resetCatalog(() => setCatalogFormat(value))} onQuery={(value) => { setCatalogOffset(0); setCatalogQuery(value); }} onOffset={setCatalogOffset} onOpen={openAsset} onProcess={() => setShowProcess(true)} /> : null}
        {page === "search" ? <SearchPage input={searchInput} query={searchQuery} results={searchResults} total={searchTotal} offset={searchOffset} loading={searchLoading} submitted={searchSubmitted} hasAssets={overview.tableAssets + overview.textAssets > 0} type={searchType} quality={searchQuality} format={searchFormat} match={searchMatch} formats={overview.formats} onInput={setSearchInput} onSubmit={submitSearch} onType={(value) => { setSearchType(value); setSearchOffset(0); }} onQuality={(value) => { setSearchQuality(value); setSearchOffset(0); }} onFormat={(value) => { setSearchFormat(value); setSearchOffset(0); }} onMatch={(value) => { setSearchMatch(value); setSearchOffset(0); }} onOffset={(value) => { setSearchOffset(value); void refreshSearch(value); }} onOpen={openSearchResult} /> : null}
        {page === "query" ? <QueryPage assets={queryAssets} selectedIds={querySelectedIds} schema={querySchema} sql={querySql} result={queryResult} loading={queryLoading} error={queryError} onToggle={toggleQueryAsset} onSql={setQuerySql} onRun={runSql} onOpen={openAsset} /> : null}
        {page === "quality" ? <QualityPage issues={issues} onUpdate={updateIssue} onOpen={openAsset} /> : null}
        {page === "tasks" ? <TasksPage tasks={tasks} onProcess={() => setShowProcess(true)} onOpenCatalog={() => setPage("catalog")} /> : null}
        {page === "detail" && selected ? <DetailPage detail={selected} tab={detailTab} onTab={setDetailTab} tableLayer={tableLayer} onTableLayer={setTableLayer} tableOffset={tableOffset} onTableOffset={setTableOffset} tablePreview={tablePreview} textPreview={textPreview} semanticConfigured={health?.llm.configured === true} semanticEnriching={semanticEnriching} semanticNotice={semanticNotice} onSemanticEnrich={requestSemanticEnrichment} onBack={() => setPage("catalog")} onUpdateIssue={updateIssue} /> : null}
        {page === "detail" && !selected ? <EmptyState title="正在加载资产" body="正在读取本地目录与画像信息。" /> : null}
      </main>

      {showProcess ? <ProcessDialog source={taskSource} onSource={setTaskSource} onClose={() => setShowProcess(false)} onSubmit={startProcess} /> : null}
      {showSemanticConfirm && selected ? <SemanticConfirmDialog assetName={selected.displayName} onCancel={() => setShowSemanticConfirm(false)} onConfirm={() => void confirmSemanticEnrichment()} /> : null}
    </div>
  );
}

function NavButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button className={`nav-button ${active ? "active" : ""}`} onClick={onClick}>{children}</button>;
}

function OverviewPage({ overview, health, tasks, onProcess, onNavigate, onOpenAsset }: { overview: Overview; health: HealthResponse | null; tasks: Task[]; onProcess: () => void; onNavigate: (page: Page) => void; onOpenAsset: (id: string) => void }) {
  const totalAssets = overview.tableAssets + overview.textAssets;
  const formatItems = Object.entries(overview.formats);
  return <section className="page-section">
    <div className="page-heading"><div><div className="eyebrow">PROJECT OVERVIEW</div><h1>项目资料</h1><p className="heading-note">本地目录处理状态与数据资产概览</p></div><button className="primary-button" onClick={onProcess}>处理新目录 <span>＋</span></button></div>
    <div className="summary-line"><strong>{number(overview.files)}</strong> 个文件 <span>·</span> <strong>{number(overview.tableAssets)}</strong> 个表格资产 <span>·</span> <strong>{number(overview.textAssets)}</strong> 个文本资产</div>
    <div className="overview-grid">
      <section className="panel status-panel"><PanelTitle title="处理状态" action={<button className="text-button" onClick={() => onNavigate("tasks")}>查看任务 →</button>} /><div className="status-grid"><Metric label="Ready" value={overview.ready} tone="good" /><Metric label="Needs review" value={overview.needsReview} tone="review" /><Metric label="失败 / Unusable" value={overview.failed + overview.unusable} tone="bad" /><Metric label="Unsupported" value={overview.unsupported} tone="muted" /></div></section>
      <section className="panel composition-panel"><PanelTitle title="资产组成" /><div className="composition-bar"><span className="bar-table" style={{ width: `${totalAssets ? (overview.tableAssets / totalAssets) * 100 : 0}%` }} /><span className="bar-text" style={{ width: `${totalAssets ? (overview.textAssets / totalAssets) * 100 : 0}%` }} /></div><div className="legend"><span><i className="legend-dot bar-table" />表格 {number(overview.tableAssets)}</span><span><i className="legend-dot bar-text" />文本 {number(overview.textAssets)}</span></div><div className="format-list">{formatItems.length ? formatItems.slice(0, 6).map(([format, count]) => <span key={format}><b>{format.toUpperCase()}</b>{number(count)}</span>) : <span className="muted">尚未处理资料</span>}</div></section>
    </div>
    <section className="panel recent-panel"><PanelTitle title="最近任务" action={<button className="text-button" onClick={() => onNavigate("tasks")}>全部任务 →</button>} />{tasks.length ? <div className="task-table">{tasks.slice(0, 5).map((task) => <TaskRow key={task.taskId} task={task} />)}</div> : <EmptyState title="尚未处理资料" body="输入一个本地科研资料目录，开始建立数据目录。" compact />}</section>
    <div className="info-strip"><span className="info-icon">i</span><span>AI 语义分析：<strong>{health?.llm.configured ? "provider 已配置" : "尚未配置模型"}</strong>。默认处理完全离线；语义整理仅由显式命令触发。</span></div>
  </section>;
}

function Metric({ label, value, tone }: { label: string; value: number; tone: "good" | "review" | "bad" | "muted" }) {
  return <div className="metric"><div className={`metric-value ${tone}`}>{number(value)}</div><div className="metric-label">{label}</div></div>;
}

function PanelTitle({ title, action }: { title: string; action?: React.ReactNode }) { return <div className="panel-title"><h2>{title}</h2>{action}</div>; }

function CatalogPage({ assets, total, offset, loading, type, quality, format, query, formats, onType, onQuality, onFormat, onQuery, onOffset, onOpen, onProcess }: { assets: AssetSummary[]; total: number; offset: number; loading: boolean; type: "" | "table" | "text"; quality: "" | QualityStatus; format: string; query: string; formats: Record<string, number>; onType: (value: "" | "table" | "text") => void; onQuality: (value: "" | QualityStatus) => void; onFormat: (value: string) => void; onQuery: (value: string) => void; onOffset: (value: number) => void; onOpen: (id: string) => void; onProcess: () => void }) {
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">DATA CATALOG</div><h1>数据目录</h1><p className="heading-note">每个表格与文本资产独立保留来源、清洗和质量边界</p></div><button className="primary-button" onClick={onProcess}>处理新目录 <span>＋</span></button></div>
    <div className="filter-panel"><div className="segmented"><button className={!type ? "selected" : ""} onClick={() => onType("")}>全部</button><button className={type === "table" ? "selected" : ""} onClick={() => onType("table")}>表格</button><button className={type === "text" ? "selected" : ""} onClick={() => onType("text")}>文本</button></div><select value={quality} onChange={(event) => onQuality(event.target.value as "" | QualityStatus)}><option value="">全部质量</option><option value="ready">Ready</option><option value="needs_review">Needs review</option><option value="unusable">Unusable</option></select><select value={format} onChange={(event) => onFormat(event.target.value)}><option value="">全部格式</option>{Object.keys(formats).sort().map((item) => <option key={item} value={item}>{item.toUpperCase()}</option>)}</select><label className="search-box"><span>⌕</span><input value={query} onChange={(event) => onQuery(event.target.value)} placeholder="搜索资产名或来源文件" /></label></div>
    <div className="list-meta"><span>{loading ? "读取中…" : `${number(total)} 个资产`}</span><span>只搜索来源名称与文件名</span></div>{assets.length ? <div className="asset-list">{assets.map((asset) => <AssetListItem asset={asset} key={asset.assetId} onOpen={onOpen} />)}</div> : <EmptyState title="暂无数据资产" body="处理一个本地资料目录后，表格与文本资产会出现在这里。" />}
    {total > 25 ? <Pagination offset={offset} limit={25} total={total} onOffset={onOffset} /> : null}</section>;
}

function AssetListItem({ asset, onOpen }: { asset: AssetSummary; onOpen: (id: string) => void }) {
  return <button className="asset-list-item" onClick={() => onOpen(asset.assetId)}><div className={`asset-type-mark ${asset.assetType}`}>{asset.assetType === "table" ? "表" : "文"}</div><div className="asset-main"><div className="asset-name">{asset.effectiveDisplayName || asset.fallbackDisplayName}</div><div className="asset-source">{asset.source.relativePath} <span>·</span> {asset.source.format?.toUpperCase() || "UNKNOWN"}</div></div><div className="asset-size">{dimensions(asset)}</div><div className={`quality-pill ${asset.qualityStatus}`}>{qualityLabel(asset.qualityStatus)}</div><div className="asset-extractor">{asset.extractor}</div><div className={`semantic-pill ${asset.semanticStatus}`}>{asset.semanticStatus === "enriched" ? "AI 已整理" : "AI 未整理"}</div><span className="chevron">›</span></button>;
}

function QualityPage({ issues, onUpdate, onOpen }: { issues: QualityIssue[]; onUpdate: (issue: QualityIssue, status: QualityIssue["status"]) => void; onOpen: (id: string) => void }) {
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">QUALITY REVIEW</div><h1>质量检查</h1><p className="heading-note">只处理审阅状态，不会修改 raw 或 normalized 数据</p></div><div className="open-count">{number(issues.length)} 个待处理</div></div>{issues.length ? <div className="quality-list">{issues.map((issue) => <IssueCard key={issue.issue_id} issue={issue} onUpdate={onUpdate} onOpen={onOpen} />)}</div> : <EmptyState title="暂无待处理问题" body="当前没有 open 状态的质量问题。" />}</section>;
}

function IssueCard({ issue, onUpdate, onOpen }: { issue: QualityIssue; onUpdate: (issue: QualityIssue, status: QualityIssue["status"]) => void; onOpen: (id: string) => void }) {
  return <article className="issue-card"><div className={`severity-mark ${issue.severity}`} /> <div className="issue-content"><div className="issue-top"><span className={`severity-label ${issue.severity}`}>{issue.severity}</span><button className="issue-asset" onClick={() => onOpen(issue.asset_id)}>{issue.effective_display_name || issue.fallback_display_name || issue.asset_id}</button><span className="issue-time">{formatDate(issue.created_at)}</span></div><h3>{issue.issue_type}</h3><p>{issue.description}</p><div className="evidence">证据：{compactValue(issue.evidence)}</div><div className="suggestion">建议：{issue.suggested_action}</div><div className="issue-actions"><button onClick={() => onUpdate(issue, "accepted")}>接受</button><button onClick={() => onUpdate(issue, "resolved")}>标记已解决</button><button className="quiet" onClick={() => onUpdate(issue, "ignored")}>忽略</button></div></div></article>;
}

function TasksPage({ tasks, onProcess, onOpenCatalog }: { tasks: Task[]; onProcess: () => void; onOpenCatalog: () => void }) {
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">PROCESSING TASKS</div><h1>处理任务</h1><p className="heading-note">后台任务每秒更新一次；核心流水线继续保持逐文件隔离</p></div><button className="primary-button" onClick={onProcess}>处理新目录 <span>＋</span></button></div>{tasks.length ? <div className="task-list">{tasks.map((task) => <TaskCard key={task.taskId} task={task} onOpenCatalog={onOpenCatalog} />)}</div> : <EmptyState title="尚无处理任务" body="从一个本地目录开始，建立第一个数据目录。" />}</section>;
}

function TaskRow({ task }: { task: Task }) { return <div className="task-row"><span className={`task-status-dot ${task.status}`} /><div className="task-row-source">{task.source}</div><div className="task-row-stage">{STAGE_LABELS[task.currentStage] || task.currentStage}</div><div className="task-row-progress"><span style={{ width: `${task.progress * 100}%` }} /></div><div className="task-row-status">{task.status === "succeeded" ? "完成" : task.status === "failed" ? "失败" : `${Math.round(task.progress * 100)}%`}</div></div>; }

function TaskCard({ task, onOpenCatalog }: { task: Task; onOpenCatalog: () => void }) { return <article className="task-card"><div className="task-card-header"><div><span className={`task-status-dot ${task.status}`} /> <strong>{task.status === "succeeded" ? "处理完成" : task.status === "failed" ? "处理失败" : "处理中"}</strong></div><span className="task-time">{formatDate(task.startedAt)}</span></div><div className="task-source">{task.source}</div><div className="progress-track"><span style={{ width: `${task.progress * 100}%` }} /></div><div className="task-card-footer"><span>{STAGE_LABELS[task.currentStage] || task.currentStage} · {Math.round(task.progress * 100)}%</span>{task.status === "succeeded" ? <button className="text-button" onClick={onOpenCatalog}>查看资产 →</button> : null}{task.errorSummary ? <span className="task-error">{task.errorSummary}</span> : null}</div>{task.counts.tableAssets != null ? <div className="task-counts"><span>文件 {number(task.counts.filesDiscovered)}</span><span>表格 {number(task.counts.tableAssets)}</span><span>文本 {number(task.counts.textAssets)}</span><span>质量问题 {number(task.counts.qualityIssues)}</span></div> : null}</article>; }

type SearchPageProps = {
  input: string;
  query: string;
  results: SearchResult[];
  total: number;
  offset: number;
  loading: boolean;
  submitted: boolean;
  hasAssets: boolean;
  type: "all" | "table" | "text";
  quality: "" | QualityStatus;
  format: string;
  match: "all" | "phrase";
  formats: Record<string, number>;
  onInput: (value: string) => void;
  onSubmit: () => void;
  onType: (value: "all" | "table" | "text") => void;
  onQuality: (value: "" | QualityStatus) => void;
  onFormat: (value: string) => void;
  onMatch: (value: "all" | "phrase") => void;
  onOffset: (value: number) => void;
  onOpen: (result: SearchResult) => void;
};

function SearchPage({ input, query, results, total, offset, loading, submitted, hasAssets, type, quality, format, match, formats, onInput, onSubmit, onType, onQuality, onFormat, onMatch, onOffset, onOpen }: SearchPageProps) {
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">LOCAL RETRIEVAL</div><h1>数据检索</h1><p className="heading-note">本地 lexical 检索：文件、表格元数据与 TextChunk；不使用模型改写或 rerank</p></div></div>
    <form className="search-command" onSubmit={(event) => { event.preventDefault(); onSubmit(); }}><span className="search-command-icon">⌕</span><input aria-label="搜索资料" value={input} onChange={(event) => onInput(event.target.value)} placeholder="搜索资料、表格、正文、列名……" /><button className="primary-button" type="submit">搜索</button></form>
    <div className="filter-panel search-filters"><div className="segmented"><button className={type === "all" ? "selected" : ""} onClick={() => onType("all")} type="button">全部</button><button className={type === "table" ? "selected" : ""} onClick={() => onType("table")} type="button">表格</button><button className={type === "text" ? "selected" : ""} onClick={() => onType("text")} type="button">文本</button></div><select value={quality} onChange={(event) => onQuality(event.target.value as "" | QualityStatus)}><option value="">全部质量</option><option value="ready">Ready</option><option value="needs_review">Needs review</option><option value="unusable">Unusable</option></select><select value={format} onChange={(event) => onFormat(event.target.value)}><option value="">全部格式</option>{Object.keys(formats).sort().map((item) => <option key={item} value={item}>{item.toUpperCase()}</option>)}</select><select value={match} onChange={(event) => onMatch(event.target.value as "all" | "phrase")}><option value="all">按词匹配</option><option value="phrase">完整短语</option></select></div>
    {loading ? <div className="loading-box search-loading">正在检索本地目录……</div> : !submitted ? <EmptyState title="尚未输入搜索内容" body="输入关键词后，ChongZu 会在本地目录与 TextChunk 中检索。" /> : !query ? <EmptyState title="尚未输入搜索内容" body="搜索框为空；不会执行全库扫描。" /> : !hasAssets ? <EmptyState title="当前尚未处理任何资料" body="先处理一个本地资料目录，建立 Catalog 后再进行检索。" /> : !results.length ? <EmptyState title="没有找到匹配结果" body={`没有找到与“${query}”匹配的资料、列名或正文片段。`} /> : <><div className="list-meta"><span>{number(total)} 个结果</span><span>结果按本地确定性分数排序；每个资产最多显示 3 条</span></div><div className="search-results">{results.map((result) => <SearchResultItem key={result.resultId} result={result} onOpen={onOpen} />)}</div>{total > 30 ? <Pagination offset={offset} limit={30} total={total} onOffset={onOffset} /> : null}</>}
  </section>;
}

function SearchResultItem({ result, onOpen }: { result: SearchResult; onOpen: (result: SearchResult) => void }) {
  const location = [result.pageNumber != null ? `Page ${result.pageNumber}` : "", result.sheetName ? `Sheet ${result.sheetName}` : ""].filter(Boolean).join(" / ");
  return <button className="search-result" onClick={() => onOpen(result)}><div className={`asset-type-mark ${result.assetType}`}>{result.assetType === "table" ? "表" : "文"}</div><div className="search-result-main"><div className="search-result-title">{result.displayName}</div><div className="search-result-source">{result.sourceFile}{location ? ` · ${location}` : ""} · {result.sourceFormat?.toUpperCase() || "UNKNOWN"}</div><div className="search-snippet"><HighlightedSnippet result={result} /></div></div><div className="search-result-meta"><span className="search-match-kind">{result.matchKind}</span><span className={`quality-pill ${result.qualityStatus}`}>{qualityLabel(result.qualityStatus)}</span><span className="search-score">{result.score.toFixed(1)}</span></div><span className="chevron">›</span></button>;
}

function HighlightedSnippet({ result }: { result: SearchResult }) {
  const ranges = result.matchOffsets.filter((range) => range.length === 2 && range[1] > range[0]).sort((left, right) => left[0] - right[0]);
  if (!ranges.length) return <>{result.snippet}</>;
  const pieces: React.ReactNode[] = [];
  let cursor = 0;
  ranges.forEach(([start, end], index) => {
    const safeStart = Math.max(cursor, Math.min(start, result.snippet.length));
    const safeEnd = Math.max(safeStart, Math.min(end, result.snippet.length));
    if (safeStart > cursor) pieces.push(<span key={`text-${index}`}>{result.snippet.slice(cursor, safeStart)}</span>);
    if (safeEnd > safeStart) pieces.push(<mark key={`mark-${index}`}>{result.snippet.slice(safeStart, safeEnd)}</mark>);
    cursor = safeEnd;
  });
  if (cursor < result.snippet.length) pieces.push(<span key="tail">{result.snippet.slice(cursor)}</span>);
  return <>{pieces}</>;
}

type QueryPageProps = {
  assets: AssetSummary[];
  selectedIds: string[];
  schema: SqlSchemaResponse | null;
  sql: string;
  result: SqlQueryResponse | null;
  loading: boolean;
  error: string;
  onToggle: (assetId: string) => void;
  onSql: (value: string) => void;
  onRun: () => void;
  onOpen: (assetId: string) => void;
};

function QueryPage({ assets, selectedIds, schema, sql, result, loading, error, onToggle, onSql, onRun, onOpen }: QueryPageProps) {
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">SAFE SQL WORKBENCH</div><h1>数据查询</h1><p className="heading-note">只读查询专用内存连接；只能访问明确选中的 normalized TableAsset</p></div></div>
    <div className="query-layout"><aside className="query-sidebar panel"><div className="panel-title"><h2>选择表格资产</h2><span>{selectedIds.length} / {schema?.limits.maxSelectedAssets ?? 8}</span></div>{assets.length ? <div className="query-asset-list">{assets.map((asset) => <label className="query-asset" key={asset.assetId}><input type="checkbox" checked={selectedIds.includes(asset.assetId)} onChange={() => onToggle(asset.assetId)} /><span className="query-asset-copy"><strong>{asset.effectiveDisplayName || asset.fallbackDisplayName}</strong><small>{asset.source.relativePath} · {dimensions(asset)}</small></span><button type="button" className="text-button" onClick={() => onOpen(asset.assetId)}>查看</button></label>)}</div> : <EmptyState compact title="暂无表格资产" body="先处理一个本地资料目录。" />}</aside>
      <div className="query-workspace">{error ? <div className="query-error" role="alert">{error}</div> : null}<div className="panel query-panel"><div className="panel-title"><div><h2>SQL</h2><span className="panel-note">禁止 DDL/DML、文件函数、扩展和多语句；结果最多 500 行；选中表总行数超过 {schema?.limits.maxInputRows ?? 50000} 时会拒绝执行</span></div><button className="primary-button" disabled={!selectedIds.length || !sql.trim() || loading} onClick={onRun}>{loading ? "执行中…" : "运行查询"}</button></div>{schema?.relations.length ? <div className="relation-map">{schema.relations.map((relation) => <div className="relation-chip" key={relation.alias}><b>{relation.alias}</b><span>{relation.displayName}</span></div>)}</div> : <div className="query-hint">请选择 1～8 个表格资产，系统会映射为 t1、t2……</div>}<textarea className="sql-editor" value={sql} onChange={(event) => onSql(event.target.value)} spellCheck={false} aria-label="SQL 查询" placeholder="SELECT * FROM t1 LIMIT 20" />{result ? <QueryResult result={result} /> : <div className="query-empty">查询结果会显示在这里。当前不会自动生成 SQL。</div>}</div></div>
    </div>
  </section>;
}

function QueryResult({ result }: { result: SqlQueryResponse }) {
  return <div className="query-result"><div className="query-result-meta"><span>{number(result.rowCount)} 行 · {result.executionMs.toFixed(1)} ms</span>{result.truncated ? <strong>结果已截断（受本地上限限制）</strong> : null}<span className="sandbox-label">{result.sandbox}</span></div><div className="table-wrap"><table><thead><tr>{result.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{result.rows.map((row, index) => <tr key={index}>{result.columns.map((column) => <td key={column} title={displayValue(row[column])}>{displayValue(row[column])}</td>)}</tr>)}</tbody></table></div></div>;
}

function DetailPage({ detail, tab, onTab, tableLayer, onTableLayer, tableOffset, onTableOffset, tablePreview, textPreview, semanticConfigured, semanticEnriching, semanticNotice, onSemanticEnrich, onBack, onUpdateIssue }: { detail: AssetDetail; tab: "data" | "profile" | "quality" | "source" | "semantic"; onTab: (value: "data" | "profile" | "quality" | "source" | "semantic") => void; tableLayer: "raw" | "normalized"; onTableLayer: (value: "raw" | "normalized") => void; tableOffset: number; onTableOffset: (value: number) => void; tablePreview: TablePreview | null; textPreview: TextPreview | null; semanticConfigured: boolean; semanticEnriching: boolean; semanticNotice: string; onSemanticEnrich: () => void; onBack: () => void; onUpdateIssue: (issue: QualityIssue, status: QualityIssue["status"]) => void }) {
  return <section className="page-section detail-section"><button className="back-button" onClick={onBack}>← 数据目录</button><div className="detail-heading"><div className={`asset-type-mark large ${detail.assetType}`}>{detail.assetType === "table" ? "表" : "文"}</div><div><div className="eyebrow">{typeLabel(detail.assetType)}资产</div><h1>{detail.displayName}</h1><div className="detail-source">{detail.source.relativePath} <span>·</span> {detail.source.format?.toUpperCase()}</div></div><div className={`quality-pill ${detail.qualityStatus}`}>{qualityLabel(detail.qualityStatus)}</div></div><div className="detail-tabs">{([["data", "数据"], ["profile", "画像"], ["quality", `质量${detail.qualityIssues.length ? ` · ${detail.qualityIssues.length}` : ""}`], ["source", "来源"], ["semantic", "AI语义"]] as const).map(([key, label]) => <button key={key} className={tab === key ? "active" : ""} onClick={() => onTab(key)}>{label}</button>)}</div>{tab === "data" ? detail.assetType === "table" ? <TableData detail={detail} layer={tableLayer} onLayer={onTableLayer} offset={tableOffset} onOffset={onTableOffset} preview={tablePreview} /> : <TextData detail={detail} preview={textPreview} /> : null}{tab === "profile" ? <ProfileView detail={detail} /> : null}{tab === "quality" ? <DetailQuality detail={detail} onUpdate={onUpdateIssue} /> : null}{tab === "source" ? <SourceView detail={detail} /> : null}{tab === "semantic" ? <SemanticView detail={detail} configured={semanticConfigured} enriching={semanticEnriching} notice={semanticNotice} onEnrich={onSemanticEnrich} /> : null}</section>;
}

function TableData({ detail, layer, onLayer, offset, onOffset, preview }: { detail: AssetDetail; layer: "raw" | "normalized"; onLayer: (value: "raw" | "normalized") => void; offset: number; onOffset: (value: number) => void; preview: TablePreview | null }) { return <div className="detail-panel"><div className="data-toolbar"><div><h2>表格预览</h2><p>默认查看 normalized；raw artifact 保持不变</p></div><div className="segmented"><button className={layer === "normalized" ? "selected" : ""} onClick={() => { onLayer("normalized"); onOffset(0); }}>Normalized</button><button className={layer === "raw" ? "selected" : ""} onClick={() => { onLayer("raw"); onOffset(0); }}>Raw</button></div></div>{preview ? <><div className="table-wrap"><table><thead><tr><th className="row-number">#</th>{preview.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{preview.rows.map((row, index) => <tr key={`${offset}-${index}`}><td className="row-number">{offset + index + 1}</td>{preview.columns.map((column) => <td key={column} title={displayValue(row[column])}>{displayValue(row[column])}</td>)}</tr>)}</tbody></table></div><Pagination offset={offset} limit={20} total={preview.pagination.total ?? detail.dimensions.rows ?? 0} onOffset={onOffset} /></> : <div className="loading-box">读取表格预览…</div>}</div>; }

function TextData({ detail, preview }: { detail: AssetDetail; preview: TextPreview | null }) { return <div className="detail-panel"><div className="data-toolbar"><div><h2>文本预览</h2><p>{detail.provenance.sourceKind} · {number(detail.dimensions.chunks)} chunks · {number(detail.dimensions.chars)} 字符</p></div><span className="source-badge">{detail.provenance.extractor.toLowerCase().includes("ocr") ? "OCR" : "Native"}</span></div>{preview ? <><div className="text-preview" aria-label="normalized text preview">{preview.text || "（空文本）"}</div><div className="preview-foot">显示 {number(preview.offset)}–{number(preview.offset + preview.text.length)} / {number(preview.chars)} 字符 · {preview.layer}</div></> : <div className="loading-box">读取文本预览…</div>}</div>; }

function ProfileView({ detail }: { detail: AssetDetail }) { const profile = detail.profile; if (!profile) return <EmptyState title="暂无画像" body="该资产尚未完成 deterministic profiling。" />; const columns = Array.isArray(profile.columns) ? profile.columns as Array<Record<string, unknown>> : []; return <div className="detail-panel"><div className="data-toolbar"><div><h2>确定性画像</h2><p>profile 只描述数据，不改变 raw / normalized artifact</p></div></div><div className="profile-summary"><span>行 {number(Number(profile.row_count ?? profile.char_count ?? 0))}</span><span>列 {number(Number(profile.column_count ?? 0))}</span><span>空值 {number(Number(profile.null_count ?? 0))}</span><span>版本 {String(profile.profile_version ?? "—")}</span></div>{columns.length ? <div className="profile-columns">{columns.map((column) => <div className="profile-column" key={String(column.name)}><div><strong>{String(column.name)}</strong><span>{String(column.inferred_physical_type ?? "string")}</span></div><div className="profile-column-stats"><span>null {number(Number(column.null_count ?? 0))} · distinct {number(Number(column.distinct_count ?? 0))}</span><span>sample: {compactValue(column.sample_values)}</span></div></div>)}</div> : <pre className="json-preview">{JSON.stringify(profile, null, 2)}</pre>}</div>; }

function DetailQuality({ detail, onUpdate }: { detail: AssetDetail; onUpdate: (issue: QualityIssue, status: QualityIssue["status"]) => void }) { return <div className="detail-panel">{detail.qualityIssues.length ? <div className="quality-list compact">{detail.qualityIssues.map((issue) => <IssueCard key={issue.issue_id} issue={issue} onUpdate={onUpdate} onOpen={() => undefined} />)}</div> : <EmptyState title="暂无质量问题" body="这个资产目前没有记录的质量问题。" />}</div>; }

function SourceView({ detail }: { detail: AssetDetail }) { return <div className="detail-panel"><div className="source-grid"><SourceField label="来源文件" value={detail.source.relativePath} /><SourceField label="格式" value={detail.source.format?.toUpperCase()} /><SourceField label="SHA-256" value={detail.source.sha256} wide /><SourceField label="file_id" value={detail.source.fileId} wide /><SourceField label="extractor" value={`${detail.provenance.extractor} · ${detail.provenance.extractorVersion}`} /><SourceField label="extraction run" value={detail.provenance.extractionRunId} /><SourceField label="sheet / page" value={[detail.provenance.sheetName, detail.provenance.pageNumber ? `Page ${detail.provenance.pageNumber}` : null].filter(Boolean).join(" / ") || "—"} /><SourceField label="source range" value={compactValue(detail.provenance.sourceRange)} wide /><SourceField label="raw artifact" value={detail.artifacts.raw} wide /><SourceField label="normalized artifact" value={detail.artifacts.normalized} wide /><SourceField label="profile" value={detail.artifacts.profile} wide /></div></div>; }

function SourceField({ label, value, wide }: { label: string; value: unknown; wide?: boolean }) { return <div className={`source-field ${wide ? "wide" : ""}`}><span>{label}</span><code>{String(value ?? "—")}</code></div>; }

function SemanticView({ detail, configured, enriching, notice, onEnrich }: { detail: AssetDetail; configured: boolean; enriching: boolean; notice: string; onEnrich: () => void }) {
  const semantic = detail.semantic;
  const action = configured ? <button className="primary-button semantic-action" disabled={enriching} onClick={onEnrich}>{enriching ? "整理中..." : semantic && detail.semanticStatus === "enriched" ? "再次 AI 整理" : "AI 整理"}</button> : null;
  if (!semantic || detail.semanticStatus !== "enriched") {
    return <div className="detail-panel semantic-empty"><div className="semantic-lock">AI</div><h2>{configured ? "尚未进行 AI 整理" : "尚未配置 AI 模型"}</h2><p>{configured ? "当前资产还没有 SemanticMetadata。" : "配置 provider 后，才能手动发起单 Asset AI 整理。"}</p>{action}<span>Semantic status: pending</span></div>;
  }
  const fields = Array.isArray(semantic.semanticFields) ? semantic.semanticFields : [];
  return <div className="detail-panel semantic-panel">
    <div className="semantic-header"><div><div className="eyebrow">SEMANTIC METADATA</div><h2>{semantic.display_name}</h2></div><div className="semantic-header-actions">{action}<span className="semantic-confidence">confidence {semantic.confidence.toFixed(2)}</span></div></div>
    {notice ? <div className="semantic-notice" role="status">{notice}</div> : null}
    <div className="semantic-facts"><div><span>category</span><strong>{semantic.category}</strong></div><div><span>model</span><strong>{semantic.model}</strong></div><div><span>prompt</span><strong>{semantic.prompt_version}</strong></div><div><span>run</span><strong>{semantic.semantic_run_id}</strong></div></div>
    <div className="semantic-copy"><h3>description</h3><p>{semantic.description}</p><h3>summary</h3><p>{semantic.summary}</p><h3>keywords</h3><div className="keyword-list">{semantic.keywords.map((keyword) => <span key={keyword}>{keyword}</span>)}</div></div>
    {detail.assetType === "table" ? <><h3 className="semantic-subheading">semantic fields</h3>{fields.length ? <div className="table-wrap"><table><thead><tr><th>source column</th><th>semantic name</th><th>description</th><th>type</th><th>unit</th><th>confidence</th></tr></thead><tbody>{fields.map((field) => <tr key={`${field.source_column}-${field.semantic_name}`}><td>{field.source_column}</td><td>{field.semantic_name}</td><td>{field.description}</td><td>{field.semantic_type}</td><td>{field.unit ?? "—"}</td><td>{field.confidence.toFixed(2)}</td></tr>)}</tbody></table></div> : <p className="muted">No semantic fields were returned.</p>}</> : null}
  </div>;
}

function SemanticConfirmDialog({ assetName, onCancel, onConfirm }: { assetName: string; onCancel: () => void; onConfirm: () => void }) {
  return <div className="dialog-backdrop" role="presentation"><div className="dialog semantic-confirm" role="dialog" aria-modal="true" aria-labelledby="semantic-confirm-title"><div className="dialog-header"><div><div className="eyebrow">EXPLICIT PROVIDER ACTION</div><h2 id="semantic-confirm-title">确认 AI 整理</h2></div><button className="close-button" onClick={onCancel} aria-label="取消">×</button></div><p className="semantic-confirm-asset">当前资产：{assetName}</p><p>AI 整理将向当前配置的大模型服务发送该资产的受控摘要/样本。</p><p>不发送原始文件；不发送完整大型表格；不修改原始或规范化数据。</p><div className="dialog-actions"><button className="secondary-button" onClick={onCancel}>取消</button><button className="primary-button" onClick={onConfirm}>开始整理</button></div></div></div>;
}

function ProcessDialog({ source, onSource, onClose, onSubmit }: { source: string; onSource: (value: string) => void; onClose: () => void; onSubmit: () => void }) { return <div className="dialog-backdrop" role="presentation"><div className="dialog" role="dialog" aria-modal="true"><div className="dialog-header"><div><div className="eyebrow">NEW PROCESS</div><h2>处理新目录</h2></div><button className="close-button" onClick={onClose}>×</button></div><label className="field-label" htmlFor="source-path">资料目录</label><input id="source-path" className="path-input" value={source} onChange={(event) => onSource(event.target.value)} placeholder="例如：E:\\Research\\Project" autoFocus /><p className="field-help">请输入或粘贴本机目录路径。浏览器不会直接读取本机目录。</p><div className="dialog-actions"><button className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={!source.trim()} onClick={onSubmit}>开始处理</button></div></div></div>; }

function Pagination({ offset, limit, total, onOffset }: { offset: number; limit: number; total: number; onOffset: (value: number) => void }) { const page = Math.floor(offset / limit) + 1; const pages = Math.max(1, Math.ceil(total / limit)); return <div className="pagination"><span>第 {number(page)} / {number(pages)} 页</span><div><button disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - limit))}>上一页</button><button disabled={offset + limit >= total} onClick={() => onOffset(offset + limit)}>下一页</button></div></div>; }

function EmptyState({ title, body, compact = false }: { title: string; body: string; compact?: boolean }) { return <div className={`empty-state ${compact ? "compact" : ""}`}><div className="empty-mark">○</div><strong>{title}</strong><span>{body}</span></div>; }

function compactValue(value: unknown): string { if (value == null) return "—"; const text = typeof value === "string" ? value : JSON.stringify(value); return text.length > 180 ? `${text.slice(0, 180)}…` : text; }
function displayValue(value: unknown): string { if (value == null || value === "") return "NULL"; if (typeof value === "object") return compactValue(value); return String(value); }

export default App;
