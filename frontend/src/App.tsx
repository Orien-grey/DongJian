import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiClientError } from "./api";
import type {
  AnalysisEvidence,
  AnalysisRun,
  AnalysisRunSummary,
  AnalysisScopeKind,
  AISettings,
  AssetDetail,
  AssetSummary,
  FileContentBlock,
  FileContentResponse,
  FileInsight,
  FileInsightQueueSummary,
  FileSearchResponse,
  FileSummary,
  HealthResponse,
  Overview,
  Page,
  QualityIssue,
  QualityStatus,
  Report,
  ReportEvidence,
  ReportSummary,
  SearchResponse,
  SearchOccurrence,
  SearchResult,
  SqlQueryResponse,
  SqlSchemaResponse,
  TablePreview,
  Task,
  TextPreview,
  VisionMode,
} from "./types";

const STAGE_LABELS: Record<string, string> = {
  preparing_scope: "正在准备资料",
  preparing_context: "正在准备资料",
  preparing_report: "正在准备资料",
  loading_analysis: "正在汇总本地证据",
  preparing_evidence: "正在汇总本地证据",
  loading_sources: "加载来源",
  loading_file_insights: "加载文件整理",
  preparing_analysis: "准备分析",
  executing_analysis: "执行分析",
  calling_model: "调用模型",
  requesting_model: "正在请求模型",
  ai_file_insight: "AI 文件整理",
  validating: "验证结果",
  persisting: "保存结果",
  ai_analysis: "数据分析",
  report_generation: "报告生成",
  vision_extraction: "AI Vision 提取",
  queued: "排队中",
  scan: "扫描",
  discover: "扫描中",
  hash: "登记文件",
  discovering: "扫描中",
  registering: "登记中",
  scanning: "扫描中",
  extract: "提取",
  registry_init: "准备数据目录",
  clean: "清洗",
  profile: "画像",
  catalog: "目录",
  completed: "完成",
  failed: "失败",
  cancelling: "正在停止",
  cancelled: "已取消",
  local_ready: "已可查看",
  interrupted: "已中断",
};

const SUBSTAGE_LABELS: Record<string, string> = {
  "preparing bounded context": "正在读取内容",
  preparing_scope: "正在汇总本地证据",
  preparing_context: "正在汇总本地证据",
  preparing_report: "正在准备报告",
  requesting_model: "正在调用 AI",
  calling_model: "正在调用 AI",
  validating: "正在检查 AI 结果",
  persisting: "正在保存",
  completed: "已完成",
  cancelled: "已取消",
  cancelling: "正在取消",
  "AI file understanding complete": "整理完成",
  "AI file understanding queue complete": "整理队列完成",
};

function taskStageLabel(stage: string, substage?: string | null): string {
  return SUBSTAGE_LABELS[substage || ""] || STAGE_LABELS[stage] || "正在处理";
}

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

const EMPTY_FILE_INSIGHT_QUEUE: FileInsightQueueSummary = {
  queued: 0,
  running: 0,
  completed: 0,
  failed: 0,
  cancelled: 0,
  total: 0,
  pendingReady: 0,
  ready: 0,
  cached: 0,
  active: 0,
};

type FileSearchState = {
  query: string;
  results: SearchOccurrence[];
  total: number;
  offset: number;
  index: number;
  matchedPages: number[];
  matchedSections: string[];
  searching: boolean;
};

const EMPTY_FILE_SEARCH: FileSearchState = {
  query: "",
  results: [],
  total: 0,
  offset: 0,
  index: -1,
  matchedPages: [],
  matchedSections: [],
  searching: false,
};

type ResetUiState = {
  requestId: string;
  phase: string;
  result: "running" | "succeeded" | "failed";
  startedAt: number;
  serverUnavailable: boolean;
  safeError: string | null;
};

const RESET_PHASE_LABELS: Record<string, string> = {
  accepted: "正在停止后台任务",
  handler_completed: "正在停止后台任务",
  supervisor_started: "正在停止后台任务",
  draining: "正在停止后台任务",
  server_stopped: "正在关闭本地服务",
  cleanup: "正在清理生成数据",
  registry_recreated: "正在重建本地索引",
  restart: "正在重新启动服务",
  health_restored: "正在恢复页面",
  completed: "清空完成",
  failed: "清空失败",
};

function resetPhaseLabel(phase: string): string {
  return RESET_PHASE_LABELS[phase] || "正在清空项目资料";
}

function fileInsightStatusLabel(status: string | null | undefined): string {
  if (status === "no_evidence") return "暂无本地证据";
  if (status === "disabled") return "未启用 AI 整理";
  if (status === "queued") return "已加入整理队列 · 等待模型";
  if (status === "requesting_model") return "AI 正在整理";
  if (status === "validating") return "正在验证整理结果";
  if (status === "persisting") return "正在保存整理结果";
  if (status === "cancelling") return "正在取消整理";
  if (status === "cancelled") return "已取消整理";
  if (status === "completed") return "整理完成";
  if (status === "failed") return "整理失败";
  if (status === "not_found") return "不可用";
  return "尚未整理";
}

function number(value: number | null | undefined): string {
  return value == null ? "—" : new Intl.NumberFormat("zh-CN").format(value);
}

function fileSize(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value < 1024) return `${number(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function errorText(error: unknown): string {
  if (error instanceof ApiClientError) {
    const diagnostic = error.diagnostic ? `：${error.diagnostic}` : "";
    const stage = error.stage ? `（阶段：${error.stage}）` : "";
    const completed = Array.isArray(error.details?.completedPhases) && error.details.completedPhases.length
      ? `（已完成：${error.details.completedPhases.join("、")}）`
      : "";
    const requestId = error.requestId ? `（请求 ID：${error.requestId}）` : "";
    return `${error.message}${diagnostic}${stage}${completed}${requestId}`;
  }
  return "请求未完成，请稍后重试。";
}

function qualityLabel(status: QualityStatus): string {
  return { ready: "可直接使用", needs_review: "需要审核", unusable: "不可用" }[status] ?? status;
}

function typeLabel(type: "table" | "text"): string {
  return type === "table" ? "表格" : "文本";
}

function dimensions(asset: AssetSummary): string {
  if (asset.assetType === "table") return `${number(asset.rows)} × ${number(asset.columns)}`;
  return `${number(asset.chars)} 字符 · ${number(asset.chunks)} 段文本`;
}

function suggestedActionLabel(action: string): string {
  const normalized = action.toLowerCase();
  if (normalized.includes("ocr")) return "请先核对 OCR 结果与原始页面。";
  if (normalized.includes("header") || normalized.includes("source boundary")) return "请打开资产核对来源边界或表头。";
  if (normalized.includes("semantic")) return "请先确认原始与规范化内容，再进行 AI 整理。";
  if (normalized.includes("raw") || normalized.includes("normalized")) return "请打开原始与规范化结果核对后再继续。";
  return "请打开资产核对原始内容后再决定。";
}

function matchKindLabel(kind: string): string {
  return {
    text_exact: "正文精确匹配",
    text_substring: "正文包含匹配",
    text_token: "正文关键词匹配",
    text_phrase: "正文短语匹配",
    fallback_name_contains: "文件名匹配",
    source_file_contains: "来源文件匹配",
    semantic_display_name_contains: "资产名称匹配",
    semantic_metadata_contains: "资产信息匹配",
  }[kind] ?? "本地匹配";
}

function sourceKindLabel(kind: string): string {
  return { page: "PDF 页面", image: "图片", sheet: "工作表", file: "文件" }[kind] ?? kind;
}

function layerLabel(layer: string): string {
  return layer === "normalized" ? "规范化内容" : layer === "raw" ? "原始内容" : layer;
}

function taskStatusLabel(status: Task["status"]): string {
  return {
    queued: "排队中",
    running: "处理中",
    cancelling: "正在停止",
    succeeded: "处理完成",
    failed: "处理失败",
    cancelled: "已取消",
    interrupted: "已中断",
  }[status] ?? "处理中";
}

function taskTypeLabel(task: Task): string {
  if (task.taskType === "process") return task.visionMode === "ai_vision" ? "目录处理 · AI 视觉" : "目录处理";
  if (task.taskType === "file_reprocess") return "单文件重新处理";
  if (task.taskType === "file_insight" || task.taskType === "file_insight_batch") return "AI 文件整理";
  if (task.taskType === "ai_analysis") return "AI 分析";
  if (task.taskType === "report_generation") return "报告生成";
  return "后台任务";
}

function taskUpdatedLabel(task: Task): string {
  return formatDate(task.updatedAt || task.finishedAt || task.startedAt);
}

function visionStatusLabel(content: FileContentResponse, task: Task | null): string | null {
  const format = content.file.format.toLowerCase();
  if (!["png", "jpg", "jpeg", "pdf"].includes(format)) return null;
  const used = content.sections.some((section) => section.blocks.some((block) => {
    const extractor = String(block.provenance?.extractor || block.provenance?.extractorId || "").toLowerCase();
    return extractor === "vision_llm" || extractor.includes("vision");
  }));
  if (used) return "已使用 AI 视觉增强";
  if (task?.taskType === "process" && task.visionMode === "ai_vision") {
    if (["queued"].includes(task.status)) return "等待 AI 视觉增强";
    if (["running", "cancelling"].includes(task.status) && ["vision_extraction", "extract", "clean"].includes(task.currentStage)) return "AI 视觉处理中";
    if (task.status === "failed" && task.currentFileId === content.file.fileId) return "AI 视觉增强失败，已保留本地识别结果";
  }
  return "本地 OCR";
}

function fileInsightTaskStatus(task: Task | null, fallback: string | null | undefined): string {
  if (!task) return fallback || "not_started";
  if (task.status === "queued") return "queued";
  if (task.status === "running") {
    if (task.currentSubstage === "validating") return "validating";
    if (task.currentSubstage === "persisting") return "persisting";
    return "requesting_model";
  }
  if (task.status === "cancelling") return "cancelling";
  if (task.status === "succeeded") return "completed";
  if (task.status === "cancelled" || task.status === "interrupted") return "cancelled";
  if (task.status === "failed") return "failed";
  return fallback || "not_started";
}

function issueTypeLabel(issueType: string): string {
  return {
    low_content: "内容过少",
    empty_content: "没有提取到内容",
    empty_text: "没有提取到文本",
    low_ocr_confidence: "OCR 可信度较低",
    incomplete_text_provenance: "文本来源信息不完整",
    cleaning_failed: "清洗失败",
  }[issueType] ?? issueType;
}

function issueDescription(issue: QualityIssue): string {
  const evidence = issue.evidence && typeof issue.evidence === "object" ? issue.evidence as Record<string, unknown> : {};
  const chars = Number(evidence.char_count ?? evidence.charCount ?? 0);
  if (issue.issue_type === "low_content") return `系统只从该资产提取到 ${number(chars)} 个字符，可能是空白页、图片页或文本层不完整。`;
  if (issue.issue_type === "empty_text") return "系统没有从该资产提取到可用文本，可能是空白页或图片内容。";
  if (issue.issue_type === "low_ocr_confidence") return "OCR 结果的文字可信度较低，建议打开资产核对原始页面。";
  if (issue.issue_type === "cleaning_failed") return "确定性清洗阶段未完成，规范化数据可能不可用；原始提取结果仍会保留。";
  if (issue.issue_type === "incomplete_text_provenance") return "文本已提取，但来源坐标或运行信息不完整，建议核对资产来源。";
  return issue.description;
}

function severityLabel(severity: QualityIssue["severity"]): string {
  return { info: "提示", warning: "注意", error: "错误", critical: "严重" }[severity];
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

function formatDuration(seconds: number): string {
  const safe = Math.max(0, Math.round(seconds));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const rest = safe % 60;
  return [hours, minutes, rest].map((value) => String(value).padStart(2, "0")).join(":");
}

function App() {
  const [page, setPage] = useState<Page>("overview");
  const [overview, setOverview] = useState<Overview>(EMPTY_OVERVIEW);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [aiPersistedSettings, setAiPersistedSettings] = useState<AISettings | null>(null);
  const [aiForm, setAiForm] = useState({ baseUrl: "", apiKey: "", model: "", timeout: "120", visionEnabled: false, fileInsightEnabled: false, clearApiKey: false });
  const [aiDraftDirty, setAiDraftDirty] = useState(false);
  const [aiTestResult, setAiTestResult] = useState<{ kind: "success" | "error"; message: string; structuredOutputOk?: boolean; category?: string } | null>(null);
  const [aiSettingsState, setAiSettingsState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [aiSettingsError, setAiSettingsError] = useState("");
  const [resetBusy, setResetBusy] = useState(false);
  const [resetState, setResetState] = useState<ResetUiState | null>(null);
  const [resetClock, setResetClock] = useState(() => Date.now());
  const [resetMessage, setResetMessage] = useState("");
  const [resetMessageKind, setResetMessageKind] = useState<"" | "success" | "error">("");
  const [aiBusy, setAiBusy] = useState(false);
  const [aiMessage, setAiMessage] = useState("");
  const [aiMessageKind, setAiMessageKind] = useState<"" | "success" | "error">("");
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [files, setFiles] = useState<FileSummary[]>([]);
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [catalogError, setCatalogError] = useState("");
  const [catalogOffset, setCatalogOffset] = useState(0);
  const [catalogType, setCatalogType] = useState<"" | "table_file" | "document" | "image" | "unprocessed" | "failed">("");
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
  const [fileInsightQueue, setFileInsightQueue] = useState<FileInsightQueueSummary>(EMPTY_FILE_INSIGHT_QUEUE);
  const [analysisQuestion, setAnalysisQuestion] = useState("");
  const [analysisScope, setAnalysisScope] = useState<AnalysisScopeKind>("all");
  const [analysisSelectedIds, setAnalysisSelectedIds] = useState<string[]>([]);
  const [analysisAssets, setAnalysisAssets] = useState<AssetSummary[]>([]);
  const [analysisHistory, setAnalysisHistory] = useState<AnalysisRunSummary[]>([]);
  const [analysisRun, setAnalysisRun] = useState<AnalysisRun | null>(null);
  const [analysisTaskId, setAnalysisTaskId] = useState<string | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisError, setAnalysisError] = useState("");
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [selectedReport, setSelectedReport] = useState<Report | null>(null);
  const [reportFileIds, setReportFileIds] = useState<string[]>([]);
  const [reportSelectAll, setReportSelectAll] = useState(false);
  const [reportFilters, setReportFilters] = useState<Record<string, string>>({});
  const [reportType, setReportType] = useState<"overview" | "analysis">("overview");
  const [reportTitle, setReportTitle] = useState("");
  const [reportPurpose, setReportPurpose] = useState("");
  const [reportTaskId, setReportTaskId] = useState<string | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState("");
  const [selected, setSelected] = useState<AssetDetail | null>(null);
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [selectedFileContent, setSelectedFileContent] = useState<FileContentResponse | null>(null);
  const [workspaceVersion, setWorkspaceVersion] = useState(0);
  const [workspaceTargetFileIds, setWorkspaceTargetFileIds] = useState<string[]>([]);
  const [selectedFileRefreshVersion, setSelectedFileRefreshVersion] = useState(0);
  const [fileViewerMode, setFileViewerMode] = useState<"reading" | "source">("source");
  const [reprocessBusy, setReprocessBusy] = useState(false);
  const [fileLocator, setFileLocator] = useState<FileLocator>({});
  const [fileSearch, setFileSearch] = useState<FileSearchState>(EMPTY_FILE_SEARCH);
  const [fileDetailState, setFileDetailState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [fileDetailError, setFileDetailError] = useState("");
  const [fileTextLoadingKey, setFileTextLoadingKey] = useState<string | null>(null);
  const [fileReloadKey, setFileReloadKey] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailTab, setDetailTab] = useState<"data" | "profile" | "quality" | "source" | "semantic">("data");
  const [tableLayer, setTableLayer] = useState<"raw" | "normalized">("normalized");
  const [tableOffset, setTableOffset] = useState(0);
  const [tablePreview, setTablePreview] = useState<TablePreview | null>(null);
  const [textPreview, setTextPreview] = useState<TextPreview | null>(null);
  const [taskSource, setTaskSource] = useState("");
  const [taskVisionMode, setTaskVisionMode] = useState<VisionMode>("local");
  const [taskAutoFileInsight, setTaskAutoFileInsight] = useState(false);
  const [showProcess, setShowProcess] = useState(false);
  const [showSemanticConfirm, setShowSemanticConfirm] = useState(false);
  const [semanticEnriching, setSemanticEnriching] = useState(false);
  const [semanticNotice, setSemanticNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const knownTaskStatuses = useRef<Record<string, Task["status"]>>({});
  const pendingTaskTransitions = useRef<Record<string, "cancelling">>({});
  const pendingIssueUpdates = useRef<Set<string>>(new Set());
  const catalogRequestGeneration = useRef(0);
  const fileRequestGeneration = useRef(0);
  const workspaceSnapshotRequestGeneration = useRef(0);
  const aiSettingsRequestGeneration = useRef(0);
  const aiDraftDirtyRef = useRef(false);
  const resetBusyRef = useRef(false);
  const workspaceVersionRef = useRef(0);

  useEffect(() => {
    if (!resetBusy) return;
    const timer = window.setInterval(() => setResetClock(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [resetBusy]);

  const refreshOverview = useCallback(async () => {
    try {
      setOverview(await api.overview());
    } catch (cause) {
      if (!resetBusyRef.current) setError(errorText(cause));
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch (cause) {
      if (!resetBusyRef.current) setError(errorText(cause));
    }
  }, []);

  const refreshAISettings = useCallback(async (replaceDraft = false) => {
    const generation = aiSettingsRequestGeneration.current + 1;
    aiSettingsRequestGeneration.current = generation;
    setAiSettingsState("loading");
    setAiSettingsError("");
    try {
      const result = await api.aiSettings();
      if (generation !== aiSettingsRequestGeneration.current) return;
      setAiPersistedSettings(result.settings);
      if (replaceDraft || !aiDraftDirtyRef.current) {
        setAiForm({
          baseUrl: result.settings.baseUrl,
          model: result.settings.model,
          timeout: String(result.settings.timeout),
          visionEnabled: result.settings.visionEnabled,
          fileInsightEnabled: result.settings.fileInsightEnabled === true,
          apiKey: "",
          clearApiKey: false,
        });
        setTaskAutoFileInsight(result.settings.fileInsightEnabled === true);
        aiDraftDirtyRef.current = false;
        setAiDraftDirty(false);
      }
      setAiSettingsState("ready");
    } catch (cause) {
      if (generation !== aiSettingsRequestGeneration.current) return;
      setAiSettingsError(errorText(cause));
      setAiSettingsState("error");
    }
  }, []);

  const refreshCatalog = useCallback(async () => {
    const generation = catalogRequestGeneration.current + 1;
    catalogRequestGeneration.current = generation;
    setLoading(true);
    setCatalogError("");
    try {
      const params = new URLSearchParams({ limit: "25", offset: String(catalogOffset) });
       if (catalogType) params.set("category", catalogType);
      if (catalogQuality) params.set("quality", catalogQuality);
      if (catalogFormat) params.set("format", catalogFormat);
      if (catalogQuery.trim()) params.set("q", catalogQuery.trim());
       const result = await api.files(params);
       if (generation !== catalogRequestGeneration.current) return;
       setFiles(result.items);
      setCatalogTotal(result.pagination.total);
    } catch (cause) {
      if (generation === catalogRequestGeneration.current) setCatalogError(errorText(cause));
    } finally {
      if (generation === catalogRequestGeneration.current) setLoading(false);
    }
  }, [catalogFormat, catalogOffset, catalogQuality, catalogQuery, catalogType]);

  const refreshIssues = useCallback(async () => {
    try {
      const params = new URLSearchParams({ status: "open", limit: "100", offset: "0" });
      setIssues((await api.quality(params)).items);
    } catch (cause) {
      if (!resetBusyRef.current) setError(errorText(cause));
    }
  }, []);

  const refreshTasks = useCallback(async () => {
    try {
      const next = (await api.tasks()).items;
      const previous = knownTaskStatuses.current;
      const terminalTransition = next.some((task) => {
        const before = previous[task.taskId];
        return before != null
          && ["queued", "running", "cancelling"].includes(before)
          && ["succeeded", "failed", "cancelled", "interrupted"].includes(task.status);
      });
      knownTaskStatuses.current = Object.fromEntries(next.map((task) => [task.taskId, task.status]));
      setTasks(next);
      if (terminalTransition) {
        // A terminal process transition refreshes aggregate screens. Detail
        // pages own their request lifecycle and must not be re-fetched by the
        // one-second task poller.
        await Promise.all([refreshOverview(), refreshCatalog(), refreshIssues()]);
      }
      return next;
    } catch (cause) {
      if (!resetBusyRef.current) setError(errorText(cause));
      return [];
    }
  }, [refreshCatalog, refreshIssues, refreshOverview]);

  const refreshFileInsightQueue = useCallback(async () => {
    try {
      setFileInsightQueue(await api.fileInsightQueue());
    } catch (cause) {
      if (!resetBusyRef.current) setError(errorText(cause));
    }
  }, []);

  const refreshWorkspaceSnapshot = useCallback(async () => {
    const generation = workspaceSnapshotRequestGeneration.current + 1;
    workspaceSnapshotRequestGeneration.current = generation;
    try {
      const snapshot = await api.workspaceSnapshot();
      if (generation !== workspaceSnapshotRequestGeneration.current) return null;
      const next: Task[] = (Array.isArray(snapshot.tasks) ? snapshot.tasks : []).map((task): Task => {
        if (pendingTaskTransitions.current[task.taskId] !== "cancelling") return task;
        if (["succeeded", "failed", "cancelled", "interrupted"].includes(task.status)) {
          delete pendingTaskTransitions.current[task.taskId];
          return task;
        }
        return { ...task, status: "cancelling", currentStage: "cancelling" };
      });
      knownTaskStatuses.current = Object.fromEntries(next.map((task) => [task.taskId, task.status])) as Record<string, Task["status"]>;
      setTasks(next);
      if (snapshot.workspaceVersion !== workspaceVersionRef.current) {
        workspaceVersionRef.current = snapshot.workspaceVersion;
        setWorkspaceVersion(snapshot.workspaceVersion);
        setWorkspaceTargetFileIds(Array.isArray(snapshot.changedFileIds) ? snapshot.changedFileIds : snapshot.changedFileId ? [snapshot.changedFileId] : []);
      }
      return snapshot;
    } catch (cause) {
      if (generation === workspaceSnapshotRequestGeneration.current && !resetBusyRef.current) setError(errorText(cause));
      return null;
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
      const result = await api.catalog(new URLSearchParams({ view: "assets", type: "table", limit: "100", offset: "0" }));
      setQueryAssets(result.items);
    } catch (cause) {
      setQueryError(errorText(cause));
    }
  }, []);

  const refreshAnalysis = useCallback(async () => {
    try {
      const [catalog, history] = await Promise.all([
        api.catalog(new URLSearchParams({ view: "assets", limit: "100", offset: "0" })),
        api.analysisRuns(20),
      ]);
      setAnalysisAssets(catalog.items);
      setAnalysisHistory(history.items);
    } catch (cause) {
      setAnalysisError(errorText(cause));
    }
  }, []);

  const refreshReports = useCallback(async () => {
    try {
      const reportResult = await api.reports(50);
      setReports(reportResult.items);
    } catch (cause) {
      setReportError(errorText(cause));
    }
  }, []);

  const loadReport = useCallback(async (reportId: string) => {
    try {
      setSelectedReport((current) => current?.report_id === reportId ? current : null);
      setSelectedReport((await api.report(reportId)).report);
    } catch (cause) {
      setReportError(errorText(cause));
    }
  }, []);

  const loadAnalysisRun = useCallback(async (runId: string) => {
    try {
      setAnalysisRun((current) => current?.analysis_run_id === runId ? current : null);
      setAnalysisRun((await api.analysisRun(runId)).run);
    } catch (cause) {
      setAnalysisError(errorText(cause));
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
    void refreshOverview();
    void refreshCatalog();
    void refreshIssues();
    void refreshWorkspaceSnapshot();
    void refreshFileInsightQueue();
  }, [refreshCatalog, refreshFileInsightQueue, refreshHealth, refreshIssues, refreshOverview, refreshWorkspaceSnapshot]);

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
    if (!selectedFileId) return;
    const controller = new AbortController();
    const generation = fileRequestGeneration.current + 1;
    fileRequestGeneration.current = generation;
    setFileDetailState("loading");
    setFileDetailError("");
    setSelectedFileContent(null);
    void api.fileContent(selectedFileId, { page: fileLocator.page, sheet: fileLocator.sheet }, controller.signal).then((value) => {
      if (generation !== fileRequestGeneration.current || controller.signal.aborted) return;
      setSelectedFileContent(value);
      setFileDetailState("ready");
    }).catch((cause) => {
      if (controller.signal.aborted || generation !== fileRequestGeneration.current) return;
      setFileDetailError(errorText(cause));
      setFileDetailState("error");
    });
    return () => {
      controller.abort();
    };
  }, [fileLocator.page, fileLocator.sheet, fileReloadKey, selectedFileId]);

  useEffect(() => {
    if (!selected || selected.assetType !== "table" || detailTab !== "data") return;
    void api.tablePreview(selected.assetId, tableLayer, 20, tableOffset).then(setTablePreview).catch((cause) => setError(errorText(cause)));
  }, [detailTab, selected, tableLayer, tableOffset]);

  useEffect(() => {
    if (!selected || selected.assetType !== "text" || detailTab !== "data") return;
    void api.textPreview(selected.assetId, 8_000, 0).then(setTextPreview).catch((cause) => setError(errorText(cause)));
  }, [detailTab, selected]);

  const hasActiveWorkspaceWork = tasks.some((task) => ["queued", "running", "cancelling"].includes(task.status));

  useEffect(() => {
    if (resetBusy || !hasActiveWorkspaceWork) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      await refreshWorkspaceSnapshot();
      if (!cancelled) timer = window.setTimeout(() => void poll(), 1_000);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [hasActiveWorkspaceWork, refreshWorkspaceSnapshot, resetBusy]);

  useEffect(() => {
    if (workspaceVersion <= 0) return;
    const timer = window.setTimeout(() => {
      void refreshOverview();
      void refreshIssues();
      void refreshFileInsightQueue();
      if (page === "catalog") void refreshCatalog();
      const targetFileId = selectedFileId;
      if (targetFileId && workspaceTargetFileIds.includes(targetFileId)) {
        setSelectedFileRefreshVersion(workspaceVersion);
        setFileReloadKey((current) => current + 1);
      }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [page, refreshCatalog, refreshFileInsightQueue, refreshIssues, refreshOverview, selectedFileId, workspaceTargetFileIds, workspaceVersion]);

  useEffect(() => {
    if (page === "catalog") void refreshCatalog();
  }, [page, refreshCatalog]);

  useEffect(() => {
    if (page === "query") void refreshQueryAssets();
  }, [page, refreshQueryAssets]);

  useEffect(() => {
    if (page === "settings") {
      void refreshAISettings();
      void refreshFileInsightQueue();
    }
  }, [page, refreshAISettings, refreshFileInsightQueue]);

  useEffect(() => {
    if (page === "analysis") void refreshAnalysis();
  }, [page, refreshAnalysis]);

  useEffect(() => {
    if (page === "reports") void refreshReports();
  }, [page, refreshReports]);

  useEffect(() => {
    if (!analysisTaskId) return;
    const task = tasks.find((item) => item.taskId === analysisTaskId);
    if (!task || !task.analysisRunId) return;
    if (!["succeeded", "failed", "cancelled", "interrupted"].includes(task.status)) return;
    if (analysisRun?.analysis_run_id === task.analysisRunId && analysisRun.status !== "running") return;
    void loadAnalysisRun(task.analysisRunId);
  }, [analysisRun, analysisTaskId, loadAnalysisRun, tasks]);

  useEffect(() => {
    if (!reportTaskId) return;
    const task = tasks.find((item) => item.taskId === reportTaskId);
    if (!task || ["queued", "running", "cancelling"].includes(task.status)) return;
    if (task.status === "succeeded" && task.reportId) void loadReport(task.reportId);
    void refreshReports();
  }, [loadReport, reportTaskId, refreshReports, tasks]);

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

  const openFile = (fileId: string, locator: FileLocator = { page: 1 }) => {
    setSelectedFileId(fileId);
    setFileViewerMode("source");
    setSelectedFileRefreshVersion(0);
    setFileLocator(locator);
    setFileSearch(EMPTY_FILE_SEARCH);
    setFileReloadKey((current) => current + 1);
    setSelectedFileContent(null);
    setFileDetailError("");
    setFileTextLoadingKey(null);
    setFileDetailState("loading");
    setSelectedId(null);
    setPage("file-detail");
  };

  const navigateFile = (next: FileLocator) => {
    setFileLocator((current) => {
      const hasTextTarget = next.textAssetId != null || next.textOffset != null || next.matchStart != null;
      const hasTableTarget = next.tableAssetId != null || next.tableRow != null || next.tableColumn != null;
      return {
        ...current,
        ...next,
        ...(hasTextTarget ? {} : { textAssetId: undefined, textOffset: undefined, matchStart: undefined, matchEnd: undefined }),
        ...(hasTableTarget ? {} : { tableAssetId: undefined, tableRow: undefined, tableColumn: undefined, tableMatchStart: undefined, tableMatchEnd: undefined }),
      };
    });
  };

  const retryFile = () => {
    setFileReloadKey((current) => current + 1);
  };

  const reprocessSelectedFile = async () => {
    if (!selectedFileId || reprocessBusy) return;
    setReprocessBusy(true);
    setFileDetailError("");
    try {
      const result = await api.reprocessFile(selectedFileId);
      knownTaskStatuses.current[result.task.taskId] = result.task.status;
      setTasks((current) => [result.task, ...current.filter((item) => item.taskId !== result.task.taskId)]);
      setFileDetailError("已提交当前文件的本地重新处理；原始文件不会被修改。");
    } catch (cause) {
      setFileDetailError(errorText(cause));
    } finally {
      setReprocessBusy(false);
    }
  };

  const loadMoreFileText = useCallback(async (blockKey: string, assetId: string, offset: number) => {
    const generation = fileRequestGeneration.current;
    setFileTextLoadingKey(blockKey);
    try {
      const preview = await api.textPreview(assetId, 12_000, offset);
      if (generation !== fileRequestGeneration.current) return;
      setSelectedFileContent((current) => {
        if (!current) return current;
        let changed = false;
        const sections = current.sections.map((section) => ({
          ...section,
          blocks: section.blocks.map((block, index) => {
            if (block.type !== "text" || `${section.sectionId}:${index}` !== blockKey || block.assetId !== assetId) return block;
            changed = true;
            return {
              ...block,
              text: `${block.text}${preview.text}`,
              nextTextOffset: preview.offset + preview.text.length,
              truncated: preview.hasNext,
              continuationAvailable: preview.hasNext,
            };
          }),
        }));
        return changed ? { ...current, sections } : current;
      });
    } catch (cause) {
      if (generation === fileRequestGeneration.current) setFileDetailError(errorText(cause));
    } finally {
      if (generation === fileRequestGeneration.current) setFileTextLoadingKey(null);
    }
  }, []);

  const openSearchResult = (result: SearchResult) => {
    const fileId = result.fileId || (typeof result.provenance?.fileId === "string" ? result.provenance.fileId : null);
    if (fileId) {
      openFile(fileId, {
        page: result.pageNumber ?? 1,
        sheet: result.sheetName ?? undefined,
        textAssetId: result.locator?.assetId ?? (result.assetType === "text" ? result.assetId : undefined),
        textOffset: result.locator?.offset,
        matchStart: result.locator?.matchOffsets?.[0]?.[0],
        matchEnd: result.locator?.matchOffsets?.[0]?.[1],
      });
      return;
    }
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
      const result = await api.process(taskSource.trim(), taskVisionMode, taskAutoFileInsight);
      setShowProcess(false);
      setTaskSource("");
      setTaskVisionMode("local");
      setTaskAutoFileInsight(false);
      setPage("tasks");
      // Register the task before the first poll so a very fast task still
      // produces the queued/running -> terminal refresh transition.
      knownTaskStatuses.current[result.taskId] = result.task.status;
      setTasks((current) => [result.task, ...current.filter((item) => item.taskId !== result.taskId)]);
      void refreshOverview();
    } catch (cause) {
      setError(errorText(cause));
    }
  };

  const markTaskCancelling = (taskId: string) => {
    pendingTaskTransitions.current[taskId] = "cancelling";
    setTasks((current) => current.map((item) => item.taskId === taskId ? { ...item, status: "cancelling", currentStage: "cancelling" } : item));
  };

  const toggleAnalysisAsset = (assetId: string) => {
    setAnalysisRun(null);
    setAnalysisSelectedIds((current) => current.includes(assetId)
      ? current.filter((value) => value !== assetId)
      : [...current, assetId].slice(0, 8));
  };

  const startAnalysis = async () => {
    setAnalysisError("");
    if (!health?.llm.configured) {
      setAnalysisError("未配置 AI 模型，当前完全离线。请在“设置 → AI模型”填写项目配置。");
      return;
    }
    if (!health.llm.enabled) {
      setAnalysisError("AI 模型已配置但不可用，请先完成连接测试。");
      return;
    }
    if (!analysisQuestion.trim()) {
      setAnalysisError("请输入分析问题。");
      return;
    }
    if (analysisScope === "selected" && !analysisSelectedIds.length) {
      setAnalysisError("选择特定数据时，至少选择一个数据表或文本内容。");
      return;
    }
    setAnalysisLoading(true);
    try {
      const result = await api.analysisStart(
        analysisQuestion.trim(),
        analysisScope,
        analysisScope === "selected" ? analysisSelectedIds : [],
      );
      setAnalysisTaskId(result.taskId);
      setAnalysisRun(null);
      knownTaskStatuses.current[result.taskId] = result.task.status;
      setTasks((current) => [result.task, ...current.filter((item) => item.taskId !== result.taskId)]);
      void refreshAnalysis();
    } catch (cause) {
      if (cause instanceof ApiClientError && cause.code === "MODEL_NOT_CONFIGURED") {
        setAnalysisError("未配置 AI 模型，当前完全离线。");
      } else if (cause instanceof ApiClientError && cause.code === "MODEL_UNVERIFIED") {
        setAnalysisError("AI 模型已配置但不可用，请先完成连接测试。");
      } else {
        setAnalysisError(errorText(cause));
      }
    } finally {
      setAnalysisLoading(false);
    }
  };

  const cancelAnalysis = async () => {
    if (!analysisTaskId) return;
    markTaskCancelling(analysisTaskId);
    try {
      const result = await api.cancelTask(analysisTaskId);
      delete pendingTaskTransitions.current[analysisTaskId];
      setTasks((current) => current.map((item) => item.taskId === result.task.taskId ? result.task : item));
    } catch (cause) {
      delete pendingTaskTransitions.current[analysisTaskId];
      void refreshWorkspaceSnapshot();
      setAnalysisError(errorText(cause));
    }
  };

  const toggleReportFile = (fileId: string) => {
    setReportSelectAll(false);
    setReportFileIds((current) => current.includes(fileId)
      ? current.filter((value) => value !== fileId)
      : [...current, fileId].slice(0, 1024));
  };

  const selectAllReportFiles = (filters: Record<string, string> = {}) => {
    setReportSelectAll(true);
    setReportFileIds([]);
    setReportFilters(filters);
  };

  const clearReportFiles = () => {
    setReportSelectAll(false);
    setReportFileIds([]);
    setReportFilters({});
  };

  const startReport = async () => {
    setReportError("");
    if (!reportSelectAll && reportFileIds.length === 0) {
      setReportError("请至少选择一个文件，或点击“全部当前筛选资料”。");
      return;
    }
    setReportLoading(true);
    try {
      const result = await api.reportStart(
        reportFileIds,
        reportTitle.trim(),
        reportPurpose.trim(),
        reportType,
        reportSelectAll,
        reportSelectAll ? reportFilters : {},
      );
      setReportTaskId(result.taskId);
      knownTaskStatuses.current[result.taskId] = result.task.status;
      setTasks((current) => [result.task, ...current.filter((item) => item.taskId !== result.taskId)]);
    } catch (cause) {
      setReportError(errorText(cause));
    } finally {
      setReportLoading(false);
    }
  };

  const cancelReport = async () => {
    if (!reportTaskId) return;
    markTaskCancelling(reportTaskId);
    try {
      const result = await api.cancelTask(reportTaskId);
      delete pendingTaskTransitions.current[reportTaskId];
      setTasks((current) => current.map((item) => item.taskId === result.task.taskId ? result.task : item));
    } catch (cause) {
      delete pendingTaskTransitions.current[reportTaskId];
      void refreshWorkspaceSnapshot();
      setReportError(errorText(cause));
    }
  };

  const exportReport = async (format: "markdown" | "html") => {
    if (!selectedReport) return;
    try {
      const result = await api.reportExport(selectedReport.report_id, format);
      const blob = new Blob([result.content], { type: result.contentType });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${selectedReport.title || "analysis-report"}.${format === "html" ? "html" : "md"}`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      setReportError(errorText(cause));
    }
  };

  const updateIssue = async (issue: QualityIssue, status: QualityIssue["status"]) => {
    if (pendingIssueUpdates.current.has(issue.issue_id)) return;
    pendingIssueUpdates.current.add(issue.issue_id);
    const previousIssues = issues;
    if (status !== "open") {
      setIssues((current) => current.filter((item) => item.issue_id !== issue.issue_id));
    }
    try {
      await api.updateQuality(issue.issue_id, status);
      // The row and open count are already updated optimistically.  Keep the
      // successful click responsive while the aggregate refreshes in the
      // background; a refresh failure must not roll back a committed status.
      void refreshOverview();
      void refreshIssues();
      if (selectedId) {
        void api.asset(selectedId).then(setSelected).catch((cause) => setError(errorText(cause)));
      }
    } catch (cause) {
      setIssues(previousIssues);
      setError(errorText(cause));
    } finally {
      pendingIssueUpdates.current.delete(issue.issue_id);
    }
  };

  const requestSemanticEnrichment = () => {
    if (!selected || !health?.llm.enabled || semanticEnriching) return;
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

  const aiPayload = (): { baseUrl: string; apiKey?: string; clearApiKey?: boolean; model: string; timeout: number; visionEnabled: boolean; fileInsightEnabled: boolean } => {
    const payload: { baseUrl: string; apiKey?: string; clearApiKey?: boolean; model: string; timeout: number; visionEnabled: boolean; fileInsightEnabled: boolean } = {
      baseUrl: aiForm.baseUrl,
      model: aiForm.model,
      timeout: Number(aiForm.timeout),
      visionEnabled: aiForm.visionEnabled,
      fileInsightEnabled: aiForm.fileInsightEnabled,
    };
    if (aiForm.apiKey.trim()) payload.apiKey = aiForm.apiKey;
    if (aiForm.clearApiKey) payload.clearApiKey = true;
    return payload;
  };

  const saveAISettings = async () => {
    setAiBusy(true);
    setAiMessage("");
    setAiMessageKind("");
    try {
      const saved = await api.saveAiSettings(aiPayload());
      setAiPersistedSettings(saved.settings);
      setAiSettingsState("ready");
      setAiSettingsError("");
      setAiForm({ baseUrl: saved.settings.baseUrl, model: saved.settings.model, timeout: String(saved.settings.timeout), visionEnabled: saved.settings.visionEnabled, fileInsightEnabled: saved.settings.fileInsightEnabled === true, apiKey: "", clearApiKey: false });
      setTaskAutoFileInsight(saved.settings.fileInsightEnabled === true);
      aiDraftDirtyRef.current = false;
      setAiDraftDirty(false);
      setAiTestResult(null);
      await refreshHealth();
      setAiMessage("配置已保存；文本 Semantic/Analysis 可按配置使用，Vision 仅在勾选并明确选择后使用。");
      setAiMessageKind("success");
    } catch (cause) {
      setAiMessage(errorText(cause));
      setAiMessageKind("error");
    } finally {
      setAiBusy(false);
    }
  };

  const testAISettings = async () => {
    setAiBusy(true);
    setAiMessage("");
    setAiMessageKind("");
    try {
      const tested = await api.testAiConnection(aiPayload());
      setAiTestResult({ kind: "success", message: tested.structuredOutputOk ? "连接成功，服务同时返回了可识别的结构化响应。" : "连接成功；基础聊天可用，但结构化 JSON 能力未确认。", structuredOutputOk: tested.structuredOutputOk });
      setAiMessage("连接测试只验证了当前草稿，尚未保存配置。");
      setAiMessageKind("success");
    } catch (cause) {
      setAiTestResult({ kind: "error", message: errorText(cause), category: cause instanceof ApiClientError ? cause.category : undefined });
      setAiMessage("连接测试失败；当前草稿未被清空，也没有写入配置。");
      setAiMessageKind("error");
    } finally {
      setAiBusy(false);
    }
  };

  const bulkFileInsights = async () => {
    setAiBusy(true);
    setAiMessage("");
    setAiMessageKind("");
    try {
      const result = await api.bulkFileInsights(true);
      if (result.task) {
        knownTaskStatuses.current[result.task.taskId] = result.task.status;
        setTasks((current) => [result.task as Task, ...current.filter((item) => item.taskId !== result.task?.taskId)]);
      }
    setAiMessage(result.taskId ? `已加入 AI 文件整理队列：${number(result.queued ?? 0)} 个文件。` : `没有新的可查看文件需要整理（缓存 ${number(result.cached ?? 0)} 个）。`);
      setAiMessageKind("success");
      await refreshFileInsightQueue();
    } catch (cause) {
      setAiMessage(errorText(cause));
      setAiMessageKind("error");
    } finally {
      setAiBusy(false);
    }
  };

  const queueFileInsight = async (fileId: string) => {
    const result = await api.queueFileInsight(fileId);
    if (result.task) {
      knownTaskStatuses.current[result.task.taskId] = result.task.status;
      setTasks((current) => [result.task as Task, ...current.filter((item) => item.taskId !== result.task?.taskId)]);
    }
    return result;
  };

  const cancelTask = async (taskId: string) => {
    markTaskCancelling(taskId);
    try {
      const result = await api.cancelTask(taskId);
      delete pendingTaskTransitions.current[taskId];
      setTasks((current) => current.map((item) => item.taskId === taskId ? result.task : item));
      await refreshFileInsightQueue();
    } catch (cause) {
      delete pendingTaskTransitions.current[taskId];
      void refreshWorkspaceSnapshot();
      setError(errorText(cause));
    }
  };

  const resetProjectData = async (confirmation: string) => {
    resetBusyRef.current = true;
    setResetBusy(true);
    setResetClock(Date.now());
    setResetMessage("正在提交清空请求…");
    setResetMessageKind("");
    setError("");
    let requestId: string | null = null;
    let phase = "accepted";
    try {
      const accepted = await api.resetWorkspace(confirmation);
      requestId = accepted.requestId;
      setResetState({ requestId, phase, result: "running", startedAt: Date.now(), serverUnavailable: false, safeError: null });
      setResetMessage("正在清空项目资料");
      setSelectedFileId(null);
      setSelectedFileContent(null);
      setFileDetailState("idle");
      let completed = false;
      const deadline = Date.now() + 90_000;
      while (Date.now() < deadline) {
        let status: Awaited<ReturnType<typeof api.resetStatus>>;
        try {
          status = await api.resetStatus(accepted.requestId);
          phase = status.phase;
          setResetState((current) => current ? { ...current, phase: status.phase, result: status.result, serverUnavailable: false, safeError: status.safe_error } : current);
        } catch {
          setResetState((current) => current ? { ...current, serverUnavailable: true } : current);
          await new Promise((resolve) => window.setTimeout(resolve, 500));
          continue;
        }
        if (status.result === "failed") {
          throw new ApiClientError("RESET_FAILED", status.safe_error || "项目重置失败。", accepted.requestId, false, undefined, "RESET", status.phase);
        }
        let healthReady = false;
        try {
          setHealth(await api.health());
          healthReady = true;
          setResetState((current) => current ? { ...current, serverUnavailable: false } : current);
        } catch {
          setResetState((current) => current ? { ...current, serverUnavailable: true } : current);
        }
        if (status.result === "succeeded" && healthReady) {
          completed = true;
          break;
        }
        if (status.result === "running" && !healthReady) {
          await new Promise((resolve) => window.setTimeout(resolve, 500));
          continue;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 500));
      }
      if (!completed) throw new ApiClientError("RESET_TIMEOUT", "项目重置未在限定时间内完成。", requestId || undefined, false, undefined, "RESET", phase);
      setResetState((current) => current ? { ...current, phase: "completed", result: "succeeded", serverUnavailable: false, safeError: null } : current);
      setResetMessage("项目资料已清空");
      setResetMessageKind("success");
      workspaceVersionRef.current = 0;
      setWorkspaceVersion(0);
      setWorkspaceTargetFileIds([]);
      setSelectedFileRefreshVersion(0);
      setTasks([]);
      setFiles([]);
      setOverview(EMPTY_OVERVIEW);
      setIssues([]);
      setFileInsightQueue(EMPTY_FILE_INSIGHT_QUEUE);
      await Promise.all([refreshHealth(), refreshOverview(), refreshCatalog(), refreshIssues(), refreshWorkspaceSnapshot(), refreshFileInsightQueue()]);
      await new Promise((resolve) => window.setTimeout(resolve, 1_200));
      setPage("overview");
    } catch (cause) {
      setResetState((current) => current ? { ...current, phase, result: "failed", safeError: cause instanceof ApiClientError ? cause.message : "项目重置失败。" } : current);
      setResetMessage(errorText(cause));
      setResetMessageKind("error");
    } finally {
      resetBusyRef.current = false;
      setResetBusy(false);
    }
  };

  const activeTaskCount = tasks.filter((task) => ["queued", "running", "cancelling"].includes(task.status)).length;
  const selectedFileInsightTask = selectedFileId
    ? tasks.find((task) => (task.taskType === "file_insight" && task.fileInsightId === selectedFileId)
      || (task.taskType === "file_insight_batch" && task.currentFileId === selectedFileId)) ?? null
    : null;
  const selectedFileReprocessTask = selectedFileId
    ? tasks.find((task) => task.taskType === "file_reprocess" && task.currentFileId === selectedFileId && ["queued", "running", "cancelling"].includes(task.status)) ?? null
    : null;
  const selectedFileVisionTask = selectedFileId
    ? tasks.find((task) => task.taskType === "process" && task.visionMode === "ai_vision" && (task.currentFileId === selectedFileId || task.status === "queued")) ?? null
    : null;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark">洞</div>
        <div className="brand-copy">
          <div className="brand-name">洞见</div>
          <div className="brand-subtitle">科研资料工作台</div>
        </div>
        <nav className="main-nav" aria-label="主导航">
          <NavButton active={page === "overview"} onClick={() => setPage("overview")}>概览</NavButton>
          <NavButton active={page === "catalog" || page === "detail" || page === "file-detail"} onClick={() => setPage("catalog")}>数据目录</NavButton>
          <NavButton active={page === "search"} onClick={() => setPage("search")}>数据检索</NavButton>
          <NavButton active={page === "query"} onClick={() => setPage("query")}>数据查询</NavButton>
          <NavButton active={page === "analysis"} onClick={() => setPage("analysis")}>AI 分析</NavButton>
          <NavButton active={page === "quality"} onClick={() => setPage("quality")}>质量检查{overview.openQualityIssues ? <span className="nav-count">{overview.openQualityIssues}</span> : null}</NavButton>
          <NavButton active={page === "tasks"} onClick={() => setPage("tasks")}>处理任务{activeTaskCount ? <span className="nav-count">{activeTaskCount}</span> : null}</NavButton>
          <NavButton active={page === "settings"} onClick={() => setPage("settings")}>设置</NavButton>
        </nav>
        <div className="report-nav-shortcut"><NavButton active={page === "reports"} onClick={() => setPage("reports")}>报告</NavButton></div>
        <div className="topbar-status"><span className="status-dot" />本地离线模式{activeTaskCount ? <span className="topbar-task-status">· {activeTaskCount} 个任务进行中</span> : null}</div>
      </header>

      <main className="main-content">
        {error ? <div className="error-banner" role="alert"><span>{error}</span><button onClick={() => setError("")}>关闭</button></div> : null}
        {page === "overview" ? <OverviewPage overview={overview} health={health} tasks={tasks} onProcess={() => setShowProcess(true)} onNavigate={setPage} onOpenAsset={openAsset} /> : null}
        {page === "catalog" ? <CatalogPage files={files} total={catalogTotal} offset={catalogOffset} loading={loading} error={catalogError} onRetry={() => void refreshCatalog()} type={catalogType} quality={catalogQuality} format={catalogFormat} query={catalogQuery} formats={overview.formats} onType={(value) => resetCatalog(() => setCatalogType(value))} onQuality={(value) => resetCatalog(() => setCatalogQuality(value))} onFormat={(value) => resetCatalog(() => setCatalogFormat(value))} onQuery={(value) => { setCatalogOffset(0); setCatalogQuery(value); }} onOffset={setCatalogOffset} onOpen={openFile} onProcess={() => setShowProcess(true)} /> : null}
        {page === "search" ? <SearchPage input={searchInput} query={searchQuery} results={searchResults} total={searchTotal} offset={searchOffset} loading={searchLoading} submitted={searchSubmitted} hasAssets={overview.tableAssets + overview.textAssets > 0} type={searchType} quality={searchQuality} format={searchFormat} match={searchMatch} formats={overview.formats} onInput={setSearchInput} onSubmit={submitSearch} onType={(value) => { setSearchType(value); setSearchOffset(0); }} onQuality={(value) => { setSearchQuality(value); setSearchOffset(0); }} onFormat={(value) => { setSearchFormat(value); setSearchOffset(0); }} onMatch={(value) => { setSearchMatch(value); setSearchOffset(0); }} onOffset={(value) => { setSearchOffset(value); void refreshSearch(value); }} onOpen={openSearchResult} /> : null}
        {page === "query" ? <QueryPage assets={queryAssets} selectedIds={querySelectedIds} schema={querySchema} sql={querySql} result={queryResult} loading={queryLoading} error={queryError} onToggle={toggleQueryAsset} onSql={setQuerySql} onRun={runSql} onOpen={openAsset} /> : null}
        {page === "analysis" ? <AnalysisPage health={health} assets={analysisAssets} selectedIds={analysisSelectedIds} scope={analysisScope} question={analysisQuestion} history={analysisHistory} run={analysisRun} task={analysisTaskId ? tasks.find((item) => item.taskId === analysisTaskId) ?? null : null} loading={analysisLoading} error={analysisError} onQuestion={setAnalysisQuestion} onScope={(value) => { setAnalysisScope(value); setAnalysisRun(null); }} onToggleAsset={toggleAnalysisAsset} onStart={() => void startAnalysis()} onCancel={() => void cancelAnalysis()} onHistory={(runId) => void loadAnalysisRun(runId)} onOpenAsset={openAsset} /> : null}
        {page === "quality" ? <QualityPage issues={issues} onUpdate={updateIssue} onOpen={openAsset} onOpenFile={openFile} /> : null}
        {page === "tasks" ? <TasksPage tasks={tasks} onProcess={() => setShowProcess(true)} onOpenCatalog={() => setPage("catalog")} onCancel={(taskId) => void cancelTask(taskId)} /> : null}
        {page === "settings" ? <SettingsPage settings={aiPersistedSettings} settingsState={aiSettingsState} settingsError={aiSettingsError} form={aiForm} draftDirty={aiDraftDirty} testResult={aiTestResult} busy={aiBusy} message={aiMessage} messageKind={aiMessageKind} resetBusy={resetBusy} resetMessage={resetMessage} resetMessageKind={resetMessageKind} fileInsightQueue={fileInsightQueue} onForm={(value) => { aiDraftDirtyRef.current = true; setAiDraftDirty(true); setAiForm(value); setAiTestResult(null); }} onSave={() => void saveAISettings()} onTest={() => void testAISettings()} onRetrySettings={() => void refreshAISettings()} onReset={(confirmation) => void resetProjectData(confirmation)} onBulkFileInsights={() => void bulkFileInsights()} /> : null}
        {page === "detail" && selected ? <DetailPage detail={selected} tab={detailTab} onTab={setDetailTab} tableLayer={tableLayer} onTableLayer={setTableLayer} tableOffset={tableOffset} onTableOffset={setTableOffset} tablePreview={tablePreview} textPreview={textPreview} semanticConfigured={health?.llm.enabled === true} semanticEnriching={semanticEnriching} semanticNotice={semanticNotice} onSemanticEnrich={requestSemanticEnrichment} onBack={() => setPage("catalog")} onUpdateIssue={updateIssue} /> : null}
        {page === "detail" && !selected ? <EmptyState title="正在加载资产" body="正在读取本地目录与画像信息。" /> : null}
        {page === "file-detail" && fileDetailState === "ready" && selectedFileContent ? <FileDetailPageV2 content={selectedFileContent} locator={fileLocator} fileSearch={fileSearch} onFileSearch={setFileSearch} onBack={() => setPage("catalog")} onOpenAsset={openAsset} onNavigate={navigateFile} onLoadMoreText={loadMoreFileText} textLoadingKey={fileTextLoadingKey} viewerMode={fileViewerMode} onViewerMode={setFileViewerMode} workspaceVersion={selectedFileRefreshVersion} fileInsightTask={selectedFileInsightTask} visionTask={selectedFileVisionTask} reprocessTask={selectedFileReprocessTask} onReprocess={() => void reprocessSelectedFile()} reprocessBusy={reprocessBusy} onQueueInsight={queueFileInsight} /> : null}
         {page === "file-detail" && fileDetailState === "idle" ? <EmptyState title="尚未选择文件" body="从数据目录打开一个源文件后，这里会展示它的主要内容。" /> : null}
         {page === "file-detail" && fileDetailState === "loading" ? <div className="loading-box file-detail-loading"><LoadingMessage loading label="正在加载文件" /></div> : null}
        {page === "file-detail" && fileDetailState === "error" ? <section className="file-detail-error"><strong>加载失败</strong><span>{fileDetailError || "无法读取文件详情。"}</span><button className="secondary-button" onClick={retryFile}>重试</button></section> : null}
        {page === "reports" ? <ReportsPageV2 files={files} reports={reports} selected={selectedReport} selectedFileIds={reportFileIds} selectAllCurrentFilter={reportSelectAll} reportType={reportType} title={reportTitle} purpose={reportPurpose} task={reportTaskId ? tasks.find((item) => item.taskId === reportTaskId) ?? null : null} loading={reportLoading} error={reportError} onToggleFile={toggleReportFile} onSelectAll={selectAllReportFiles} onClearSelection={clearReportFiles} onReportType={setReportType} onTitle={setReportTitle} onPurpose={setReportPurpose} onStart={() => void startReport()} onCancel={() => void cancelReport()} onOpen={(id) => void loadReport(id)} onExport={(format) => void exportReport(format)} onOpenAsset={openAsset} /> : null}
      </main>

      {showProcess ? <ProcessDialog source={taskSource} onSource={setTaskSource} visionMode={taskVisionMode} onVisionMode={setTaskVisionMode} visionEnabled={health?.llm.visionEnabled === true} autoFileInsight={taskAutoFileInsight} onAutoFileInsight={setTaskAutoFileInsight} onClose={() => setShowProcess(false)} onSubmit={startProcess} /> : null}
      {showSemanticConfirm && selected ? <SemanticConfirmDialog assetName={selected.displayName} onCancel={() => setShowSemanticConfirm(false)} onConfirm={() => void confirmSemanticEnrichment()} /> : null}
      {resetBusy ? <ResetProgressOverlay state={resetState} message={resetMessage} now={resetClock} /> : null}
      {!resetBusy && resetMessage && resetMessageKind && page !== "settings" ? <div className={`reset-result-banner ${resetMessageKind}`} role={resetMessageKind === "error" ? "alert" : "status"}>{resetMessage}</div> : null}
    </div>
  );
}

function NavButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button className={`nav-button ${active ? "active" : ""}`} onClick={onClick}>{children}</button>;
}

function ResetProgressOverlay({ state, message, now }: { state: ResetUiState | null; message: string; now: number }) {
  const elapsed = state ? Math.max(0, Math.floor((now - state.startedAt) / 1000)) : 0;
  const phase = state ? resetPhaseLabel(state.phase) : message || "正在清空项目资料";
  return <div className="reset-overlay" role="status" aria-live="polite"><div className="reset-overlay-card"><div className="eyebrow">PROJECT RESET</div><h2>正在清空项目资料</h2><strong className="reset-overlay-phase">{phase}</strong><span>{state?.serverUnavailable ? "本地服务正在重新启动，请稍候…" : `已用时 ${formatDuration(elapsed)}`}</span>{state?.requestId ? <small>请求已受理，正在等待本地服务恢复。</small> : null}</div></div>;
}

function LoadingMessage({ loading, label }: { loading: boolean; label: string }) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!loading) {
      setSlow(false);
      return;
    }
    const timer = window.setTimeout(() => setSlow(true), 1_500);
    return () => window.clearTimeout(timer);
  }, [loading]);
  return <span role="status">{slow ? "仍在处理中…" : label}</span>;
}

function aiStatusLabel(settings: AISettings | null): string {
  if (!settings) return "正在读取配置";
  return {
    NOT_CONFIGURED: "未配置，完全离线",
    INCOMPLETE: "配置未完成，保持离线",
    UNVERIFIED: "配置已保存，等待连接测试",
    CONNECTION_FAILED: "连接失败，保持离线",
    INVALID_CONFIGURATION: "配置无效，保持离线",
    ENABLED: "已连接，AI 已启用",
    CONFIGURED: "使用 .env 高级配置",
  }[settings.status] ?? settings.status;
}

type AnalysisPageProps = {
  health: HealthResponse | null;
  assets: AssetSummary[];
  selectedIds: string[];
  scope: AnalysisScopeKind;
  question: string;
  history: AnalysisRunSummary[];
  run: AnalysisRun | null;
  task: Task | null;
  loading: boolean;
  error: string;
  onQuestion: (value: string) => void;
  onScope: (value: AnalysisScopeKind) => void;
  onToggleAsset: (assetId: string) => void;
  onStart: () => void;
  onCancel: () => void;
  onHistory: (runId: string) => void;
  onOpenAsset: (assetId: string) => void;
};

function analysisModelStatus(health: HealthResponse | null): { label: string; tone: string } {
  if (!health) return { label: "正在读取 AI 状态", tone: "muted" };
  if (!health.llm.configured || ["NOT_CONFIGURED", "INCOMPLETE", "INVALID_CONFIGURATION"].includes(health.llm.status)) {
    return { label: "未配置 · 完全离线", tone: "muted" };
  }
  if (!health.llm.enabled) return { label: "已配置但不可用", tone: "review" };
  return { label: "可执行分析", tone: "good" };
}

function AnalysisPage({ health, assets, selectedIds, scope, question, history, run, task, loading, error, onQuestion, onScope, onToggleAsset, onStart, onCancel, onHistory, onOpenAsset }: AnalysisPageProps) {
  const status = analysisModelStatus(health);
  const running = task != null && ["queued", "running", "cancelling"].includes(task.status);
  return <section className="page-section analysis-page">
    <div className="page-heading"><div><div className="eyebrow">AI DATA ANALYSIS V1</div><h1>AI 分析</h1><p className="heading-note">一次问题 → 一次分析结果。模型只能通过本地 Search、Context 和 Safe SQL 获取当前工作区证据。</p></div></div>
    <div className="analysis-layout">
      <section className="panel analysis-input-panel">
        <div className="panel-title"><div><h2>提出一个问题</h2><span className="panel-note">仅基于已入库的数据表和文本内容，不进行通用知识问答。</span></div></div>
        <div className="analysis-form">
          <label className="field-label" htmlFor="analysis-question">分析问题</label>
          <textarea id="analysis-question" className="analysis-question" value={question} onChange={(event) => onQuestion(event.target.value)} placeholder="例如：2025 年各地区业务量有什么变化？" maxLength={2000} />
          <div className="analysis-scope-title">数据范围</div>
          <label className="analysis-scope-option"><input type="radio" checked={scope === "all"} onChange={() => onScope("all")} /> 全部已入库数据</label>
          <label className="analysis-scope-option"><input type="radio" checked={scope === "selected"} onChange={() => onScope("selected")} /> 用户选择的数据表或文本内容（最多 8 个）</label>
          {scope === "selected" ? <div className="analysis-asset-picker">{assets.length ? assets.map((asset) => <label className="analysis-asset-option" key={asset.assetId}><input type="checkbox" checked={selectedIds.includes(asset.assetId)} onChange={() => onToggleAsset(asset.assetId)} /><span><strong>{asset.effectiveDisplayName || asset.fallbackDisplayName}</strong><small>{asset.source.relativePath} · {typeLabel(asset.assetType)}</small></span></label>) : <span className="muted">暂无已入库资产。</span>}</div> : <p className="analysis-scope-note">系统会按需检索和加载有界上下文，不会一次发送整个数据库或全部文件内容。</p>}
          <div className="analysis-ai-status"><span>AI 状态</span><strong className={`status-${status.tone}`}>{status.label}</strong></div>
          <button className="primary-button analysis-start" disabled={loading || running || !question.trim() || !health?.llm.enabled || (scope === "selected" && !selectedIds.length)} onClick={onStart}>{loading ? "准备中…" : running ? "分析进行中…" : "开始分析"}</button>
          {running ? <button className="secondary-button analysis-cancel" onClick={onCancel}>取消分析</button> : null}
          {error ? <div className="analysis-error" role="alert">{error}</div> : null}
        </div>
      </section>
      <section className="analysis-output-column">
        {running && task ? <AnalysisProgress task={task} /> : null}
        {run ? <AnalysisResult run={run} onOpenAsset={onOpenAsset} /> : <div className="panel analysis-empty"><strong>{running ? "分析运行中" : "结果将在这里显示"}</strong><span>{running ? "本地正在按步骤获取证据并验证引用。" : "完成一次分析后，这里会展示结论、关键发现、来源、SQL 摘要和局限。"}</span></div>}
        <section className="panel analysis-history"><div className="panel-title"><div><h2>历史分析</h2><span className="panel-note">刷新页面后仍可查看已保存的单次分析结果。</span></div></div>{history.length ? <div className="analysis-history-list">{history.map((item) => <button type="button" className="analysis-history-item" key={item.analysis_run_id} onClick={() => onHistory(item.analysis_run_id)}><span><strong>{item.question}</strong><small>{formatDate(item.created_at)} · {analysisRunStatusLabel(item.status)}</small></span><span className="text-button">查看</span></button>)}</div> : <div className="analysis-history-empty">暂无历史分析。</div>}</section>
      </section>
    </div>
  </section>;
}

function AnalysisProgress({ task }: { task: Task }) {
  const step = task.currentStep ?? 0;
  const max = task.maxSteps ?? 6;
  return <div className="panel analysis-progress"><div className="panel-title"><div><h2>分析运行中</h2><span className="panel-note">当前阶段：{taskStageLabel(task.currentStage, task.currentSubstage)}</span></div><strong>{step} / {max}</strong></div><div className="progress-track"><span style={{ width: `${task.progress * 100}%` }} /></div><div className="analysis-progress-meta"><span>当前步骤：{task.currentStep ?? 0} / {task.maxSteps ?? 6}</span><span>已用时 {number(task.elapsedSeconds)} 秒</span><span>{task.status === "cancelling" ? "正在取消" : `${Math.round(task.progress * 100)}%`}</span></div></div>;
}

function analysisRunStatusLabel(status: string): string {
  return {
    running: "运行中",
    completed: "已完成",
    insufficient_evidence: "证据不足",
    failed: "失败",
    cancelled: "已取消",
  }[status] ?? status;
}

function evidenceSourceLabel(evidence: AnalysisEvidence): string {
  const source = evidence.source ?? {};
  const provenance = evidence.provenance ?? {};
  const name = String(source.relativePath ?? evidence.display_name ?? evidence.asset_id ?? "本地证据");
  const sheetName = source.sheetName ?? provenance.sheetName;
  const pageNumber = source.pageNumber ?? provenance.pageNumber;
  const location = [sheetName ? `Sheet ${sheetName}` : "", pageNumber ? `Page ${pageNumber}` : ""].filter(Boolean).join(" · ");
  return location ? `${name} · ${location}` : name;
}

function EvidenceView({ evidence, onOpenAsset }: { evidence: AnalysisEvidence; onOpenAsset: (assetId: string) => void }) {
  const assetId = evidence.asset_id ?? (Array.isArray(evidence.asset_ids) ? evidence.asset_ids[0] : undefined);
  const rows = Array.isArray(evidence.rows) ? evidence.rows : [];
  const columns = Array.isArray(evidence.columns) ? evidence.columns : [];
  return <div className="analysis-evidence"><div className="analysis-evidence-heading"><span>{evidenceSourceLabel(evidence)}</span>{assetId ? <button className="text-button" onClick={() => onOpenAsset(assetId)}>查看来源</button> : null}</div>{evidence.kind === "sql_result" && columns.length ? <div className="table-wrap analysis-evidence-table"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{rows.slice(0, 20).map((row, index) => <tr key={index}>{columns.map((column) => <td key={column}>{displayValue(row[column])}</td>)}</tr>)}</tbody></table></div> : null}{evidence.snippet || evidence.text ? <p className="analysis-evidence-snippet">{evidence.snippet || String(evidence.text).slice(0, 1000)}</p> : null}{evidence.kind === "sql_result" ? <small>{number(evidence.row_count ?? rows.length)} 行{evidence.truncated ? " · 结果已截断" : ""}</small> : null}</div>;
}

function AnalysisResult({ run, onOpenAsset }: { run: AnalysisRun; onOpenAsset: (assetId: string) => void }) {
  const evidence = run.evidence_manifest ?? {};
  const referenced = new Set(run.findings.flatMap((finding) => finding.evidence_ids));
  const referencedEvidence = Array.from(referenced).map((id) => evidence[id]).filter(Boolean);
  const sqlCount = Object.values(evidence).filter((item) => item.kind === "sql_result").length;
  return <div className="analysis-result-stack"><section className="panel analysis-result"><div className="panel-title"><div><div className="eyebrow">ANALYSIS RESULT</div><h2>分析结论</h2></div><span className={`analysis-result-status ${run.status}`}>{analysisRunStatusLabel(run.status)}</span></div><p className="analysis-answer">{run.answer || "当前数据不足以支持该结论。"}</p><div className="analysis-meta">{run.model_identity.model ? `模型：${String(run.model_identity.model)}` : "模型：provider-neutral"} · {run.steps_used} / {run.max_steps} 步 · {run.provider_calls} 次模型调用</div></section>
    <section className="panel analysis-findings"><div className="panel-title"><h2>关键发现</h2><span>{run.findings.length} 条有依据发现</span></div>{run.findings.length ? <div className="analysis-finding-list">{run.findings.map((finding, index) => <article className="analysis-finding" key={`${finding.statement}-${index}`}><strong>{finding.statement}</strong><small>{finding.support_level === "inference" ? "分析推断" : "数据直接支持"} · 证据 {finding.evidence_ids.join(", ")}</small></article>)}</div> : <div className="analysis-history-empty">没有可验证的关键发现。</div>}{run.unverified_findings.length ? <div className="analysis-unverified"><strong>无法确认</strong>{run.unverified_findings.map((finding, index) => <p key={`${finding.statement}-${index}`}>{finding.statement}</p>)}</div> : null}</section>
    <section className="panel analysis-sources"><div className="panel-title"><div><h2>使用的数据来源</h2><span>{referencedEvidence.length} 个证据 · {sqlCount} 个 SQL 结果</span></div></div>{referencedEvidence.length ? referencedEvidence.map((item) => <EvidenceView key={item.evidence_id} evidence={item} onOpenAsset={onOpenAsset} />) : <div className="analysis-history-empty">没有已解析的来源证据。</div>}</section>
    {run.limitations.length ? <section className="panel analysis-limitations"><div className="panel-title"><h2>局限 / 无法确认事项</h2></div><ul>{run.limitations.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul></section> : null}
  </div>;
}

function FileInsightQueueCard({ queue, enabled, busy, onToggle, onBulk }: { queue: FileInsightQueueSummary; enabled: boolean; busy: boolean; onToggle: () => void; onBulk: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const pending = queue.pendingReady ?? 0;
  return <section className="panel settings-side-card file-insight-queue-card">
    <div className="settings-side-label">AI 文件整理</div>
    <div className="settings-field settings-toggle-field compact-toggle"><div><span className="field-label">自动整理新文件</span><small>本地文件处理完成后，按有界上下文进入独立队列。</small></div><button type="button" role="switch" aria-checked={enabled} className={`settings-toggle ${enabled ? "on" : ""}`} onClick={onToggle}><span /></button></div>
    <div className="file-insight-queue-count">待整理：<strong>{number(pending)}</strong> 个文件</div>
    {confirming ? <div className="file-insight-confirm" role="dialog" aria-label="确认批量 AI 文件整理"><p>将向当前配置的 AI 服务发送每个文件的有界整理上下文。</p><small>不会上传整个文件；已有有效缓存、已排队或正在处理的文件会跳过。</small><div className="dialog-actions"><button type="button" className="secondary-button" onClick={() => setConfirming(false)}>取消</button><button type="button" className="primary-button" disabled={busy || pending === 0} onClick={() => { setConfirming(false); onBulk(); }}>确认整理</button></div></div> : <button type="button" className="secondary-button file-insight-bulk-button" disabled={busy || pending === 0} onClick={() => setConfirming(true)}>整理所有未整理文件</button>}
    <div className="file-insight-queue-stats" aria-live="polite"><span>已完成 {number(queue.completed)} / {number(queue.total)}</span><span>等待队列 {number(queue.queued)}</span><span>请求模型 {number(queue.requestingModel ?? queue.running)}</span><span>验证 {number(queue.validating ?? 0)}</span><span>保存 {number(queue.persisting ?? 0)}</span><span>失败 {number(queue.failed)}</span></div>
  </section>;
}

function SettingsPage({ settings, settingsState, settingsError, form, draftDirty, testResult, busy, message, messageKind, resetBusy, resetMessage, resetMessageKind, fileInsightQueue, onForm, onSave, onTest, onRetrySettings, onReset, onBulkFileInsights }: {
  settings: AISettings | null;
  settingsState: "idle" | "loading" | "ready" | "error";
  settingsError: string;
  form: { baseUrl: string; apiKey: string; model: string; timeout: string; visionEnabled: boolean; fileInsightEnabled: boolean; clearApiKey: boolean };
  draftDirty: boolean;
  testResult: { kind: "success" | "error"; message: string; structuredOutputOk?: boolean; category?: string } | null;
  busy: boolean;
  message: string;
  messageKind: "" | "success" | "error";
  resetBusy: boolean;
  resetMessage: string;
  resetMessageKind: "" | "success" | "error";
  fileInsightQueue: FileInsightQueueSummary;
  onForm: (value: { baseUrl: string; apiKey: string; model: string; timeout: string; visionEnabled: boolean; fileInsightEnabled: boolean; clearApiKey: boolean }) => void;
  onSave: () => void;
  onTest: () => void;
  onRetrySettings: () => void;
  onReset: (confirmation: string) => void;
  onBulkFileInsights: () => void;
}) {
  return <section className="page-section settings-page"><div className="page-heading"><div><div className="eyebrow">SETTINGS</div><h1>设置</h1><p className="heading-note">AI 模型是可选能力；文件扫描、提取、清洗和查询始终可以离线运行。</p></div></div>
    {settingsState === "loading" && !settings ? <div className="settings-load-state"><LoadingMessage loading label="正在读取 AI 配置…" /></div> : null}
    {settingsState === "error" ? <div className="settings-load-error" role="alert"><span>{settingsError || "无法读取 AI 配置。"}</span><button type="button" className="secondary-button" onClick={onRetrySettings}>重试</button></div> : null}
    <div className="settings-layout">
      <section className="panel settings-panel"><PanelTitle title="AI 模型" /><div className="settings-status">状态：<strong>{aiStatusLabel(settings)}</strong>{settings?.apiKeyConfigured ? " · API Key 已保存" : " · 未保存 API Key"}{draftDirty ? " · 有未保存草稿" : ""}</div>
        <div className="settings-form">
        <div className="settings-field settings-toggle-field"><div><span className="field-label">视觉提取</span><small>开启后，JPG、PNG 和扫描 PDF 可发送给当前配置的 AI 服务处理。</small></div><button type="button" role="switch" aria-checked={form.visionEnabled} className={`settings-toggle ${form.visionEnabled ? "on" : ""}`} onClick={() => onForm({ ...form, visionEnabled: !form.visionEnabled })}><span /></button></div>
        <p className="settings-note">未配置：完全离线。配置完成但未启用 Vision：仅允许文本 Semantic / Analysis。启用视觉提取后，仍只在用户明确选择时发送图像或扫描 PDF 页面。</p>
        <label className="settings-field"><span className="field-label">服务地址（Base URL）</span><input value={form.baseUrl} onChange={(event) => onForm({ ...form, baseUrl: event.target.value })} placeholder="https://example.com/v1" autoComplete="url" /><small>OpenAI-compatible 服务地址；不会自动访问公共服务。</small></label>
        <label className="settings-field"><span className="field-label">API Key</span><input type="password" value={form.apiKey} onChange={(event) => onForm({ ...form, apiKey: event.target.value, clearApiKey: false })} placeholder={settings?.apiKeyConfigured && !form.clearApiKey ? "留空保持已保存密钥" : "输入 API Key"} autoComplete="new-password" /><small>{settings?.apiKeyConfigured && !form.apiKey && !form.clearApiKey ? "已保存密钥；页面不会回显。留空保存时继续保留。" : "密钥只用于当前草稿和请求，不进入 Registry、任务或日志。"}</small>{settings?.apiKeyConfigured || form.apiKey ? <button type="button" className="text-button settings-key-clear" onClick={() => onForm({ ...form, apiKey: "", clearApiKey: true })}>{form.clearApiKey ? "已选择清除密钥" : "清除已保存密钥"}</button> : null}</label>
        <label className="settings-field"><span className="field-label">模型名称</span><input value={form.model} onChange={(event) => onForm({ ...form, model: event.target.value })} placeholder="qwen3.6-35b-a3b" /></label>
        <label className="settings-field"><span className="field-label">请求超时（秒）</span><input type="number" min="1" max="600" value={form.timeout} onChange={(event) => onForm({ ...form, timeout: event.target.value })} /></label>
        <div className="settings-actions"><button type="button" className="primary-button" disabled={busy} onClick={onTest}>{busy ? "处理中..." : "测试连接"}</button><button type="button" className="secondary-button" disabled={busy} onClick={onSave}>保存配置</button></div>{busy ? <div className="loading-box settings-request-loading"><LoadingMessage loading label="正在提交设置请求…" /></div> : null}
        {message ? <div className={`settings-status ${messageKind}`} role="status">{message}</div> : null}
        {testResult ? <div className={`settings-status ${testResult.kind}`} role="status">{testResult.message}{testResult.category ? `（${testResult.category}）` : ""}</div> : null}
        <p className="settings-note">测试连接只发送最小固定请求，不发送工作区内容。连接状态仅表示当前配置的服务是否返回了兼容响应。</p>
        </div>
      </section>
      <aside className="settings-rail">
        <FileInsightQueueCard queue={fileInsightQueue} enabled={form.fileInsightEnabled} busy={busy} onToggle={() => onForm({ ...form, fileInsightEnabled: !form.fileInsightEnabled })} onBulk={onBulkFileInsights} />
        <section className="panel settings-side-card"><div className="settings-side-label">当前状态</div><strong>{aiStatusLabel(settings)}</strong><p>{settings?.apiKeyConfigured ? "项目配置中已有密钥，但密钥本身不会回显。" : "尚未保存密钥；当前仍可保持完全离线。"}</p>{draftDirty ? <span className="settings-side-badge">有未保存草稿</span> : <span className="settings-side-badge quiet">草稿与已保存配置一致</span>}</section>
        <section className="panel settings-side-card"><div className="settings-side-label">最近测试</div>{testResult ? <><strong className={testResult.kind}>{testResult.kind === "success" ? "基础连接成功" : "连接失败"}</strong><p>{testResult.message}</p></> : <p>尚未测试当前草稿。测试不会保存配置，也不会发送工作区内容。</p>}</section>
        <section className="panel settings-side-card"><div className="settings-side-label">离线边界</div><p>文件扫描、PDF/DOCX/XLSX 提取、目录和查询不依赖 AI。模型只在用户明确测试或触发高级能力时访问配置的地址。</p></section>
      </aside>
    </div>
    <ProjectResetPanel busy={resetBusy} message={resetMessage} messageKind={resetMessageKind} onReset={onReset} />
  </section>;
}

function ProjectResetPanel({ busy, message, messageKind, onReset }: { busy: boolean; message: string; messageKind: "" | "success" | "error"; onReset: (confirmation: string) => void }) {
  const [confirmation, setConfirmation] = useState("");
  const confirmed = confirmation.trim() === "清空";
  return <section className="panel danger-panel"><PanelTitle title="危险操作" /><div className="danger-panel-body"><strong>清空当前项目资料</strong><p>清除当前项目生成的 Registry、目录、产物、缓存和任务结果；原始资料、运行时、模型与 AI 配置会保留。正在运行的 server.log 作为活动日志可能保留，不影响清空完成。</p><label className="settings-field"><span className="field-label">请输入“清空”确认</span><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="清空" autoComplete="off" /></label><button type="button" className="danger-button" disabled={!confirmed || busy} onClick={() => onReset(confirmation)}>{busy ? "正在清空..." : "清空当前项目资料"}</button>{message ? <div className={`settings-status ${busy ? "" : messageKind}`} role={busy || messageKind !== "error" ? "status" : "alert"}>{message}</div> : null}</div></section>;
}

function OverviewPage({ overview, health, tasks, onProcess, onNavigate, onOpenAsset }: { overview: Overview; health: HealthResponse | null; tasks: Task[]; onProcess: () => void; onNavigate: (page: Page) => void; onOpenAsset: (id: string) => void }) {
  const totalAssets = overview.tableAssets + overview.textAssets;
  const formatItems = Object.entries(overview.formats);
  return <section className="page-section">
    <div className="page-heading"><div><div className="eyebrow">PROJECT OVERVIEW</div><h1>项目资料</h1><p className="heading-note">本地目录处理状态与数据资产概览</p></div><button className="primary-button" onClick={onProcess}>处理新目录 <span>＋</span></button></div>
    <div className="summary-line"><strong>{number(overview.files)}</strong> 个文件 <span>·</span> <strong>{number(overview.tableAssets)}</strong> 个表格资产 <span>·</span> <strong>{number(overview.textAssets)}</strong> 个文本资产</div>
    <div className="overview-grid">
      <section className="panel status-panel"><PanelTitle title="处理状态" action={<button className="text-button" onClick={() => onNavigate("tasks")}>查看任务 →</button>} /><div className="status-grid"><Metric label="可直接使用" value={overview.ready} tone="good" /><Metric label="需要审核" value={overview.needsReview} tone="review" /><Metric label="失败 / 不可用" value={overview.failed + overview.unusable} tone="bad" /><Metric label="不支持" value={overview.unsupported} tone="muted" /></div></section>
      <section className="panel composition-panel"><PanelTitle title="资产组成" /><div className="composition-bar"><span className="bar-table" style={{ width: `${totalAssets ? (overview.tableAssets / totalAssets) * 100 : 0}%` }} /><span className="bar-text" style={{ width: `${totalAssets ? (overview.textAssets / totalAssets) * 100 : 0}%` }} /></div><div className="legend"><span><i className="legend-dot bar-table" />表格 {number(overview.tableAssets)}</span><span><i className="legend-dot bar-text" />文本 {number(overview.textAssets)}</span></div><div className="format-list">{formatItems.length ? formatItems.slice(0, 6).map(([format, count]) => <span key={format}><b>{format.toUpperCase()}</b>{number(count)}</span>) : <span className="muted">尚未处理资料</span>}</div></section>
    </div>
    <section className="panel recent-panel"><PanelTitle title="最近任务" action={<button className="text-button" onClick={() => onNavigate("tasks")}>全部任务 →</button>} />{tasks.length ? <div className="task-table">{tasks.slice(0, 5).map((task) => <TaskRow key={task.taskId} task={task} />)}</div> : <EmptyState title="尚未处理资料" body="输入一个本地科研资料目录，开始建立数据目录。" compact />}</section>
    <div className="info-strip"><span className="info-icon">i</span><span>AI 服务状态：<strong>{health?.llm.enabled ? "已连接并启用" : health?.llm.configured ? "已配置但尚未验证" : "尚未配置模型"}</strong>。默认处理完全离线；AI 文件整理只在设置中开启后、文件本地处理完成时进入后台队列。</span></div>
  </section>;
}

function Metric({ label, value, tone }: { label: string; value: number; tone: "good" | "review" | "bad" | "muted" }) {
  return <div className="metric"><div className={`metric-value ${tone}`}>{number(value)}</div><div className="metric-label">{label}</div></div>;
}

function PanelTitle({ title, action }: { title: string; action?: React.ReactNode }) { return <div className="panel-title"><h2>{title}</h2>{action}</div>; }

function CatalogPage({ files, total, offset, loading, error, onRetry, type, quality, format, query, formats, onType, onQuality, onFormat, onQuery, onOffset, onOpen, onProcess }: { files: FileSummary[]; total: number; offset: number; loading: boolean; error: string; onRetry: () => void; type: "" | "table_file" | "document" | "image" | "unprocessed" | "failed"; quality: "" | QualityStatus; format: string; query: string; formats: Record<string, number>; onType: (value: "" | "table_file" | "document" | "image" | "unprocessed" | "failed") => void; onQuality: (value: "" | QualityStatus) => void; onFormat: (value: string) => void; onQuery: (value: string) => void; onOffset: (value: number) => void; onOpen: (id: string) => void; onProcess: () => void }) {
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">DATA CATALOG</div><h1>数据目录</h1><p className="heading-note">一级列表按源文件展示；文件详情中查看正文、表格和精确来源。</p></div><button className="primary-button" onClick={onProcess}>处理新目录 <span>＋</span></button></div>
    <div className="filter-panel"><div className="segmented"><button className={!type ? "selected" : ""} onClick={() => onType("")}>全部</button><button className={type === "table_file" ? "selected" : ""} onClick={() => onType("table_file")}>表格文件</button><button className={type === "document" ? "selected" : ""} onClick={() => onType("document")}>文档</button><button className={type === "image" ? "selected" : ""} onClick={() => onType("image")}>图片</button></div><select value={type === "unprocessed" || type === "failed" ? type : ""} onChange={(event) => onType(event.target.value as "" | "unprocessed" | "failed")}><option value="">处理状态</option><option value="unprocessed">未处理</option><option value="failed">失败</option></select><select value={quality} onChange={(event) => onQuality(event.target.value as "" | QualityStatus)}><option value="">全部质量</option><option value="ready">可直接使用</option><option value="needs_review">需要审核</option><option value="unusable">不可用</option></select><select value={format} onChange={(event) => onFormat(event.target.value)}><option value="">全部格式</option>{Object.keys(formats).sort().map((item) => <option key={item} value={item}>{item.toUpperCase()}</option>)}</select><label className="search-box"><span>⌕</span><input value={query} onChange={(event) => onQuery(event.target.value)} placeholder="搜索文件名或来源路径" /></label></div>
    {error ? <div className="catalog-error" role="alert"><span>{error}</span><button type="button" className="secondary-button" onClick={onRetry}>重试</button></div> : null}<div className="list-meta"><span>{loading ? <LoadingMessage loading label="读取中…" /> : `${number(total)} 个文件`}</span><span>文件是一级目录单位；数据表和文本内容保留在详情页</span></div>{files.length ? <div className="asset-list">{files.map((file) => <FileListItem file={file} key={file.fileId} onOpen={onOpen} />)}</div> : error ? null : <EmptyState title="暂无文件" body="处理一个本地资料目录后，源文件会出现在这里。" />}
    {total > 25 ? <Pagination offset={offset} limit={25} total={total} onOffset={onOffset} /> : null}</section>;
}

function FileListItem({ file, onOpen }: { file: FileSummary; onOpen: (id: string) => void }) {
  const quality = file.qualityStatus as QualityStatus;
  const insightStatus = file.fileInsightStatus;
  const insightLabel = fileInsightStatusLabel(insightStatus);
  const processingLabel = file.processingStatus === "no_evidence" || file.evidenceStatus === "no_evidence" ? "暂无本地证据" : file.processingStatus === "ready" ? "可查看" : file.processingStatus === "failed" ? "处理失败" : file.processingStatus === "unsupported" ? "暂不支持" : file.processingStatus === "processing" ? "处理中" : "未处理";
  return <button className="asset-list-item file-list-item" onClick={() => onOpen(file.fileId)}><div className="asset-type-mark">文</div><div className="asset-main"><div className="asset-name">{file.displayName}</div><div className="asset-source">{file.relativePath} <span>·</span> {file.format?.toUpperCase() || "UNKNOWN"}</div></div><div className="file-counts">{fileSize(file.sizeBytes)} · {file.textPages} 页文本 · {file.tableAssets} 个表格</div><div className={`quality-pill ${quality}`}>{qualityLabel(quality)}</div><div className={`file-status-pill ${file.processingStatus}`}>{processingLabel}</div>{file.processingStatus === "ready" ? <div className={`semantic-pill ${insightStatus}`}>{insightLabel}</div> : null}<span className="chevron">›</span></button>;
}

function FileDetailPage({ content, onBack, onOpenAsset, onLoadMoreText, textLoadingKey }: { content: FileContentResponse; onBack: () => void; onOpenAsset: (id: string) => void; onLoadMoreText: (blockKey: string, assetId: string, offset: number) => void; textLoadingKey: string | null }) {
  const file = content.file;
  const tableBlocks = content.sections.flatMap((section) => section.blocks).filter((block): block is Extract<FileContentBlock, { type: "table" }> => block.type === "table");
  const textAssetIds = Array.from(new Set(content.sections.flatMap((section) => section.blocks).filter((block): block is Extract<FileContentBlock, { type: "text" }> => block.type === "text").map((block) => block.assetId)));
  const candidateCount = tableBlocks.filter((block) => block.candidate).length;
  const isImage = ["jpg", "jpeg", "png"].includes(file.format.toLowerCase());
  const sourcePreview = content.sourcePreview;
  const sourceImageUrl = sourcePreview?.available && sourcePreview.kind === "image" ? sourcePreview.url : null;
  return <section className="page-section file-detail-section"><button className="back-button" onClick={onBack}>← 数据目录</button><div className="detail-heading"><div className="asset-type-mark large">文</div><div><div className="eyebrow">文件详情</div><h1>{file.displayName}</h1><div className="detail-source">{file.relativePath} · {file.format.toUpperCase()} · {fileSize(file.sizeBytes)}</div></div><div className={`quality-pill ${file.qualityStatus as QualityStatus}`}>{qualityLabel(file.qualityStatus as QualityStatus)}</div></div><div className="file-detail-grid"><section className="panel file-pages-panel"><div className="panel-title"><div><h2>文件内容</h2><span className="panel-note">按源文件顺序直接阅读有界正文与表格预览</span></div><span>{file.textPages} 页文本 · {file.tableAssets} 个表格</span></div>{sourceImageUrl ? <div className="source-image-preview"><div className="source-preview-label">原始图片</div><img src={sourceImageUrl} alt={`原始图片：${file.displayName}`} loading="lazy" /></div> : null}{isImage ? <div className="candidate-notice">检测到 {candidateCount} 个候选表格；候选结果需要结合原图和质量信息核对。</div> : candidateCount ? <div className="candidate-notice">检测到 {candidateCount} 个候选表格；候选结果需要结合原页面和质量信息核对。</div> : null}{content.truncated ? <div className="content-limit-notice">内容较长，当前页面先展示有界连续阅读预览，可按段继续加载；完整 raw / normalized 数据和 provenance 仍可从技术详情详情打开。</div> : null}{content.sections.length ? <div className="file-pages">{content.sections.map((section) => <article className="file-page" key={section.sectionId}><div className="file-page-title"><span>{section.label}</span><span className="panel-note">{section.blocks.length} 个阅读段</span></div>{sourcePreview?.available && sourcePreview.kind === "pdf_page" && section.pageNumber != null ? <details className="source-page-preview"><summary>原始页面预览</summary><img src={`${sourcePreview.url}?page=${section.pageNumber}`} alt={`${file.displayName} 第 ${section.pageNumber} 页`} loading="lazy" /></details> : null}{section.blocks.map((block, index) => block.type === "text" ? <div className="file-content-text" key={`${block.assetId}-text-${index}`}><p>{block.text || "（空文本）"}</p>{block.truncated ? <><small>文本预览已截断</small><button type="button" className="text-button continuation-button" disabled={textLoadingKey === `${section.sectionId}:${index}`} onClick={() => onLoadMoreText(`${section.sectionId}:${index}`, block.assetId, block.nextTextOffset ?? block.text.length)}>{textLoadingKey === `${section.sectionId}:${index}` ? "正在加载…" : "继续加载"}</button></> : null}</div> : <FileContentTableBlock block={block} onOpenAsset={onOpenAsset} key={`${block.assetId}-table-${index}`} />)}</article>)}</div> : <EmptyState compact title="暂无可展示内容" body="当前文件没有可用于主视图的正文或表格预览。" />}</section><aside className="panel file-side-panel"><div className="panel-title"><h2>技术详情</h2><span>{number(textAssetIds.length)} 个文本 · {number(tableBlocks.length)} 个表格</span></div>{textAssetIds.length ? <div className="file-table-list file-text-asset-list">{textAssetIds.map((assetId) => <button className="file-table-item" key={assetId} onClick={() => onOpenAsset(assetId)}><strong>文本技术详情</strong><small>查看完整资产、raw / normalized 与 provenance</small></button>)}</div> : null}{tableBlocks.length ? <div className="file-table-list">{tableBlocks.map((block) => <button className="file-table-item" key={block.assetId} onClick={() => onOpenAsset(block.assetId)}><strong>{block.displayName || "表格资产"}</strong><small>{block.candidate ? "候选表格" : "表格"} · {block.sheetName || (block.pageNumber != null ? `第 ${block.pageNumber} 页` : "文件内容")}</small></button>)}</div> : !textAssetIds.length ? <EmptyState compact title="暂无技术详情" body="该文件当前没有可打开的文本或表格资产。" /> : null}<div className="file-source-box"><h2>来源</h2><dl><dt>SHA-256</dt><dd>{file.sha256 || "—"}</dd><dt>file_id</dt><dd>{file.fileId}</dd><dt>AI 状态</dt><dd>{file.semanticStatus === "enriched" ? "已整理" : "未整理"}</dd></dl></div></aside></div></section>;
}

function LegacyFileDetailPageV1({ content, locator, onBack, onOpenAsset, onNavigate, onLoadMoreText, textLoadingKey }: { content: FileContentResponse; locator: FileLocator; onBack: () => void; onOpenAsset: (id: string) => void; onNavigate: (locator: FileLocator) => void; onLoadMoreText: (blockKey: string, assetId: string, offset: number) => void; textLoadingKey: string | null }) {
  const file = content.file;
  const navigation = content.navigation ?? { kind: "continuous", current: null, total: null };
  const [mode, setMode] = useState<"reading" | "source">(() => ["pdf", "png", "jpg", "jpeg"].includes(file.format.toLowerCase()) ? "source" : "reading");
  const [searchInput, setSearchInput] = useState("");
  const [searchResults, setSearchResults] = useState<SearchOccurrence[]>([]);
  const [searchTotal, setSearchTotal] = useState(0);
  const [searchPages, setSearchPages] = useState<number[]>([]);
  const [searchSections, setSearchSections] = useState<string[]>([]);
  const [searchIndex, setSearchIndex] = useState(-1);
  const [searching, setSearching] = useState(false);
  const [insight, setInsight] = useState<FileInsight | null>(null);
  const sourcePreview = content.sourcePreview;
  const currentPage = navigation.kind === "page" && typeof navigation.current === "number" ? navigation.current : null;
  const totalPages = navigation.kind === "page" ? Number(navigation.total ?? 0) : 0;
  const tables = content.sections.flatMap((section) => section.blocks).filter((block): block is Extract<FileContentBlock, { type: "table" }> => block.type === "table");
  const textCount = new Set(content.sections.flatMap((section) => section.blocks).filter((block) => block.type === "text").map((block) => block.assetId)).size;
  const noEvidence = file.processingStatus === "no_evidence" || file.evidenceStatus === "no_evidence";
  const fileInsightTask: Task | null = null;
  useEffect(() => {
    let active = true;
    const load = () => void api.fileInsight(file.fileId).then((value) => {
      if (!active) return;
      setInsight(value);
    }).catch(() => { if (active) setInsight({ fileId: file.fileId, status: "not_started", insight: null, metadata: null }); });
    load();
    return () => { active = false; };
  }, [file.fileId]);

  useEffect(() => {
    if (locator.textAssetId == null || locator.textOffset == null) return;
    const textBlocks = content.sections.flatMap((section) => section.blocks.map((block, index) => ({ block, key: `${section.sectionId}:${index}` })));
    const target = textBlocks.find((item) => item.block.type === "text" && item.block.assetId === locator.textAssetId);
    if (!target || target.block.type !== "text") return;
    const blockStart = target.block.textOffset ?? 0;
    const blockEnd = blockStart + target.block.text.length;
    if (locator.textOffset >= blockEnd && target.block.truncated) {
      if (textLoadingKey !== target.key) onLoadMoreText(target.key, target.block.assetId, target.block.nextTextOffset ?? blockEnd);
      return;
    }
    if (locator.textOffset < blockStart || locator.textOffset > blockEnd) return;
    const textOnlyIndex = textBlocks.filter((item) => item.block.type === "text").findIndex((item) => item.key === target.key);
    const node = document.querySelectorAll<HTMLDivElement>(".file-content-text")[textOnlyIndex] ?? null;
    if (node) {
      node.classList.add("locator-highlight");
      node.scrollIntoView({ behavior: "smooth", block: "center" });
      const timer = window.setTimeout(() => {
        node.classList.remove("locator-highlight");
      }, 2_200);
      return () => window.clearTimeout(timer);
    }
  }, [content, locator.textAssetId, locator.textOffset, onLoadMoreText, textLoadingKey]);

  const navigateToSearchResult = (result: SearchOccurrence) => {
    onNavigate({
      page: result.page ?? undefined,
      sheet: result.sheet ?? undefined,
      textAssetId: result.assetId,
      textOffset: result.startOffset,
      matchStart: result.startOffset,
      matchEnd: result.endOffset,
    });
  };

  const runFileSearch = async () => {
    const value = searchInput.trim();
    if (!value) return;
    setSearching(true);
    try {
      const response = await api.fileSearch(file.fileId, value);
      setSearchResults(response.results);
      setSearchTotal(response.totalOccurrences);
      setSearchPages(response.matchedPages);
      setSearchSections(response.matchedSections);
      setSearchIndex(response.results.length ? 0 : -1);
      const first = response.results[0];
      if (first) navigateToSearchResult(first);
    } catch (cause) {
      setSearchResults([]);
      setSearchTotal(0);
      setSearchPages([]);
      setSearchSections([]);
    } finally {
      setSearching(false);
    }
  };

  const moveSearch = (delta: number) => {
    if (!searchResults.length) return;
    const next = (searchIndex + delta + searchResults.length) % searchResults.length;
    setSearchIndex(next);
    const result = searchResults[next];
    navigateToSearchResult(result);
  };

  const queueInsight = async () => {
    setInsight({ fileId: file.fileId, status: "queued", insight: null, metadata: null });
    try {
      await api.queueFileInsight(file.fileId);
    } catch {
      setInsight({ fileId: file.fileId, status: "failed", insight: null, metadata: null });
    }
  };

  const movePage = (delta: number) => {
    if (currentPage == null || !totalPages) return;
    onNavigate({ page: Math.max(1, Math.min(totalPages, currentPage + delta)) });
  };
  const jumpPage = (value: string) => {
    const parsed = Number(value);
    if (Number.isInteger(parsed) && parsed >= 1 && parsed <= totalPages) onNavigate({ page: parsed });
  };
  const nativeSource = mode === "source" && sourcePreview?.available && sourcePreview.sourceUrl
    ? sourcePreview.sourceUrl
    : null;
  const openSource = !nativeSource && mode === "source" && sourcePreview?.available && sourcePreview.kind === "pdf_page" && currentPage != null
    ? `${sourcePreview.url}?page=${currentPage}`
    : !nativeSource && mode === "source" && sourcePreview?.available && sourcePreview.kind === "image" ? sourcePreview.url : null;

  return <section className="page-section file-detail-section"><button className="back-button" onClick={onBack}>← 数据目录</button><div className="detail-heading"><div className="asset-type-mark large">文</div><div><div className="eyebrow">文件详情</div><h1>{file.displayName}</h1><div className="detail-source">{file.relativePath} · {file.format.toUpperCase()} · {fileSize(file.sizeBytes)}</div></div><div className={`quality-pill ${file.qualityStatus as QualityStatus}`}>{qualityLabel(file.qualityStatus as QualityStatus)}</div></div>
    <div className="file-viewer-toolbar"><div className="viewer-navigation">{navigation.kind === "page" ? <><button className="secondary-button" disabled={currentPage === 1} onClick={() => movePage(-1)}>上一页</button><strong>Page {currentPage} / {totalPages}</strong><button className="secondary-button" disabled={currentPage === totalPages} onClick={() => movePage(1)}>下一页</button><input aria-label="跳转页码" type="number" min={1} max={totalPages} value={currentPage ?? 1} onChange={(event) => jumpPage(event.target.value)} /></> : navigation.kind === "sheet" ? <div className="sheet-tabs">{(navigation.items ?? []).map((name) => <button key={name} className={navigation.current === name ? "selected" : ""} onClick={() => onNavigate({ sheet: name })}>{name}</button>)}</div> : <strong>{navigation.kind === "document" ? "连续文档" : navigation.kind === "image" ? "单图" : "连续阅读"}</strong>}</div><form className="file-search-box" onSubmit={(event) => { event.preventDefault(); void runFileSearch(); }}><input aria-label="查找当前文件" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="查找当前文件" /><button type="submit" className="secondary-button">{searching ? "查找中" : "查找"}</button>{searchResults.length ? <><button type="button" className="text-button" onClick={() => moveSearch(-1)}>上一个</button><span>{searchIndex + 1} / {searchResults.length}</span><button type="button" className="text-button" onClick={() => moveSearch(1)}>下一个</button></> : null}</form><div className="viewer-mode-switch"><button className={mode === "source" ? "selected" : ""} onClick={() => setMode("source")}>原始页面</button><button className={mode === "reading" ? "selected" : ""} onClick={() => setMode("reading")}>整理阅读</button></div></div>
    <div className="file-detail-grid"><section className="panel file-pages-panel">{openSource ? <div className="source-image-preview"><div className="source-preview-label">原始来源</div><img src={openSource} alt={`${file.displayName} source`} /></div> : null}{mode === "reading" ? <>{content.sections.length ? <div className="file-pages">{content.sections.map((section) => <article className="file-page" key={section.sectionId}><div className="file-page-title"><span>{section.label}</span><span className="panel-note">{section.blocks.length} 个阅读块</span></div>{section.blocks.map((block, index) => block.type === "text" ? <div className="file-content-text" key={`${block.assetId}-text-${index}`}><p>{block.text || "（空文本）"}</p>{block.truncated ? <><small>文本预览已截断</small><button type="button" className="text-button continuation-button" disabled={textLoadingKey === `${section.sectionId}:${index}`} onClick={() => onLoadMoreText(`${section.sectionId}:${index}`, block.assetId, block.nextTextOffset ?? block.text.length)}>{textLoadingKey === `${section.sectionId}:${index}` ? "正在加载" : "继续加载"}</button></> : null}</div> : <FileContentTableBlockV2 block={block} onOpenAsset={onOpenAsset} key={`${block.assetId}-table-${index}`} />)}</article>)}</div> : <EmptyState compact title="暂无可展示内容" body="本地处理尚未生成可阅读内容。" />}</> : <div className="source-empty-hint">已切换到原始来源。PDF 显示当前页，图片显示原图。</div>}</section><aside className="panel file-side-panel"><div className="panel-title"><h2>文件内容</h2><span>{textCount} 个文本 · {tables.length} 个表格</span></div><div className="asset-summary-grid"><span>文本<strong>{file.textPages || textCount}</strong></span><span>表格<strong>{file.tableAssets}</strong></span><span>质量<strong>{file.qualityIssueCount}</strong></span></div>{tables.length ? <div className="file-table-list">{tables.map((block) => <button className="file-table-item" key={block.assetId} onClick={() => onOpenAsset(block.assetId)}><strong>{block.displayName || "表格资产"}</strong><small>{block.candidate ? "候选表格" : "结构化表格"} · {block.sheetName || (block.pageNumber != null ? `第 ${block.pageNumber} 页` : "文件内容")}</small></button>)}</div> : null}<div className="file-insight-card"><div className="panel-title"><h2>AI 文件整理</h2><span>{fileInsightStatusLabel(noEvidence ? "no_evidence" : fileInsightTaskStatus(fileInsightTask, insight?.status))}</span></div>{noEvidence ? <small className="no-evidence-copy">本地提取未产生可用正文或可信表格；不会自动加入 AI 文件整理队列。</small> : insight?.insight ? <><p>{insight.insight.summary}</p><small>{insight.insight.important_topics.slice(0, 6).join(" · ")}</small><details><summary>来源依据</summary><pre>{compactValue(insight.insight.evidence_refs)}</pre></details></> : <small>本地内容仍可使用；FileInsight 在独立后台队列中运行。</small>}{!noEvidence && fileInsightTaskStatus(fileInsightTask, insight?.status) === "failed" ? <button className="text-button" onClick={() => void queueInsight()}>重新整理</button> : null}</div><div className="file-source-box"><h2>来源</h2><dl><dt>SHA-256</dt><dd>{file.sha256 || "—"}</dd><dt>file_id</dt><dd>{file.fileId}</dd></dl></div></aside></div></section>;
}

function LegacyFileDetailPageV2({ content, locator, fileSearch, onFileSearch, onBack, onOpenAsset, onNavigate, onLoadMoreText, textLoadingKey, viewerMode, onViewerMode, workspaceVersion, fileInsightTask, visionTask, onReprocess, reprocessBusy, onQueueInsight }: FileDetailPageV2Props) {
  const file = content.file;
  const format = file.format.toLowerCase();
  const navigation = content.navigation ?? { kind: "continuous", current: null, total: null };
  const mode = viewerMode;
  const { query: searchInput, results: searchResults, total: searchTotal, matchedPages: searchPages, matchedSections: searchSections, index: searchIndex, searching, setQuery: setSearchInput, search: runFileSearch, move: moveSearch, openResult: navigateToSearchResult } = useFileSearchController(file.fileId, fileSearch, onFileSearch, onNavigate);
  const setSearchIndex = (value: number) => onFileSearch((current) => ({ ...current, index: fileSearch.offset + value }));
  const [insight, setInsight] = useState<FileInsight | null>(null);
  const sourcePreview = content.sourcePreview;
  const currentPage = navigation.kind === "page" && typeof navigation.current === "number" ? navigation.current : null;
  const totalPages = navigation.kind === "page" ? Number(navigation.total ?? 0) : 0;
  const tables = content.sections.flatMap((section) => section.blocks).filter((block): block is Extract<FileContentBlock, { type: "table" }> => block.type === "table");
  const textCount = new Set(content.sections.flatMap((section) => section.blocks).filter((block) => block.type === "text").map((block) => block.assetId)).size;
  const noEvidence = file.processingStatus === "no_evidence" || file.evidenceStatus === "no_evidence";
  const visionStatus = visionStatusLabel(content, visionTask);

  useEffect(() => {
    let active = true;
    const load = () => void api.fileInsight(file.fileId).then((value) => {
      if (!active) return;
      setInsight(value);
    }).catch(() => {
      if (active) setInsight({ fileId: file.fileId, status: "not_started", insight: null, metadata: null });
    });
    load();
    return () => { active = false; };
  }, [file.fileId, workspaceVersion]);

  useEffect(() => {
    if (locator.textAssetId == null || locator.textOffset == null) return;
    const node = document.querySelector<HTMLElement>(
      `[data-text-asset-id="${CSS.escape(locator.textAssetId)}"]`,
    );
    node?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [content, locator.textAssetId, locator.textOffset]);

  const queueInsight = async () => {
    setInsight({ fileId: file.fileId, status: "queued", insight: null, metadata: null });
    try {
      await onQueueInsight(file.fileId);
    } catch {
      setInsight({ fileId: file.fileId, status: "failed", insight: null, metadata: null });
    }
  };

  const movePage = (delta: number) => {
    if (currentPage == null || !totalPages) return;
    onNavigate({ page: Math.max(1, Math.min(totalPages, currentPage + delta)) });
  };
  const jumpPage = (value: string) => {
    const parsed = Number(value);
    if (Number.isInteger(parsed) && parsed >= 1 && parsed <= totalPages) onNavigate({ page: parsed });
  };
    const nativeSource = mode === "source" && sourcePreview?.available && sourcePreview.sourceUrl ? `${sourcePreview.sourceUrl}${currentPage != null ? `#page=${currentPage}` : ""}` : null;
  const fallbackSource = !nativeSource && mode === "source" && sourcePreview?.available && sourcePreview.kind === "pdf_page" && currentPage != null
    ? `${sourcePreview.url}?page=${currentPage}`
    : !nativeSource && mode === "source" && sourcePreview?.available && sourcePreview.kind === "image" ? sourcePreview.url : null;

  return <section className="page-section file-detail-section"><button className="back-button" onClick={onBack}>← 数据目录</button><div className="detail-heading"><div className="asset-type-mark large">文</div><div><div className="eyebrow">文件详情</div><h1>{file.displayName}</h1><div className="detail-source">{file.relativePath} · {file.format.toUpperCase()} · {fileSize(file.sizeBytes)}</div></div><div className={`quality-pill ${file.qualityStatus as QualityStatus}`}>{qualityLabel(file.qualityStatus as QualityStatus)}</div></div>
    <div className="file-viewer-toolbar"><div className="viewer-navigation">{navigation.kind === "page" ? <><button className="secondary-button" disabled={currentPage === 1} onClick={() => movePage(-1)}>上一页</button><strong>第 {currentPage} / {totalPages} 页</strong><button className="secondary-button" disabled={currentPage === totalPages} onClick={() => movePage(1)}>下一页</button><input aria-label="跳转页码" type="number" min={1} max={totalPages} value={currentPage ?? 1} onChange={(event) => jumpPage(event.target.value)} /></> : navigation.kind === "sheet" ? <div className="sheet-tabs">{(navigation.items ?? []).map((name) => <button key={name} className={navigation.current === name ? "selected" : ""} onClick={() => onNavigate({ sheet: name })}>{name}</button>)}</div> : <strong>{navigation.kind === "document" ? "连续文档" : navigation.kind === "image" ? "原始图片" : "连续阅读"}</strong>}</div><form className="file-search-box" onSubmit={(event) => { event.preventDefault(); void runFileSearch(); }}><input aria-label="查找当前文件" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="查找当前文件" /><button type="submit" className="secondary-button">{searching ? "查找中…" : "查找"}</button>{searchTotal ? <><span>找到 {number(searchTotal)} 处 · 当前 {searchIndex + 1} / {number(searchTotal)}{searchPages.length ? ` · 第 ${searchPages.join("、")} 页` : searchSections.length ? ` · ${searchSections.join("、")}` : ""}</span><button type="button" className="text-button" onClick={() => moveSearch(-1)}>上一个</button><button type="button" className="text-button" onClick={() => moveSearch(1)}>下一个</button></> : null}</form><div className="viewer-mode-switch"><button className={mode === "source" ? "selected" : ""} onClick={() => onViewerMode("source")}>原始来源</button><button className={mode === "reading" ? "selected" : ""} onClick={() => onViewerMode("reading")}>整理阅读</button></div></div>
    <div className="file-detail-grid"><section className="panel file-pages-panel">{nativeSource && format === "pdf" ? <div className="native-pdf-viewer"><div className="source-preview-label">原始 PDF</div><iframe title={`${file.displayName} 原始 PDF`} src={nativeSource} /></div> : null}{nativeSource && ["png", "jpg", "jpeg"].includes(format) ? <div className="source-image-preview"><div className="source-preview-label">原始图片</div><img src={nativeSource} alt={`${file.displayName} 原始图片`} loading="lazy" /></div> : null}{fallbackSource ? <div className="source-image-preview"><div className="source-preview-label">结构化预览回退</div><img src={fallbackSource} alt={`${file.displayName} source preview`} loading="lazy" /></div> : null}{searchTotal ? <div className="file-occurrence-results" aria-live="polite"><div className="file-occurrence-summary">找到 {number(searchTotal)} 处{searchIndex >= 0 ? ` · 当前 ${searchIndex + 1} / ${number(searchTotal)}` : ""}{searchPages.length ? ` · ${searchPages.map((page) => `第 ${page} 页`).join("、")}` : searchSections.length ? ` · ${searchSections.join("、")}` : ""}</div><details open><summary>结果列表</summary><div className="file-occurrence-list">{searchResults.map((result, index) => <button type="button" className={`file-occurrence-item ${index === searchIndex ? "selected" : ""}`} key={result.occurrenceId} onClick={() => { setSearchIndex(index); navigateToSearchResult(result); }}><strong>{result.page != null ? `第 ${result.page} 页` : result.sheet || result.section || "文件"}</strong><HighlightedSnippet text={result.snippet} offsets={result.matchOffsets ?? []} /></button>)}</div></details></div> : searchInput.trim() && !searching ? <div className="file-occurrence-summary">未找到匹配内容。</div> : null}{mode === "reading" ? content.sections.length ? <div className="file-pages">{content.sections.map((section) => <article className="file-page" key={section.sectionId}><div className="file-page-title"><span>{section.label}</span><span className="panel-note">{section.blocks.length} 个阅读块</span></div>{section.blocks.map((block, index) => block.type === "text" ? <div className="file-content-text" data-text-asset-id={block.assetId} key={`${block.assetId}-text-${index}`}><p><HighlightedText text={block.text || ""} query={searchInput} start={block.textOffset ?? 0} matchStart={locator.matchStart} matchEnd={locator.matchEnd} /></p>{block.truncated ? <><small>文本预览已截断</small><button type="button" className="text-button continuation-button" disabled={textLoadingKey === `${section.sectionId}:${index}`} onClick={() => onLoadMoreText(`${section.sectionId}:${index}`, block.assetId, block.nextTextOffset ?? block.text.length)}>{textLoadingKey === `${section.sectionId}:${index}` ? "正在加载…" : "继续加载"}</button></> : null}</div> : <FileContentTableBlockV2 block={block} searchQuery={searchInput} onOpenAsset={onOpenAsset} key={`${block.assetId}-table-${index}`} />)}</article>)}</div> : <EmptyState compact title="暂无可展示内容" body="当前文件没有可用于主视图的正文或表格预览。" /> : <div className="source-empty-hint">已切换到原始来源；PDF 使用浏览器原生查看器，图片显示原图，其他格式保留连续源样式。</div>}</section><aside className="panel file-side-panel"><div className="panel-title"><h2>文件内容</h2><span>{textCount} 个文本 · {tables.length} 个表格</span></div><div className="asset-summary-grid"><span>文本<strong>{file.textPages || textCount}</strong></span><span>表格<strong>{file.tableAssets}</strong></span><span>质量<strong>{file.qualityIssueCount}</strong></span></div>{tables.length ? <div className="file-table-list">{tables.map((block) => <button className="file-table-item" key={block.assetId} onClick={() => onOpenAsset(block.assetId)}><strong>{block.displayName || "表格资产"}</strong><small>{block.candidate ? "候选表格 · 需核验" : "表格"} · {block.sheetName || (block.pageNumber != null ? `第 ${block.pageNumber} 页` : "文件内容")}</small></button>)}</div> : null}<div className="file-insight-card"><div className="panel-title"><h2>AI 文件整理</h2><span>{fileInsightStatusLabel(noEvidence ? "no_evidence" : fileInsightTaskStatus(fileInsightTask, insight?.status))}</span></div>{visionStatus ? <small className="vision-status-copy">视觉路径：{visionStatus}</small> : null}{noEvidence ? <small className="no-evidence-copy">本地提取未产生可用正文或可信表格；不会自动加入 AI 文件整理队列。</small> : insight?.insight ? <><p>{insight.insight.summary}</p><small>{insight.insight.important_topics.slice(0, 6).join(" · ")}</small><details><summary>来源依据</summary><pre>{compactValue(insight.insight.evidence_refs)}</pre></details></> : <small>本地内容可先查看；AI 文件整理在独立后台队列中运行。</small>}{!noEvidence && ["not_started", "disabled"].includes(fileInsightTaskStatus(fileInsightTask, insight?.status)) ? <button className="text-button" onClick={() => void queueInsight()}>整理此文件</button> : !noEvidence && fileInsightTaskStatus(fileInsightTask, insight?.status) === "failed" ? <button className="text-button" onClick={() => void queueInsight()}>重新整理</button> : null}</div><div className="file-source-box"><h2>来源</h2><dl><dt>SHA-256</dt><dd>{file.sha256 || "—"}</dd><dt>file_id</dt><dd>{file.fileId}</dd></dl></div></aside></div></section>;
}

type FileDetailPageV2Props = {
  content: FileContentResponse;
  locator: FileLocator;
  fileSearch: FileSearchState;
  onFileSearch: React.Dispatch<React.SetStateAction<FileSearchState>>;
  onBack: () => void;
  onOpenAsset: (id: string) => void;
  onNavigate: (locator: FileLocator) => void;
  onLoadMoreText: (blockKey: string, assetId: string, offset: number) => void;
  textLoadingKey: string | null;
  viewerMode: "reading" | "source";
  onViewerMode: (mode: "reading" | "source") => void;
  workspaceVersion: number;
  fileInsightTask: Task | null;
  visionTask: Task | null;
  reprocessTask: Task | null;
  onReprocess: () => void;
  reprocessBusy: boolean;
  onQueueInsight: (fileId: string) => Promise<{ fileId: string; taskId?: string; task?: Task; status?: string }>;
};

function FileDetailPageV2(props: FileDetailPageV2Props) {
  const format = props.content.file.format.toLowerCase();
  const reprocessing = props.reprocessBusy || props.reprocessTask != null;
  const view = ["csv", "tsv", "xls", "xlsx", "docx", "txt"].includes(format)
    ? <SourceDocumentView {...props} />
    : <LegacyFileDetailPageV2 {...props} />;
  return <><div className="file-reprocess-bar"><button type="button" className="secondary-button" onClick={props.onReprocess} disabled={reprocessing}>{reprocessing ? "重新处理中…" : "重新处理此文件"}</button></div>{view}</>;
}

function useFileSearchController(
  fileId: string,
  fileSearch: FileSearchState,
  onFileSearch: React.Dispatch<React.SetStateAction<FileSearchState>>,
  onNavigate: (locator: FileLocator) => void,
) {
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const update = useCallback((patch: Partial<FileSearchState>) => {
    onFileSearch((current) => ({ ...current, ...patch }));
  }, [onFileSearch]);

  useEffect(() => () => {
    generation.current += 1;
    controller.current?.abort();
  }, [fileId]);

  const openResult = useCallback((result: SearchOccurrence) => {
    const tableOccurrence = result.locator?.kind === "table_cell"
      || result.row != null
      || result.column != null
      || result.cellValue != null;
    if (tableOccurrence) {
      onNavigate({
        page: result.page ?? undefined,
        sheet: result.sheet ?? undefined,
        tableAssetId: result.assetId,
        tableRow: result.row ?? result.locator?.row ?? undefined,
        tableColumn: result.column ?? result.locator?.column ?? undefined,
        tableMatchStart: result.locator?.matchStart ?? result.startOffset,
        tableMatchEnd: result.locator?.matchEnd ?? result.endOffset,
      });
      return;
    }
    onNavigate({
      page: result.page ?? undefined,
      sheet: result.sheet ?? undefined,
      textAssetId: result.assetId,
      textOffset: result.startOffset,
      matchStart: result.startOffset,
      matchEnd: result.endOffset,
    });
  }, [onNavigate]);

  const loadPage = useCallback(async (value: string, pageOffset: number, targetIndex: number) => {
    const requestValue = value.trim();
    if (!requestValue) return;
    controller.current?.abort();
    const requestController = new AbortController();
    controller.current = requestController;
    const requestGeneration = generation.current + 1;
    generation.current = requestGeneration;
    update({ query: requestValue, searching: true });
    try {
      const response = await api.fileSearch(fileId, requestValue, requestController.signal, pageOffset);
      if (requestController.signal.aborted || requestGeneration !== generation.current) return;
      const localIndex = response.results.length
        ? Math.max(0, Math.min(targetIndex - response.offset, response.results.length - 1))
        : -1;
      update({
        query: response.query,
        results: response.results,
        offset: response.offset,
        total: response.totalOccurrences,
        index: localIndex >= 0 ? localIndex + response.offset : -1,
        matchedPages: response.matchedPages,
        matchedSections: response.matchedSections,
        searching: false,
      });
      if (localIndex >= 0) openResult(response.results[localIndex]);
    } catch {
      if (requestController.signal.aborted || requestGeneration !== generation.current) return;
      update({ results: [], offset: 0, total: 0, index: -1, matchedPages: [], matchedSections: [], searching: false });
    }
  }, [fileId, openResult, update]);

  const search = useCallback(() => {
    void loadPage(fileSearch.query, 0, 0);
  }, [fileSearch.query, loadPage]);

  const move = useCallback((delta: number) => {
    if (!fileSearch.total) return;
    const currentIndex = Math.max(0, fileSearch.index);
    const targetIndex = currentIndex + delta < 0
      ? fileSearch.total - 1
      : currentIndex + delta >= fileSearch.total ? 0 : currentIndex + delta;
    const targetOffset = Math.floor(targetIndex / 100) * 100;
    if (targetOffset === fileSearch.offset && targetIndex - targetOffset < fileSearch.results.length) {
      const localIndex = targetIndex - targetOffset;
      update({ index: targetIndex });
      openResult(fileSearch.results[localIndex]);
      return;
    }
    void loadPage(fileSearch.query, targetOffset, targetIndex);
  }, [fileSearch, loadPage, openResult, update]);

  const select = useCallback((result: SearchOccurrence, index: number) => {
    update({ index: fileSearch.offset + index });
    openResult(result);
  }, [openResult, update]);

  return {
    ...fileSearch,
    setQuery: (query: string) => update({ query }),
    search,
    move,
    openResult,
    select,
  };
}

function SourceDocumentView({ content, locator, fileSearch, onFileSearch, onBack, onOpenAsset, onNavigate, onLoadMoreText, textLoadingKey, workspaceVersion, fileInsightTask, onQueueInsight }: FileDetailPageV2Props) {
  const file = content.file;
  const { query, results, offset, total, index, matchedPages, matchedSections, searching, setQuery, search, move } = useFileSearchController(file.fileId, fileSearch, onFileSearch, onNavigate);
  const [insight, setInsight] = useState<FileInsight | null>(null);
  const format = file.format.toLowerCase();
  const navigation = content.navigation ?? { kind: "continuous", current: null, total: null };
  const noEvidence = file.processingStatus === "no_evidence" || file.evidenceStatus === "no_evidence";

  useEffect(() => {
    let active = true;
    void api.fileInsight(file.fileId).then((value) => {
      if (active) setInsight(value);
    }).catch(() => {
      if (active) setInsight({ fileId: file.fileId, status: "not_started", insight: null, metadata: null });
    });
    return () => { active = false; };
  }, [file.fileId, workspaceVersion]);

  const status = noEvidence ? "no_evidence" : fileInsightTaskStatus(fileInsightTask, insight?.status ?? file.fileInsightStatus);
  const sourceContent = <SourceLikeContent content={content} locator={locator} searchQuery={query} onOpenAsset={onOpenAsset} onLoadMoreText={onLoadMoreText} textLoadingKey={textLoadingKey} />;
  const tables = content.sections.flatMap((section) => section.blocks).filter((block): block is Extract<FileContentBlock, { type: "table" }> => block.type === "table");
  const textCount = new Set(content.sections.flatMap((section) => section.blocks).filter((block) => block.type === "text").map((block) => block.assetId)).size;
  const insightLabel = fileInsightStatusLabel(status);
  const queueInsight = async () => {
    setInsight({ fileId: file.fileId, status: "queued", insight: null, metadata: null });
    try {
      await onQueueInsight(file.fileId);
    } catch {
      setInsight({ fileId: file.fileId, status: "failed", insight: null, metadata: null });
    }
  };
  const currentResult = index >= 0 ? results[index - offset] ?? null : null;

  return <section className="page-section file-detail-section">
    <button className="back-button" onClick={onBack}>← 数据目录</button>
    <div className="detail-heading">
      <div className="asset-type-mark large">文</div>
      <div><div className="eyebrow">文件详情</div><h1>{file.displayName}</h1><div className="detail-source">{file.relativePath} · {file.format.toUpperCase()} · {fileSize(file.sizeBytes)}</div></div>
      <div className={`quality-pill ${file.qualityStatus as QualityStatus}`}>{qualityLabel(file.qualityStatus as QualityStatus)}</div>
    </div>
    <div className="file-viewer-toolbar">
      <div className="viewer-navigation"><strong>{navigation.kind === "sheet" ? `工作表：${navigation.current ?? ""}` : navigation.kind === "document" ? "连续文档" : "连续阅读"}</strong></div>
      <form className="file-search-box" onSubmit={(event) => { event.preventDefault(); search(); }}><input aria-label="查找当前文件" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="查找当前文件" /><button type="submit" className="secondary-button">{searching ? "查找中…" : "查找"}</button>{total ? <><span>找到 {number(total)} 处 · 当前 {index + 1} / {number(total)}{matchedPages.length ? ` · 第 ${matchedPages.join("、")} 页` : matchedSections.length ? ` · ${matchedSections.join("、")}` : ""}</span>{currentResult ? <small className="file-search-snippet">{currentResult.snippet}</small> : null}<button type="button" className="text-button" onClick={() => move(-1)}>上一个</button><button type="button" className="text-button" onClick={() => move(1)}>下一个</button></> : null}</form>
    </div>
    <div className="file-detail-grid">
      <section className="panel file-pages-panel">{sourceContent}</section>
      <aside className="panel file-side-panel">
        <div className="panel-title"><h2>文件内容</h2><span>{textCount} 个文本 · {tables.length} 个表格</span></div>
        <div className="asset-summary-grid"><span>文本<strong>{file.textPages || textCount}</strong></span><span>表格<strong>{file.tableAssets}</strong></span><span>质量<strong>{file.qualityIssueCount}</strong></span></div>
        {tables.length ? <div className="file-table-list">{tables.map((block) => <button className="file-table-item" key={block.assetId} onClick={() => onOpenAsset(block.assetId)}><strong>{block.displayName || "表格资产"}</strong><small>{block.candidate ? "候选表格 · 需核验" : "结构化表格"} · {block.sheetName || "文件内容"}</small></button>)}</div> : null}
        <div className="file-insight-card"><div className="panel-title"><h2>AI 文件整理</h2><span>{insightLabel}</span></div>{noEvidence ? <small className="no-evidence-copy">本地提取未产生可用正文或可信表格；不会自动加入 AI 文件整理队列。</small> : insight?.insight ? <><p>{insight.insight.summary}</p><small>{(insight.insight.important_topics ?? []).slice(0, 6).join(" · ")}</small></> : <small>本地内容可先查看；AI 文件整理在独立后台队列中运行。</small>}{!noEvidence && (status === "not_started" || status === "disabled") ? <button className="text-button" onClick={() => void queueInsight()}>整理此文件</button> : !noEvidence && status === "failed" ? <button className="text-button" onClick={() => void queueInsight()}>重新整理</button> : null}</div>
        <div className="file-source-box"><h2>来源</h2><dl><dt>SHA-256</dt><dd>{file.sha256 || "—"}</dd><dt>file_id</dt><dd>{file.fileId}</dd></dl></div>
      </aside>
    </div>
  </section>;
}

function SourceLikeContent({ content, locator, searchQuery, onOpenAsset, onLoadMoreText, textLoadingKey }: { content: FileContentResponse; locator: FileLocator; searchQuery: string; onOpenAsset: (id: string) => void; onLoadMoreText: (blockKey: string, assetId: string, offset: number) => void; textLoadingKey: string | null }) {
  const noEvidence = content.file.processingStatus === "no_evidence" || content.file.evidenceStatus === "no_evidence";
  if (!content.sections.length) return <div className="empty-state compact"><div className="empty-mark">◇</div><strong>{noEvidence ? "暂无本地证据" : content.file.format === "docx" ? "当前文档包含暂未完整支持的版式内容" : "暂无可展示内容"}</strong><span>{noEvidence ? "本地提取未产生可用正文或可信表格；不会自动加入 AI 文件整理队列。" : content.file.format === "docx" ? "请查看原文件，已提取的内容会在支持范围内显示。" : "当前文件没有可用于源视图的正文或表格预览。"}</span>{content.sourcePreview?.sourceUrl ? <a className="text-button" href={content.sourcePreview.sourceUrl} target="_blank" rel="noreferrer">查看/打开原文件</a> : null}</div>;
  return <div className="file-pages source-like-content">{content.sections.map((section) => <article className="file-page" key={section.sectionId}><div className="file-page-title"><span>{section.label}</span><span className="panel-note">源文件顺序</span></div>{section.blocks.map((block, index) => block.type === "text" ? <div className="file-content-text" data-text-asset-id={block.assetId} key={`${block.assetId}-text-${index}`}><p><HighlightedText text={block.text || ""} query={locator.textAssetId === block.assetId ? searchQuery : ""} start={block.textOffset ?? 0} matchStart={locator.matchStart} matchEnd={locator.matchEnd} /></p>{block.truncated ? <><small>文本预览已截断</small><button type="button" className="text-button continuation-button" disabled={textLoadingKey === `${section.sectionId}:${index}`} onClick={() => onLoadMoreText(`${section.sectionId}:${index}`, block.assetId, block.nextTextOffset ?? block.text.length)}>{textLoadingKey === `${section.sectionId}:${index}` ? "正在加载…" : "继续加载"}</button></> : null}</div> : <FileContentTableBlockV2 block={block} locator={locator} searchQuery={searchQuery} onOpenAsset={onOpenAsset} key={`${block.assetId}-table-${index}`} />)}</article>)}</div>;
}

function HighlightedSnippet({ text, offsets, result }: { text?: string; offsets?: number[][]; result?: SearchResult }) {
  const snippet = result?.snippet ?? text ?? "";
  const ranges = (result?.matchOffsets ?? offsets ?? []).filter((range) => range.length === 2).sort((left, right) => left[0] - right[0]);
  if (!ranges.length) return <span className="search-snippet">{snippet}</span>;
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  ranges.forEach(([start, end], index) => {
    const boundedStart = Math.max(cursor, Math.min(snippet.length, start));
    const boundedEnd = Math.max(boundedStart, Math.min(snippet.length, end));
    if (boundedStart > cursor) parts.push(<span key={`text-${index}`}>{snippet.slice(cursor, boundedStart)}</span>);
    if (boundedEnd > boundedStart) parts.push(<mark key={`match-${index}`}>{snippet.slice(boundedStart, boundedEnd)}</mark>);
    cursor = boundedEnd;
  });
  if (cursor < snippet.length) parts.push(<span key="tail">{snippet.slice(cursor)}</span>);
  return <span className="search-snippet">{parts}</span>;
}

function HighlightedText({ text, query, start, matchStart, matchEnd }: { text: string; query?: string; start: number; matchStart?: number; matchEnd?: number }) {
  if (query?.trim()) {
    const needle = query.trim();
    const parts: React.ReactNode[] = [];
    let cursor = 0;
    let match = text.indexOf(needle, cursor);
    let index = 0;
    while (match >= 0) {
      if (match > cursor) parts.push(<span key={`text-${index}`}>{text.slice(cursor, match)}</span>);
      parts.push(<mark key={`match-${index}`}>{text.slice(match, match + needle.length)}</mark>);
      cursor = match + needle.length;
      match = text.indexOf(needle, cursor);
      index += 1;
    }
    if (index) {
      if (cursor < text.length) parts.push(<span key="tail">{text.slice(cursor)}</span>);
      return <>{parts}</>;
    }
    return <>{text}</>;
  }
  if (matchStart == null || matchEnd == null || matchEnd <= start || matchStart >= start + text.length) return <>{text}</>;
  const localStart = Math.max(0, matchStart - start);
  const localEnd = Math.min(text.length, matchEnd - start);
  return <>{text.slice(0, localStart)}<mark>{text.slice(localStart, localEnd)}</mark>{text.slice(localEnd)}</>;
}

function NormalizedTableRenderer({ preview, searchQuery = "", locator, rowOffset = 0, paginate = false, onOffset, total }: { preview: TablePreview; searchQuery?: string; locator?: FileLocator; rowOffset?: number; paginate?: boolean; onOffset?: (value: number) => void; total?: number }) {
  const displayColumns = preview.presentationColumns?.length === preview.columns.length ? preview.presentationColumns : preview.columns;
  const displayRows = preview.layer === "raw" && preview.headerDetected ? preview.rows.slice(1) : preview.rows;
  return <><div className="table-wrap unified-table-renderer"><table><thead><tr><th className="row-number">#</th>{displayColumns.map((column, index) => <th key={`${column}-${index}`}>{column}</th>)}</tr></thead><tbody>{displayRows.map((row, index) => <tr key={`${rowOffset}-${index}`}><td className="row-number">{rowOffset + index + 1}</td>{preview.columns.map((column, columnIndex) => <td data-table-asset-id={preview.assetId} data-table-row={rowOffset + index} data-table-column={columnIndex} key={`${rowOffset}-${index}-${column}`} title={displayValue(row[column])}><HighlightedText text={displayValue(row[column])} query={searchQuery} start={0} /></td>)}</tr>)}</tbody></table></div>{paginate && onOffset ? <Pagination offset={rowOffset} limit={preview.pagination.limit || 20} total={total ?? preview.pagination.total ?? 0} onOffset={onOffset} /> : null}</>;
}

function FileContentTableBlockV2({ block, searchQuery = "", locator, onOpenAsset, sourcePreviewUrl: providedPreviewUrl, pageNumber: providedPageNumber }: { block: Extract<FileContentBlock, { type: "table" }>; searchQuery?: string; locator?: FileLocator; onOpenAsset: (id: string) => void; sourcePreviewUrl?: string | null; pageNumber?: number | null }) {
  const sourcePreviewUrl = providedPreviewUrl ?? (block.provenance?.fileId ? `/api/v1/files/${encodeURIComponent(String(block.provenance.fileId))}/preview` : null);
  const pageNumber = providedPageNumber ?? block.pageNumber;
  const rawPreview = block.rawPreview ?? (block.preview?.layer === "raw" ? block.preview : null);
  const normalizedPreview = block.normalizedPreview ?? (block.preview?.layer === "normalized" ? block.preview : null);
  const candidateOnly = block.candidate || block.parserValidity === "invalid";
  const [layer, setLayer] = useState<"source" | "raw" | "normalized">(candidateOnly ? "source" : "normalized");
  const selectedPreview = layer === "raw" ? rawPreview ?? normalizedPreview : normalizedPreview ?? rawPreview;
  const preview = selectedPreview?.layer === "raw" && selectedPreview.headerDetected ? { ...selectedPreview, rows: selectedPreview.rows.slice(1) } : selectedPreview;
  const presentation = block.presentation as { merged_ranges?: string[]; cells?: Array<{ row?: number; column?: number; row_span?: number; column_span?: number; text?: string; value?: unknown }> } | null | undefined;
  const sourceMode = layer === "source" && Boolean(presentation);
  const columnCount = Math.max(1, Number((presentation as { column_count?: number } | null)?.column_count ?? 8));
  const widths = ((presentation as { columns?: Array<{ width?: number }> } | null)?.columns ?? []).map((item) => Number(item.width ?? 0)).filter((value) => value > 0);
  const averageWidth = widths.length ? widths.reduce((sum, value) => sum + value, 0) / widths.length : 1;
  const presentationColumns = Array.from({ length: columnCount }, (_, index) => {
    const width = widths[index] ?? averageWidth;
    return `minmax(70px, ${Math.max(0.7, width / Math.max(1, averageWidth)).toFixed(2)}fr)`;
  }).join(" ");
  const rawBbox = block.provenance?.bbox as number[] | { x0?: unknown; y0?: unknown; x1?: unknown; y1?: unknown } | undefined;
  const sourceBbox = Array.isArray(rawBbox) && rawBbox.length === 4 && rawBbox.every((value) => Number.isFinite(Number(value)))
    ? rawBbox.map((value) => Number(value))
    : rawBbox && !Array.isArray(rawBbox) && [rawBbox.x0, rawBbox.y0, rawBbox.x1, rawBbox.y1].every((value) => Number.isFinite(Number(value)))
      ? [rawBbox.x0, rawBbox.y0, rawBbox.x1, rawBbox.y1].map((value) => Number(value))
      : null;
  const sourceCropUrl = sourcePreviewUrl && sourceBbox
    ? `${sourcePreviewUrl}?${[pageNumber != null ? `page=${pageNumber}` : "", `bbox=${sourceBbox.join(",")}`].filter(Boolean).join("&")}`
    : null;
  useEffect(() => {
    if (locator?.tableAssetId !== block.assetId || locator.tableRow == null || locator.tableColumn == null) return;
    const node = Array.from(document.querySelectorAll<HTMLElement>("[data-table-asset-id]"))
      .find((item) => item.dataset.tableAssetId === block.assetId
        && Number(item.dataset.tableRow) === locator.tableRow
        && Number(item.dataset.tableColumn) === locator.tableColumn);
    if (!node) return;
    node.classList.add("locator-highlight");
    node.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
    const timer = window.setTimeout(() => node.classList.remove("locator-highlight"), 2_200);
    return () => window.clearTimeout(timer);
  }, [block.assetId, locator?.tableAssetId, locator?.tableRow, locator?.tableColumn, preview?.assetId]);
  if (!candidateOnly && layer === "normalized" && preview) {
    return <div className="file-content-table"><div className="file-content-table-heading"><div><strong>{block.displayName || "表格"}</strong><span className="table-label">结构化表格</span></div><button className="text-button" onClick={() => onOpenAsset(block.assetId)}>来源与技术信息</button></div><NormalizedTableRenderer preview={preview} searchQuery={searchQuery} locator={locator} /></div>;
  }
  return <div className={`file-content-table ${candidateOnly ? "candidate-table" : ""}`}><div className="file-content-table-heading"><div><strong>{block.displayName || "表格"}</strong><span className={block.candidate ? "candidate-label" : "table-label"}>{block.candidate ? "候选表格" : block.parserValidity === "invalid" ? "旧版 XLS 受限" : "表格"}</span></div><div className="file-content-table-actions">{sourceCropUrl ? <details className="source-table-crop" open={false}><summary>查看原表区域</summary><img src={sourceCropUrl} alt={`${block.displayName || "表格"} source crop`} loading="lazy" /></details> : null}<button className="text-button" onClick={() => onOpenAsset(block.assetId)}>查看技术详情</button></div></div>{candidateOnly ? <p className="candidate-help">{block.parserValidity === "invalid" ? "旧版 XLS 解析受限，请核对原文件。" : "候选表格 · 需核验；不会作为可靠结构化数据用于 SQL、AI 或报告统计。"}</p> : null}<div className="file-table-layer-switch" role="group" aria-label="表格显示模式">{presentation ? <button className={sourceMode ? "selected" : ""} onClick={() => setLayer("source")}>原始布局</button> : null}{rawPreview ? <button className={layer === "raw" ? "selected" : ""} onClick={() => setLayer("raw")}>原始矩阵</button> : null}<button className={layer === "normalized" ? "selected" : ""} onClick={() => setLayer("normalized")}>规范化数据</button></div>{candidateOnly && !presentation ? <details className="candidate-source-only"><summary>查看候选原表区域</summary>{sourceCropUrl ? <img src={sourceCropUrl} alt={`${block.displayName || "表格"} source crop`} loading="lazy" /> : <p className="muted">请打开原文件核对内容。</p>}</details> : sourceMode ? <div className="table-presentation-grid" style={{ gridTemplateColumns: presentationColumns }}>{(presentation?.cells ?? []).map((cell, index) => <div className="table-presentation-cell" key={index} style={{ gridRow: `${Number(cell.row ?? 0) + 1} / span ${Number(cell.row_span ?? 1)}`, gridColumn: `${Number(cell.column ?? 0) + 1} / span ${Number(cell.column_span ?? 1)}` }}>{displayValue(cell.value ?? cell.text)}</div>)}</div> : preview ? <NormalizedTableRenderer preview={preview} searchQuery={searchQuery} locator={locator} /> : <p className="muted">暂无可读表格预览。</p>}</div>;
}

function FileContentTableBlock({ block, onOpenAsset }: { block: Extract<FileContentBlock, { type: "table" }>; onOpenAsset: (id: string) => void }) {
  const rawPreview = block.rawPreview ?? (block.preview?.layer === "raw" ? block.preview : null);
  const normalizedPreview = block.normalizedPreview ?? (block.preview?.layer === "normalized" ? block.preview : null);
  const [layer, setLayer] = useState<"raw" | "normalized">(rawPreview ? "raw" : "normalized");
  const preview = layer === "raw" ? rawPreview ?? normalizedPreview : normalizedPreview ?? rawPreview;
  const showingRaw = preview?.layer === "raw";
  const displayColumns = showingRaw && preview?.presentationColumns?.length === preview.columns.length ? preview.presentationColumns : preview?.columns ?? [];
  const displayRows = showingRaw && preview?.headerDetected ? preview.rows.slice(1) : preview?.rows ?? [];
  return <div className="file-content-table"><div className="file-content-table-heading"><div><strong>{block.displayName || "表格"}</strong><span className={block.candidate ? "candidate-label" : "table-label"}>{block.candidate ? "候选表格" : "表格"}</span></div><button className="text-button" onClick={() => onOpenAsset(block.assetId)}>查看技术详情</button></div>{rawPreview && normalizedPreview ? <div className="file-table-layer-switch" role="group" aria-label="表格视图"><button type="button" className={showingRaw ? "selected" : ""} aria-pressed={showingRaw} onClick={() => setLayer("raw")}>原始提取视图</button><button type="button" className={!showingRaw ? "selected" : ""} aria-pressed={!showingRaw} onClick={() => setLayer("normalized")}>规范化数据视图</button></div> : null}{preview ? <><div className="table-preview-caption">当前为{showingRaw ? "原始提取视图" : "规范化数据视图"}{showingRaw && preview.headerDetected ? "（首行作为表头）" : ""}</div><div className="table-preview-scroll"><table className="data-table"><thead><tr>{displayColumns.map((column, columnIndex) => <th key={`${column}-${columnIndex}`}>{column}</th>)}</tr></thead><tbody>{displayRows.map((row, rowIndex) => <tr key={rowIndex}>{preview.columns.map((column, columnIndex) => <td key={`${rowIndex}-${column}-${columnIndex}`}>{displayValue(row[column])}</td>)}</tr>)}</tbody></table></div></> : <p className="muted">当前没有可读取的表格预览，请打开技术详情查看技术数据。</p>}{preview?.columnsTruncated ? <small className="candidate-help">列数较多，当前只展示前 32 列；完整表格资产仍可打开查看。</small> : null}{block.candidate ? <small className="candidate-help">这是提取器检测到的候选结构，不代表已确认的正确表格。</small> : null}</div>;
}

function QualityPage({ issues, onUpdate, onOpen, onOpenFile }: { issues: QualityIssue[]; onUpdate: (issue: QualityIssue, status: QualityIssue["status"]) => void; onOpen: (id: string) => void; onOpenFile: (id: string) => void }) {
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">QUALITY REVIEW</div><h1>质量检查</h1><p className="heading-note">这里只改变审核状态，不会修改原始或规范化数据</p><p className="quality-status-help">确认问题：认可系统判断；标记已处理：表示已在外部完成处理；暂时忽略：当前先不处理。三种操作都不会修改原始或规范化数据。</p></div><div className="open-count">{number(issues.length)} 个待审核</div></div>{issues.length ? <div className="quality-list">{issues.map((issue) => <IssueCard key={issue.issue_id} issue={issue} onUpdate={onUpdate} onOpen={onOpen} onOpenFile={onOpenFile} />)}</div> : <EmptyState title="暂无待处理问题" body="当前没有待审核的质量问题。" />}</section>;
}

function IssueCard({ issue, onUpdate, onOpen, onOpenFile }: { issue: QualityIssue; onUpdate: (issue: QualityIssue, status: QualityIssue["status"]) => void; onOpen: (id: string) => void; onOpenFile: (id: string) => void }) {
  const source = issue.source_file || issue.effective_display_name || issue.fallback_display_name || issue.asset_id;
  const location = issue.page_number ? ` · 第 ${issue.page_number} 页` : issue.sheet_name ? ` · ${issue.sheet_name}` : "";
  return <article className="issue-card"><div className={`severity-mark ${issue.severity}`} /> <div className="issue-content"><div className="issue-top"><span className={`severity-label ${issue.severity}`}>{severityLabel(issue.severity)}</span><button className="issue-asset" onClick={() => onOpenFile(issue.file_id || issue.asset_id)}>{issue.effective_display_name || issue.fallback_display_name || issue.asset_id}</button><span className="issue-time">{formatDate(issue.created_at)}</span></div><h3>{issueTypeLabel(issue.issue_type)}</h3><p>{issueDescription(issue)}</p><div className="issue-source">来源：{source}{issue.source_format ? ` · ${issue.source_format.toUpperCase()}` : ""}{location}</div><details className="evidence"><summary>查看系统证据</summary><pre>{compactValue(issue.evidence)}</pre></details><div className="suggestion">处理建议：{suggestedActionLabel(issue.suggested_action)}</div><p className="issue-help">这些操作只改变审核状态，不会修改原始或规范化数据。</p><div className="issue-actions"><button onClick={() => onUpdate(issue, "accepted")}>确认问题</button><button onClick={() => onUpdate(issue, "resolved")}>标记已处理</button><button className="quiet" onClick={() => onUpdate(issue, "ignored")}>暂时忽略</button><button className="text-button" onClick={() => onOpen(issue.asset_id)}>查看资产</button></div></div></article>;
}

function TasksPage({ tasks, onProcess, onOpenCatalog, onCancel }: { tasks: Task[]; onProcess: () => void; onOpenCatalog: () => void; onCancel: (taskId: string) => void }) {
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">PROCESSING TASKS</div><h1>处理任务</h1><p className="heading-note">后台任务每秒更新一次；核心流水线继续保持逐文件隔离</p></div><button className="primary-button" onClick={onProcess}>处理新目录 <span>＋</span></button></div>{tasks.length ? <div className="task-list">{tasks.map((task) => <TaskCardV2 key={task.taskId} task={task} onOpenCatalog={onOpenCatalog} onCancel={onCancel} />)}</div> : <EmptyState title="尚无处理任务" body="从一个本地目录开始，建立第一个数据目录。" />}</section>;
}

function TaskRow({ task }: { task: Task }) { return <div className="task-row"><span className={`task-status-dot ${task.status}`} /><div className="task-row-source">{task.source}</div><div className="task-row-stage">{taskStageLabel(task.currentStage, task.currentSubstage)}</div><div className="task-row-progress"><span style={{ width: `${task.progress * 100}%` }} /></div><div className="task-row-status">{taskStatusLabel(task.status)}</div></div>; }

function TaskCardV2({ task, onOpenCatalog, onCancel }: { task: Task; onOpenCatalog: () => void; onCancel?: (taskId: string) => void }) {
  const isFileTask = task.taskType === "process";
  const isFileInsightBatch = task.taskType === "file_insight_batch";
  const discovered = Number(task.discoveredCount ?? task.counts.discoveredCount ?? 0);
  const registered = Number(task.registeredCount ?? task.counts.registeredCount ?? 0);
  const readyLocal = Number(task.readyLocalCount ?? task.counts.readyLocalCount ?? 0);
  const failedLocal = Number(task.failedCount ?? task.counts.failedCount ?? task.counts.filesFailed ?? 0);
  const skippedLocal = Number(task.skippedCount ?? task.counts.skippedCount ?? task.counts.filesSkipped ?? 0);
  const totalKnown = task.scanComplete === true && task.total != null;
  const remaining = totalKnown ? Math.max(0, task.total! - task.completed) : null;
  const stage = task.currentStage === "local_ready" ? "可查看" : taskStageLabel(task.currentStage, task.currentSubstage);
  if (task.taskType === "file_insight") {
    return <article className="task-card task-card-readable file-insight-task-card"><div className="task-card-header"><div><span className={`task-status-dot ${task.status}`} /> <strong>{taskTypeLabel(task)} · {taskStatusLabel(task.status)}</strong></div><span className="task-time">最近更新：{taskUpdatedLabel(task)}</span></div><div className="task-source">{task.currentFile || task.source}</div><div className="task-progress-summary"><strong>{taskStageLabel(task.currentStage, task.currentSubstage)}</strong><span>已用时 {formatDuration(task.elapsedSeconds)}</span></div><div className="progress-track"><span style={{ width: `${task.progress * 100}%` }} /></div>{task.status === "succeeded" ? <button className="text-button" onClick={onOpenCatalog}>查看文件</button> : null}{["queued", "running", "cancelling"].includes(task.status) ? <button type="button" className="secondary-button" onClick={() => { if (onCancel) void onCancel(task.taskId); else void api.cancelTask(task.taskId); }}>{task.status === "cancelling" ? "正在取消…" : "取消整理"}</button> : null}{task.error ? <div className="task-error-panel"><strong>{task.error.message}</strong><span>{task.error.code}</span></div> : task.errorSummary ? <div className="task-error-panel">{task.errorSummary}</div> : null}</article>;
  }
  if (isFileInsightBatch) {
    const completed = Number(task.counts.completed ?? 0);
    const queued = Number(task.counts.queued ?? 0);
    const running = Number(task.counts.running ?? 0);
    const failed = Number(task.counts.failed ?? 0);
    return <article className="task-card task-card-readable file-insight-task-card">
      <div className="task-card-header"><div><span className={`task-status-dot ${task.status}`} /> <strong>{taskTypeLabel(task)} · {taskStatusLabel(task.status)}</strong></div><span className="task-time">最近更新：{taskUpdatedLabel(task)}</span></div>
      <div className="task-progress-summary"><strong>已完成 {number(completed)} / {number(task.total)}</strong><span>{taskStageLabel(task.currentStage, task.currentSubstage)}</span></div>
      <div className="progress-track"><span style={{ width: `${task.progress * 100}%` }} /></div>
      <div className="file-insight-queue-stats"><span>已完成 {number(completed)} / {number(task.total)}</span><span>等待 {number(queued)}</span><span>处理中 {number(running)}</span><span>失败 {number(failed)}</span></div>
      <div className="task-readable-grid"><span>当前：{task.currentFile || "—"}</span><span>有界上下文 · 不上传整个文件</span><span>运行时间：{formatDuration(task.elapsedSeconds)}</span></div>
      {task.status === "succeeded" ? <button className="text-button" onClick={onOpenCatalog}>查看本地文件</button> : null}
      {["queued", "running", "cancelling"].includes(task.status) ? <button type="button" className="secondary-button" onClick={() => { if (onCancel) void onCancel(task.taskId); else void api.cancelTask(task.taskId); }}>取消等待中的整理</button> : null}
      {task.error ? <div className="task-error-panel"><strong>{task.error.message}</strong></div> : null}
    </article>;
  }
  return <article className="task-card task-card-readable">
    <div className="task-card-header"><div><span className={`task-status-dot ${task.status}`} /> <strong>{taskTypeLabel(task)} · {taskStatusLabel(task.status)}</strong></div><span className="task-time">最近更新：{taskUpdatedLabel(task)}</span></div>
    <div className="task-source">{task.source}</div>
    {isFileTask ? <div className="task-progress-summary"><strong>{totalKnown ? `处理文件：${number(readyLocal)} / ${number(task.total)}` : "正在扫描并处理资料"}</strong><span>{totalKnown ? `阶段：${stage}` : `已发现：${number(discovered)} · 可查看：${number(readyLocal)}`}</span></div> : <div className="task-progress-summary"><strong>{stage}</strong><span>已用时 {formatDuration(task.elapsedSeconds)}</span></div>}
    <div className="progress-track"><span style={{ width: `${task.progress * 100}%` }} /></div>
    <div className="task-readable-grid"><span>当前：{task.currentFile || "—"}</span><span>{task.currentPage ? `页：${task.currentPage}${task.currentPageTotal ? ` / ${task.currentPageTotal}` : ""}` : `阶段：${taskStageLabel(task.currentStage, task.currentSubstage)}`}</span>{isFileTask ? <><span>已发现：{number(discovered)}</span><span>已登记：{number(registered)}</span><span>可查看：{number(readyLocal)}</span><span>失败：{number(failedLocal)}</span><span>跳过：{number(skippedLocal)}</span><span>{totalKnown ? `剩余：${number(remaining)}` : "总数：扫描中"}</span></> : <><span>已完成：{number(task.completed)}</span><span>需要审核：{number(task.counts.needsReview ?? task.counts.qualityIssues ?? 0)}</span><span>失败：{number(task.counts.filesFailed ?? 0)}</span><span>跳过：{number(task.counts.filesSkipped ?? 0)}</span><span>剩余：{number(remaining)}</span></>}<span>运行时间：{formatDuration(task.elapsedSeconds)}</span></div>
    {task.recentFilesPerMinute != null ? <div className="task-throughput">最近速度：{task.recentFilesPerMinute.toFixed(1)} 文件/分钟</div> : null}
    {task.status === "succeeded" ? <button className="text-button" onClick={onOpenCatalog}>查看已完成资料 →</button> : null}
    {task.error ? <div className="task-error-panel"><strong>{task.error.message}</strong><span>{taskStageLabel(task.error.stage)}{task.error.affectedFile ? ` · ${task.error.affectedFile}` : ""}</span><details><summary>查看技术详情</summary><div>错误代码：{task.error.code}</div>{task.error.requestId ? <div>请求 ID：{task.error.requestId}</div> : null}{task.error.technicalDetail ? <pre>{task.error.technicalDetail}</pre> : null}</details></div> : task.errorSummary ? <div className="task-error-panel">{task.errorSummary}</div> : null}
  </article>;
}

function TaskCard({ task, onOpenCatalog }: { task: Task; onOpenCatalog: () => void }) { return <article className="task-card"><div className="task-card-header"><div><span className={`task-status-dot ${task.status}`} /> <strong>{taskStatusLabel(task.status)}</strong></div><span className="task-time">{formatDate(task.startedAt)}</span></div><div className="task-source">{task.source}</div><div className="progress-track"><span style={{ width: `${task.progress * 100}%` }} /></div><div className="task-card-footer"><span>{STAGE_LABELS[task.currentStage] || task.currentStage} · {Math.round(task.progress * 100)}%</span>{task.currentFile ? <span className="task-current-file">当前：{task.currentFile}</span> : null}{task.currentPage ? <span className="task-current-file">Page {task.currentPage}</span> : null}{task.completed || task.total ? <span>{number(task.completed)} / {number(task.total)}</span> : null}<span>已用时 {number(task.elapsedSeconds)} 秒</span>{task.status === "succeeded" ? <button className="text-button" onClick={onOpenCatalog}>查看资产 →</button> : null}</div>{task.currentSubstage ? <div className="task-substage">{task.currentSubstage}</div> : null}{task.error ? <div className="task-error-panel"><strong>{task.error.message}</strong><span>{STAGE_LABELS[task.error.stage] || task.error.stage}{task.error.affectedFile ? ` · ${task.error.affectedFile}` : ""}</span><span>{task.error.retryable ? "可以重新处理" : "请查看技术详情"}</span><details><summary>查看技术详情</summary><div>错误代码：{task.error.code}</div>{task.error.requestId ? <div>请求 ID：{task.error.requestId}</div> : null}{task.error.technicalDetail ? <pre>{task.error.technicalDetail}</pre> : null}</details></div> : task.errorSummary ? <div className="task-error-panel">{task.errorSummary}</div> : null}{task.counts.tableAssets != null ? <div className="task-counts"><span>文件 {number(task.counts.filesDiscovered)}</span><span>表格 {number(task.counts.tableAssets)}</span><span>文本 {number(task.counts.textAssets)}</span><span>质量问题 {number(task.counts.qualityIssues)}</span></div> : null}</article>; }

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
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">LOCAL RETRIEVAL</div><h1>数据检索</h1><p className="heading-note">本地确定性检索：文件、表格元数据和文本段落；不使用模型改写或二次排序</p></div></div>
    <form className="search-command" onSubmit={(event) => { event.preventDefault(); onSubmit(); }}><span className="search-command-icon">⌕</span><input aria-label="搜索资料" value={input} onChange={(event) => onInput(event.target.value)} placeholder="搜索资料、表格、正文、列名……" /><button className="primary-button" type="submit">搜索</button></form>
    <div className="filter-panel search-filters"><div className="segmented"><button className={type === "all" ? "selected" : ""} onClick={() => onType("all")} type="button">全部</button><button className={type === "table" ? "selected" : ""} onClick={() => onType("table")} type="button">表格</button><button className={type === "text" ? "selected" : ""} onClick={() => onType("text")} type="button">文本</button></div><select value={quality} onChange={(event) => onQuality(event.target.value as "" | QualityStatus)}><option value="">全部质量</option><option value="ready">可直接使用</option><option value="needs_review">需要审核</option><option value="unusable">不可用</option></select><select value={format} onChange={(event) => onFormat(event.target.value)}><option value="">全部格式</option>{Object.keys(formats).sort().map((item) => <option key={item} value={item}>{item.toUpperCase()}</option>)}</select><select value={match} onChange={(event) => onMatch(event.target.value as "all" | "phrase")}><option value="all">按词匹配</option><option value="phrase">完整短语</option></select></div>
    {loading ? <div className="loading-box search-loading"><LoadingMessage loading label="正在检索本地目录……" /></div> : !submitted ? <EmptyState title="尚未输入搜索内容" body="输入关键词后，洞见会在本地目录与文本段落中检索。" /> : !query ? <EmptyState title="尚未输入搜索内容" body="搜索框为空；不会执行全库扫描。" /> : !hasAssets ? <EmptyState title="当前尚未处理任何资料" body="先处理一个本地资料目录，建立数据目录后再进行检索。" /> : !results.length ? <EmptyState title="没有找到匹配结果" body={`没有找到与“${query}”匹配的资料、列名或正文片段。`} /> : <><div className="list-meta"><span>{number(total)} 个结果</span><span>结果按本地确定性分数排序；每个资产最多显示 3 条</span></div><div className="search-results">{results.map((result) => <SearchResultItem key={result.resultId} result={result} onOpen={onOpen} />)}</div>{total > 30 ? <Pagination offset={offset} limit={30} total={total} onOffset={onOffset} /> : null}</>}
  </section>;
}

function SearchResultItem({ result, onOpen }: { result: SearchResult; onOpen: (result: SearchResult) => void }) {
  const location = [result.pageNumber != null ? `第 ${result.pageNumber} 页` : "", result.sheetName ? `工作表 ${result.sheetName}` : ""].filter(Boolean).join(" / ");
  return <button className="search-result" onClick={() => onOpen(result)}><div className={`asset-type-mark ${result.assetType}`}>{result.assetType === "table" ? "表" : "文"}</div><div className="search-result-main"><div className="search-result-title">{result.displayName}</div><div className="search-result-source">{result.sourceFile}{location ? ` · ${location}` : ""} · {result.sourceFormat?.toUpperCase() || "UNKNOWN"}</div><div className="search-snippet"><HighlightedSnippet result={result} /></div></div><div className="search-result-meta"><span className="search-match-kind">{matchKindLabel(result.matchKind)}</span><span className={`quality-pill ${result.qualityStatus}`}>{qualityLabel(result.qualityStatus)}</span><span className="search-score">{result.score.toFixed(1)}</span></div><span className="chevron">›</span></button>;
}

function LegacyHighlightedSnippet({ result }: { result: SearchResult }) {
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
  return <section className="page-section"><div className="page-heading"><div><div className="eyebrow">SAFE SQL WORKBENCH</div><h1>数据查询</h1><p className="heading-note">只读查询专用内存连接；只能访问明确选中的整理后数据</p></div></div>
    <div className="query-layout"><aside className="query-sidebar panel"><div className="panel-title"><h2>选择表格资产</h2><span>{selectedIds.length} / {schema?.limits.maxSelectedAssets ?? 8}</span></div>{assets.length ? <div className="query-asset-list">{assets.map((asset) => <label className="query-asset" key={asset.assetId}><input type="checkbox" checked={selectedIds.includes(asset.assetId)} onChange={() => onToggle(asset.assetId)} /><span className="query-asset-copy"><strong>{asset.effectiveDisplayName || asset.fallbackDisplayName}</strong><small>{asset.source.relativePath} · {dimensions(asset)}</small></span><button type="button" className="text-button" onClick={() => onOpen(asset.assetId)}>查看</button></label>)}</div> : <EmptyState compact title="暂无表格资产" body="先处理一个本地资料目录。" />}</aside>
      <div className="query-workspace">{error ? <div className="query-error" role="alert">{error}</div> : null}<div className="panel query-panel"><div className="panel-title"><div><h2>SQL</h2><span className="panel-note">禁止 DDL/DML、文件函数、扩展和多语句；结果最多 500 行；选中表总行数超过 {schema?.limits.maxInputRows ?? 50000} 时会拒绝执行</span></div><button className="primary-button" disabled={!selectedIds.length || !sql.trim() || loading} onClick={onRun}>{loading ? "执行中…" : "运行查询"}</button></div>{schema?.relations.length ? <div className="relation-map">{schema.relations.map((relation) => <div className="relation-chip" key={relation.alias}><b>{relation.alias}</b><span>{relation.displayName}</span></div>)}</div> : <div className="query-hint">请选择 1～8 个表格资产，系统会映射为 t1、t2……</div>}<textarea className="sql-editor" value={sql} onChange={(event) => onSql(event.target.value)} spellCheck={false} aria-label="SQL 查询" placeholder="SELECT * FROM t1 LIMIT 20" />{result ? <QueryResult result={result} /> : <div className="query-empty">查询结果会显示在这里。当前不会自动生成 SQL。</div>}</div></div>
    </div>
  </section>;
}

function QueryResult({ result }: { result: SqlQueryResponse }) {
  return <div className="query-result"><div className="query-result-meta"><span>{number(result.rowCount)} 行 · {result.executionMs.toFixed(1)} ms</span>{result.truncated ? <strong>结果已截断（受本地上限限制）</strong> : null}<span className="sandbox-label">{result.sandbox}</span></div><div className="table-wrap"><table><thead><tr>{result.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{result.rows.map((row, index) => <tr key={index}>{result.columns.map((column) => <td key={column} title={displayValue(row[column])}>{displayValue(row[column])}</td>)}</tr>)}</tbody></table></div></div>;
}

function DetailPage({ detail, tab, onTab, tableLayer, onTableLayer, tableOffset, onTableOffset, tablePreview, textPreview, semanticConfigured, semanticEnriching, semanticNotice, onSemanticEnrich, onBack, onUpdateIssue }: { detail: AssetDetail; tab: "data" | "profile" | "quality" | "source" | "semantic"; onTab: (value: "data" | "profile" | "quality" | "source" | "semantic") => void; tableLayer: "raw" | "normalized"; onTableLayer: (value: "raw" | "normalized") => void; tableOffset: number; onTableOffset: (value: number) => void; tablePreview: TablePreview | null; textPreview: TextPreview | null; semanticConfigured: boolean; semanticEnriching: boolean; semanticNotice: string; onSemanticEnrich: () => void; onBack: () => void; onUpdateIssue: (issue: QualityIssue, status: QualityIssue["status"]) => void }) {
  return <section className="page-section detail-section"><button className="back-button" onClick={onBack}>← 数据目录</button><div className="detail-heading"><div className={`asset-type-mark large ${detail.assetType}`}>{detail.assetType === "table" ? "表" : "文"}</div><div><div className="eyebrow">{typeLabel(detail.assetType)}资产</div><h1>{detail.displayName}</h1><div className="detail-source">{detail.source.relativePath} <span>·</span> {detail.source.format?.toUpperCase()}</div></div><div className={`quality-pill ${detail.qualityStatus}`}>{qualityLabel(detail.qualityStatus)}</div></div><div className="detail-tabs">{([["data", "数据"], ["profile", "画像"], ["quality", `质量${detail.qualityIssues.length ? ` · ${detail.qualityIssues.length}` : ""}`], ["source", "来源"], ["semantic", "技术详情"]] as const).map(([key, label]) => <button key={key} className={tab === key ? "active" : ""} onClick={() => onTab(key)}>{label}</button>)}</div>{tab === "data" ? detail.assetType === "table" ? <TableData detail={detail} layer={tableLayer} onLayer={onTableLayer} offset={tableOffset} onOffset={onTableOffset} preview={tablePreview} /> : <TextData detail={detail} preview={textPreview} /> : null}{tab === "profile" ? <ProfileView detail={detail} /> : null}{tab === "quality" ? <DetailQuality detail={detail} onUpdate={onUpdateIssue} /> : null}{tab === "source" ? <SourceView detail={detail} /> : null}{tab === "semantic" ? <SemanticView detail={detail} configured={semanticConfigured} enriching={semanticEnriching} notice={semanticNotice} onEnrich={onSemanticEnrich} /> : null}</section>;
}

function TableData({ detail, layer, onLayer, offset, onOffset, preview }: { detail: AssetDetail; layer: "raw" | "normalized"; onLayer: (value: "raw" | "normalized") => void; offset: number; onOffset: (value: number) => void; preview: TablePreview | null }) {
  return <div className="detail-panel"><div className="data-toolbar"><div><h2>表格预览</h2><p>默认查看规范化结果；原始结果保持不变</p></div><div className="segmented"><button className={layer === "normalized" ? "selected" : ""} onClick={() => { onLayer("normalized"); onOffset(0); }}>规范化</button><button className={layer === "raw" ? "selected" : ""} onClick={() => { onLayer("raw"); onOffset(0); }}>原始</button></div></div>{preview ? <NormalizedTableRenderer preview={preview} rowOffset={offset} paginate onOffset={onOffset} total={preview.pagination.total ?? detail.dimensions.rows ?? 0} /> : <div className="loading-box">读取表格预览…</div>}</div>;
}

function TextData({ detail, preview }: { detail: AssetDetail; preview: TextPreview | null }) { return <div className="detail-panel"><div className="data-toolbar"><div><h2>文本预览</h2><p>{sourceKindLabel(detail.provenance.sourceKind)} · {number(detail.dimensions.chunks)} 段文本 · {number(detail.dimensions.chars)} 字符</p></div><span className="source-badge">{detail.provenance.extractor.toLowerCase().includes("ocr") ? "OCR" : "文本层"}</span></div>{preview ? <><div className="text-preview" aria-label="normalized text preview">{preview.text || "（空文本）"}</div><div className="preview-foot">显示 {number(preview.offset)}–{number(preview.offset + preview.text.length)} / {number(preview.chars)} 字符 · {layerLabel(preview.layer)}</div></> : <div className="loading-box">读取文本预览…</div>}</div>; }

function ProfileView({ detail }: { detail: AssetDetail }) { const profile = detail.profile; if (!profile) return <EmptyState title="暂无画像" body="该资产尚未完成确定性数据画像。" />; const columns = Array.isArray(profile.columns) ? profile.columns as Array<Record<string, unknown>> : []; return <div className="detail-panel"><div className="data-toolbar"><div><h2>确定性画像</h2><p>画像只描述数据，不改变原始或规范化结果。</p></div></div><div className="profile-summary"><span>行 {number(Number(profile.row_count ?? profile.char_count ?? 0))}</span><span>列 {number(Number(profile.column_count ?? 0))}</span><span>空值 {number(Number(profile.null_count ?? 0))}</span><span>版本 {String(profile.profile_version ?? "—")}</span></div>{columns.length ? <div className="profile-columns">{columns.map((column) => <div className="profile-column" key={String(column.name)}><div><strong>{String(column.name)}</strong><span>{String(column.inferred_physical_type ?? "string")}</span></div><div className="profile-column-stats"><span>空值 {number(Number(column.null_count ?? 0))} · 不同值 {number(Number(column.distinct_count ?? 0))}</span><span>示例：{compactValue(column.sample_values)}</span></div></div>)}</div> : <pre className="json-preview">{JSON.stringify(profile, null, 2)}</pre>}</div>; }

function DetailQuality({ detail, onUpdate }: { detail: AssetDetail; onUpdate: (issue: QualityIssue, status: QualityIssue["status"]) => void }) { return <div className="detail-panel">{detail.qualityIssues.length ? <div className="quality-list compact">{detail.qualityIssues.map((issue) => <IssueCard key={issue.issue_id} issue={issue} onUpdate={onUpdate} onOpen={() => undefined} onOpenFile={() => undefined} />)}</div> : <EmptyState title="暂无质量问题" body="这个资产目前没有记录的质量问题。" />}</div>; }

function SourceView({ detail }: { detail: AssetDetail }) { return <div className="detail-panel"><div className="source-grid"><SourceField label="来源文件" value={detail.source.relativePath} /><SourceField label="格式" value={detail.source.format?.toUpperCase()} /><SourceField label="SHA-256" value={detail.source.sha256} wide /><SourceField label="file_id" value={detail.source.fileId} wide /><SourceField label="提取器" value={`${detail.provenance.extractor} · ${detail.provenance.extractorVersion}`} /><SourceField label="提取运行 ID" value={detail.provenance.extractionRunId} /><SourceField label="工作表 / 页面" value={[detail.provenance.sheetName, detail.provenance.pageNumber ? `第 ${detail.provenance.pageNumber} 页` : null].filter(Boolean).join(" / ") || "—"} /><SourceField label="来源范围" value={compactValue(detail.provenance.sourceRange)} wide /><SourceField label="原始结果" value={detail.artifacts.raw} wide /><SourceField label="规范化结果" value={detail.artifacts.normalized} wide /><SourceField label="数据画像" value={detail.artifacts.profile} wide /></div></div>; }

function SourceField({ label, value, wide }: { label: string; value: unknown; wide?: boolean }) { return <div className={`source-field ${wide ? "wide" : ""}`}><span>{label}</span><code>{String(value ?? "—")}</code></div>; }

function SemanticView({ detail, configured, enriching, notice, onEnrich }: { detail: AssetDetail; configured: boolean; enriching: boolean; notice: string; onEnrich: () => void }) {
  const semantic = detail.semantic;
  const action = configured ? <button className="primary-button semantic-action" disabled={enriching} onClick={onEnrich}>{enriching ? "整理中..." : semantic && detail.semanticStatus === "enriched" ? "再次 AI 整理" : "AI 整理"}</button> : null;
  if (!semantic || detail.semanticStatus !== "enriched") {
    return <div className="detail-panel semantic-empty"><div className="semantic-lock">AI</div><h2>{configured ? "尚未进行 AI 整理" : "尚未配置 AI 模型"}</h2><p>{configured ? "当前资产还没有 AI 整理结果。" : "请先在设置中完成并测试 AI 模型，才能手动发起单个资产的 AI 整理。"}</p>{action}<span>当前状态：待整理</span></div>;
  }
  const fields = Array.isArray(semantic.semanticFields) ? semantic.semanticFields : [];
  return <div className="detail-panel semantic-panel">
    <div className="semantic-header"><div><div className="eyebrow">AI 整理结果</div><h2>{semantic.display_name}</h2></div><div className="semantic-header-actions">{action}<span className="semantic-confidence">可信度 {semantic.confidence.toFixed(2)}</span></div></div>
    {notice ? <div className="semantic-notice" role="status">{notice}</div> : null}
    <div className="semantic-facts"><div><span>分类</span><strong>{semantic.category}</strong></div><div><span>模型</span><strong>{semantic.model}</strong></div><div><span>提示版本</span><strong>{semantic.prompt_version}</strong></div><div><span>运行 ID</span><strong>{semantic.semantic_run_id}</strong></div></div>
    <div className="semantic-copy"><h3>描述</h3><p>{semantic.description}</p><h3>摘要</h3><p>{semantic.summary}</p><h3>关键词</h3><div className="keyword-list">{semantic.keywords.map((keyword) => <span key={keyword}>{keyword}</span>)}</div></div>
    {detail.assetType === "table" ? <><h3 className="semantic-subheading">字段说明</h3>{fields.length ? <div className="table-wrap"><table><thead><tr><th>来源列</th><th>语义名称</th><th>说明</th><th>类型</th><th>单位</th><th>可信度</th></tr></thead><tbody>{fields.map((field) => <tr key={`${field.source_column}-${field.semantic_name}`}><td>{field.source_column}</td><td>{field.semantic_name}</td><td>{field.description}</td><td>{field.semantic_type}</td><td>{field.unit ?? "—"}</td><td>{field.confidence.toFixed(2)}</td></tr>)}</tbody></table></div> : <p className="muted">AI 未返回字段说明。</p>}</> : null}
  </div>;
}

function SemanticConfirmDialog({ assetName, onCancel, onConfirm }: { assetName: string; onCancel: () => void; onConfirm: () => void }) {
  return <div className="dialog-backdrop" role="presentation"><div className="dialog semantic-confirm" role="dialog" aria-modal="true" aria-labelledby="semantic-confirm-title"><div className="dialog-header"><div><div className="eyebrow">EXPLICIT PROVIDER ACTION</div><h2 id="semantic-confirm-title">确认 AI 整理</h2></div><button className="close-button" onClick={onCancel} aria-label="取消">×</button></div><p className="semantic-confirm-asset">当前资产：{assetName}</p><p>AI 整理将向当前配置的大模型服务发送该资产的受控摘要/样本。</p><p>不发送原始文件；不发送完整大型表格；不修改原始或规范化数据。</p><div className="dialog-actions"><button className="secondary-button" onClick={onCancel}>取消</button><button className="primary-button" onClick={onConfirm}>开始整理</button></div></div></div>;
}

function ProcessDialog({ source, onSource, visionMode, onVisionMode, visionEnabled, autoFileInsight, onAutoFileInsight, onClose, onSubmit }: { source: string; onSource: (value: string) => void; visionMode: VisionMode; onVisionMode: (value: VisionMode) => void; visionEnabled: boolean; autoFileInsight: boolean; onAutoFileInsight: (value: boolean) => void; onClose: () => void; onSubmit: () => void }) {
  return <div className="dialog-backdrop" role="presentation"><div className="dialog" role="dialog" aria-modal="true"><div className="dialog-header"><div><div className="eyebrow">NEW PROCESS</div><h2>处理新目录</h2></div><button className="close-button" onClick={onClose}>×</button></div><label className="field-label" htmlFor="source-path">资料目录</label><input id="source-path" className="path-input" value={source} onChange={(event) => onSource(event.target.value)} placeholder="例如：E:\\Research\\Project" autoFocus /><p className="field-help">请输入或粘贴本机目录路径。浏览器不会直接读取本机目录。</p><div className="field-label">内容提取</div><div className="process-mode-options"><label><input type="radio" name="vision-mode" checked={visionMode === "local"} onChange={() => onVisionMode("local")} /> 本地内容提取（离线 OCR / 表格识别）</label></div><div className="field-label">AI视觉增强</div><label className="process-option"><input type="checkbox" checked={visionMode === "ai_vision"} disabled={!visionEnabled} onChange={(event) => onVisionMode(event.target.checked ? "ai_vision" : "local")} /> 对图片 / 扫描 PDF 使用 AI Vision</label>{visionEnabled ? <p className="field-help">仅图片和明确扫描 PDF 页面会发送至当前配置的 AI 服务。</p> : <p className="field-help">AI Vision 尚未启用；默认使用本地离线提取。</p>}<div className="field-label">AI 文件整理</div><label className="process-option"><input type="checkbox" checked={autoFileInsight} onChange={(event) => onAutoFileInsight(event.target.checked)} /> 文件处理完成后自动 AI 整理</label><p className="field-help">这是独立于内容提取的可选后台任务；默认关闭，开启后每个已完成文件会消耗一次 AI 整理请求。</p><div className="dialog-actions"><button className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={!source.trim()} onClick={onSubmit}>开始处理</button></div></div></div>;
}

function Pagination({ offset, limit, total, onOffset }: { offset: number; limit: number; total: number; onOffset: (value: number) => void }) { const page = Math.floor(offset / limit) + 1; const pages = Math.max(1, Math.ceil(total / limit)); return <div className="pagination"><span>第 {number(page)} / {number(pages)} 页</span><div><button disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - limit))}>上一页</button><button disabled={offset + limit >= total} onClick={() => onOffset(offset + limit)}>下一页</button></div></div>; }

function EmptyState({ title, body, compact = false }: { title: string; body: string; compact?: boolean }) { return <div className={`empty-state ${compact ? "compact" : ""}`}><div className="empty-mark">○</div><strong>{title}</strong><span>{body}</span></div>; }

function compactValue(value: unknown): string { if (value == null) return "—"; const text = typeof value === "string" ? value : JSON.stringify(value); return text.length > 180 ? `${text.slice(0, 180)}…` : text; }
function displayValue(value: unknown): string { if (value == null || value === "") return ""; if (typeof value === "object") return compactValue(value); return String(value); }

type FileLocator = {
  page?: number;
  sheet?: string;
  textAssetId?: string;
  textOffset?: number;
  matchStart?: number;
  matchEnd?: number;
  tableAssetId?: string;
  tableRow?: number;
  tableColumn?: number;
  tableMatchStart?: number;
  tableMatchEnd?: number;
};

type ReportsPageProps = {
  runs: AnalysisRunSummary[];
  reports: ReportSummary[];
  selected: Report | null;
  selectedRunIds: string[];
  title: string;
  purpose: string;
  task: Task | null;
  loading: boolean;
  error: string;
  onToggleRun: (runId: string) => void;
  onTitle: (value: string) => void;
  onPurpose: (value: string) => void;
  onStart: () => void;
  onCancel: () => void;
  onOpen: (reportId: string) => void;
  onExport: (format: "markdown" | "html") => void;
  onOpenAsset: (assetId: string) => void;
};

function reportEvidenceLabel(evidence: ReportEvidence): string {
  if (evidence.kind === "sql_result") return "SQL 分析结果";
  const source = evidence.source ?? {};
  const parts = [String(source.relativePath ?? evidence.display_name ?? "数据来源")];
  if (source.sheetName) parts.push(`Sheet ${source.sheetName}`);
  if (source.pageNumber != null) parts.push(`第 ${source.pageNumber} 页`);
  return parts.join(" / ");
}

function reportSources(ids: string[], evidence: ReportEvidence[]): ReportEvidence[] {
  const byId = new Map(evidence.map((item) => [item.evidence_id, item]));
  return ids.map((id) => byId.get(id)).filter((item): item is ReportEvidence => item != null);
}

function ReportsPage({ runs, reports, selected, selectedRunIds, title, purpose, task, loading, error, onToggleRun, onTitle, onPurpose, onStart, onCancel, onOpen, onExport, onOpenAsset }: ReportsPageProps) {
  const running = task != null && ["queued", "running", "cancelling"].includes(task.status);
  return <section className="page-section reports-page">
    <div className="page-heading"><div><div className="eyebrow">ANALYSIS REPORT V1</div><h1>报告</h1><p className="heading-note">报告只基于已保存的分析证据，不会重新扫描工作区或执行查询。</p></div></div>
    <div className="reports-layout">
      <section className="panel report-compose-panel">
        <div className="panel-title"><div><h2>生成新报告</h2><span className="panel-note">最多选择 8 次已完成分析，可离线生成基础报告。</span></div></div>
        <div className="report-form">
          <label className="field-label" htmlFor="report-title">报告标题（可选）</label>
          <input id="report-title" className="path-input" value={title} onChange={(event) => onTitle(event.target.value)} maxLength={200} placeholder="例如：2025 年度业务情况综合分析" />
          <label className="field-label report-label-gap" htmlFor="report-purpose">报告目的 / 补充说明（可选）</label>
          <textarea id="report-purpose" className="report-purpose" value={purpose} onChange={(event) => onPurpose(event.target.value)} maxLength={2000} placeholder="说明希望读者重点关注的内容" />
          <div className="report-run-picker"><div className="field-label">报告来源分析记录（{selectedRunIds.length} / 8）</div>{runs.length ? runs.map((run) => <label className="report-run-option" key={run.analysis_run_id}><input type="checkbox" checked={selectedRunIds.includes(run.analysis_run_id)} onChange={() => onToggleRun(run.analysis_run_id)} /><span><strong>{run.question}</strong><small>{formatDate(run.created_at)} · {run.status === "insufficient_evidence" ? "证据不足" : "已完成"}</small></span></label>) : <div className="report-empty-note">暂无已完成的分析记录，请先在 AI 分析页完成一次分析。</div>}</div>
          <div className="report-ai-note">{task?.status === "succeeded" ? "报告已保存" : "模型不可用时自动生成基础报告；模型失败不会丢失报告。"}</div>
          <button className="primary-button" disabled={loading || running || !selectedRunIds.length} onClick={onStart}>{loading ? "准备中…" : running ? "报告生成中…" : "生成报告"}</button>
          {running ? <button className="secondary-button report-cancel" onClick={onCancel} disabled={task?.status === "cancelling"}>{task?.status === "cancelling" ? "正在取消…" : "取消生成"}</button> : null}
          {error ? <div className="analysis-error" role="alert">{error}</div> : null}
        </div>
      </section>
      <section className="reports-result-column">
        {running && task ? <section className="panel report-progress"><div className="panel-title"><div><h2>报告生成中</h2><span className="panel-note">当前阶段：{taskStageLabel(task.currentStage, task.currentSubstage)}</span></div><strong>{task.currentStage === "calling_model" || task.currentSubstage === "calling_model" ? "AI 已处理" : "已用时"} {number(task.elapsedSeconds)} 秒</strong></div><div className="progress-track"><span style={{ width: `${task.progress * 100}%` }} /></div><div className="analysis-progress-meta"><span>当前步骤：{task.currentStep ?? 0} / 1</span><span>已用时 {number(task.elapsedSeconds)} 秒</span><span>{task.status === "cancelling" ? "正在取消" : "有界处理"}</span></div></section> : null}
        {selected ? <ReportDetail report={selected} onExport={onExport} onOpenAsset={onOpenAsset} /> : <div className="panel analysis-empty"><strong>{running ? "正在整理报告" : "选择一个报告查看详情"}</strong><span>报告会在生成完成后保存在本地，刷新页面或重启应用后仍可查看。</span></div>}
        <section className="panel report-history"><div className="panel-title"><div><h2>最近报告</h2><span className="panel-note">损坏的单个报告文件不会影响其他报告。</span></div></div>{reports.length ? <div className="report-history-list">{reports.map((item) => <button className="report-history-item" type="button" key={item.report_id} onClick={() => onOpen(item.report_id)}><span><strong>{item.title}</strong><small>基于 {item.source_analysis_run_ids.length} 次分析 · {formatDate(item.created_at)} · {item.generation_mode === "ai_enhanced" ? "AI 辅助整理" : "基础报告"}</small></span><span className="text-button">查看</span></button>)}</div> : <div className="report-empty-note">暂无报告。</div>}</section>
      </section>
    </div>
  </section>;
}

type ReportsPageV2Props = {
  files: FileSummary[];
  reports: ReportSummary[];
  selected: Report | null;
  selectedFileIds: string[];
  selectAllCurrentFilter: boolean;
  reportType: "overview" | "analysis";
  title: string;
  purpose: string;
  task: Task | null;
  loading: boolean;
  error: string;
  onToggleFile: (fileId: string) => void;
  onSelectAll: (filters?: Record<string, string>) => void;
  onClearSelection: () => void;
  onReportType: (value: "overview" | "analysis") => void;
  onTitle: (value: string) => void;
  onPurpose: (value: string) => void;
  onStart: () => void;
  onCancel: () => void;
  onOpen: (reportId: string) => void;
  onExport: (format: "markdown" | "html") => void;
  onOpenAsset: (assetId: string) => void;
};

function ReportsPageV2({ files, reports, selected, selectedFileIds, selectAllCurrentFilter, reportType, title, purpose, task, loading, error, onToggleFile, onSelectAll, onClearSelection, onReportType, onTitle, onPurpose, onStart, onCancel, onOpen, onExport, onOpenAsset }: ReportsPageV2Props) {
  const [fileQuery, setFileQuery] = useState("");
  const [formatFilter, setFormatFilter] = useState("");
  const [processingFilter, setProcessingFilter] = useState("");
  const [insightFilter, setInsightFilter] = useState("");
  const [pickerFiles, setPickerFiles] = useState<FileSummary[]>(files);
  const [pickerTotal, setPickerTotal] = useState(files.length);
  const [pickerOffset, setPickerOffset] = useState(0);
  const [pickerLoading, setPickerLoading] = useState(false);

  useEffect(() => {
    setPickerOffset(0);
  }, [fileQuery, formatFilter]);

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({ limit: "100", offset: String(pickerOffset) });
    if (fileQuery.trim()) params.set("q", fileQuery.trim());
    if (formatFilter) params.set("format", formatFilter);
    setPickerLoading(true);
    void api.files(params).then((result) => {
      if (!active) return;
      setPickerFiles(result.items);
      setPickerTotal(result.pagination.total);
    }).catch(() => {
      if (active) {
        setPickerFiles(files);
        setPickerTotal(files.length);
      }
    }).finally(() => {
      if (active) setPickerLoading(false);
    });
    return () => { active = false; };
  }, [fileQuery, files, formatFilter, pickerOffset]);

  const filteredFiles = pickerFiles.filter((file) => {
    const query = fileQuery.trim().toLowerCase();
    return (!query || `${file.displayName} ${file.relativePath}`.toLowerCase().includes(query))
      && (!formatFilter || file.format.toLowerCase() === formatFilter)
      && (!processingFilter || file.processingStatus === processingFilter)
      && (!insightFilter || file.fileInsightStatus === insightFilter);
  });
  const pickerFormats = Array.from(new Set([...files, ...pickerFiles].map((file) => file.format.toLowerCase()))).sort();
  const selectCurrentFilter = () => onSelectAll({
    query: fileQuery.trim(),
    format: formatFilter,
    processingStatus: processingFilter,
    fileInsightStatus: insightFilter,
  });
  const running = task != null && ["queued", "running", "cancelling"].includes(task.status);
  return <section className="page-section reports-page">
    <div className="page-heading"><div><div className="eyebrow">GROUNDED REPORTS</div><h1>报告</h1><p className="heading-note">选择资料后生成报告。未选择资料时不会自动扩大范围。</p></div></div>
    <div className="reports-layout">
      <section className="panel report-compose-panel"><div className="panel-title"><div><h2>生成新报告</h2><span className="panel-note">文件综述复用已完成的 AI 文件整理与文件证据；数据分析报告使用本地统计、搜索和上下文。</span></div></div>
        <div className="report-form">
          <label className="field-label" htmlFor="report-title-m2">报告标题（可选）</label><input id="report-title-m2" className="path-input" value={title} onChange={(event) => onTitle(event.target.value)} maxLength={200} placeholder="例如：项目资料综述" />
          <label className="field-label report-label-gap" htmlFor="report-purpose-m2">报告目的 / 补充说明（可选）</label><textarea id="report-purpose-m2" className="report-purpose" value={purpose} onChange={(event) => onPurpose(event.target.value)} maxLength={2000} placeholder="说明希望读者关注的内容" />
          <label className="field-label" htmlFor="report-type-m2">报告类型</label><select id="report-type-m2" className="path-input" value={reportType} onChange={(event) => onReportType(event.target.value as "overview" | "analysis")}><option value="overview">文件综述</option><option value="analysis">数据分析报告</option></select>
          <div className="report-run-picker"><div className="report-picker-toolbar"><strong>文件选择</strong><span>{selectAllCurrentFilter ? "已选当前筛选资料" : `已选 ${number(selectedFileIds.length)}`}</span><button type="button" className="text-button" onClick={selectCurrentFilter}>全部当前筛选资料</button><button type="button" className="text-button" onClick={onClearSelection}>清空选择</button></div><div className="report-picker-filters"><input className="path-input" aria-label="搜索文件" placeholder="搜索文件" value={fileQuery} onChange={(event) => setFileQuery(event.target.value)} /><select value={formatFilter} aria-label="格式" onChange={(event) => setFormatFilter(event.target.value)}><option value="">格式</option>{pickerFormats.map((format) => <option key={format} value={format}>{format.toUpperCase()}</option>)}</select><select value={processingFilter} aria-label="本地状态" onChange={(event) => setProcessingFilter(event.target.value)}><option value="">本地状态</option><option value="ready">已可查看</option><option value="processing">处理中</option><option value="failed">失败</option></select><select value={insightFilter} aria-label="AI 整理状态" onChange={(event) => setInsightFilter(event.target.value)}><option value="">AI 整理状态</option><option value="not_started">尚未整理</option><option value="queued">等待队列</option><option value="completed">已整理</option><option value="failed">整理失败</option></select></div><div className="report-picker-scroll" aria-busy={pickerLoading}>{filteredFiles.map((file) => <label className="report-run-option" key={file.fileId}><input type="checkbox" checked={selectAllCurrentFilter || selectedFileIds.includes(file.fileId)} disabled={selectAllCurrentFilter} onChange={() => onToggleFile(file.fileId)} /><span><strong>{file.displayName}</strong><small>{file.relativePath} · {file.processingStatus === "ready" ? "已可查看" : file.processingStatus} · AI {file.fileInsightStatus === "completed" ? "已整理" : file.fileInsightStatus === "queued" ? "等待队列" : file.fileInsightStatus === "failed" ? "整理失败" : "尚未整理"}</small></span></label>)}{!filteredFiles.length ? <div className="report-empty-note">当前页没有匹配文件。</div> : null}</div>{pickerTotal > 100 ? <div className="report-picker-pagination"><button type="button" className="text-button" disabled={pickerOffset === 0} onClick={() => setPickerOffset(Math.max(0, pickerOffset - 100))}>上一页</button><span>{number(Math.floor(pickerOffset / 100) + 1)} / {number(Math.max(1, Math.ceil(pickerTotal / 100)))}</span><button type="button" className="text-button" disabled={pickerOffset + 100 >= pickerTotal} onClick={() => setPickerOffset(pickerOffset + 100)}>下一页</button></div> : null}</div>
          <div className="report-ai-note">生成过程会显示加载来源、准备分析、调用模型、验证和保存阶段；模型不可用时文件综述仍可生成离线基础结果。</div>
          <button className="primary-button" disabled={loading || running} onClick={onStart}>{loading ? "准备中…" : running ? "报告生成中…" : "生成报告"}</button>{loading ? <div className="loading-box report-request-loading"><LoadingMessage loading label="正在准备报告请求…" /></div> : null}{running ? <button className="secondary-button report-cancel" onClick={onCancel} disabled={task?.status === "cancelling"}>{task?.status === "cancelling" ? "正在取消…" : "取消生成"}</button> : null}{error ? <div className="analysis-error" role="alert">{error}</div> : null}
        </div>
      </section>
      <section className="reports-result-column">{running && task ? <section className="panel report-progress"><div className="panel-title"><div><h2>报告生成中</h2><span className="panel-note">当前阶段：{taskStageLabel(task.currentStage, task.currentSubstage)}</span></div><strong>{task.currentStage === "calling_model" || task.currentSubstage === "calling_model" ? "AI 已处理" : "已用时"} {number(task.elapsedSeconds)} 秒</strong></div><div className="analysis-progress-meta"><span>当前步骤：{task.currentStep ?? 0} / 1</span><span>已用时：{number(task.elapsedSeconds)} 秒</span><span>{task.status === "cancelling" ? "正在取消" : "后台处理中"}</span></div></section> : null}{selected ? <ReportDetail report={selected} onExport={onExport} onOpenAsset={onOpenAsset} /> : <div className="panel analysis-empty"><strong>{running ? "正在整理报告" : "选择一个报告查看详情"}</strong><span>报告会保存在本地，完成后可继续查看或导出。</span></div>}<section className="panel report-history"><div className="panel-title"><div><h2>最近报告</h2><span className="panel-note">点击报告查看摘要、发现、限制和折叠的来源证据。</span></div></div>{reports.length ? <div className="report-history-list">{reports.map((item) => <button className="report-history-item" type="button" key={item.report_id} onClick={() => onOpen(item.report_id)}><span><strong>{item.title}</strong><small>{item.report_type === "overview" ? "文件综述" : "数据分析报告"} · {formatDate(item.created_at)} · {item.generation_mode === "ai_enhanced" ? "AI 辅助整理" : "离线基础报告"}</small></span><span className="text-button">查看</span></button>)}</div> : <div className="report-empty-note">暂无报告。</div>}</section></section>
    </div>
  </section>;
}

function LegacyReportsPageV2({ files, reports, selected, selectedFileIds, title, purpose, task, loading, error, onToggleFile, onTitle, onPurpose, onStart, onCancel, onOpen, onExport, onOpenAsset }: ReportsPageV2Props) {
  const running = task != null && ["queued", "running", "cancelling"].includes(task.status);
  const allFiles = selectedFileIds.length === 0;
  return <section className="page-section reports-page">
    <div className="page-heading"><div><div className="eyebrow">GROUNDED REPORTS</div><h1>报告</h1><p className="heading-note">选择资料后生成报告。系统会在后台准备必要的分析证据，不需要额外创建分析记录。</p></div></div>
    <div className="reports-layout">
      <section className="panel report-compose-panel">
        <div className="panel-title"><div><h2>生成新报告</h2><span className="panel-note">优先复用已经完成的文件整理；需要跨文件推理时由系统自动创建内部分析。</span></div></div>
        <div className="report-form">
          <label className="field-label" htmlFor="report-title-v2">报告标题（可选）</label>
          <input id="report-title-v2" className="path-input" value={title} onChange={(event) => onTitle(event.target.value)} maxLength={200} placeholder="例如：2025 年度业务情况综合分析" />
          <label className="field-label report-label-gap" htmlFor="report-purpose-v2">报告目的 / 补充说明（可选）</label>
          <textarea id="report-purpose-v2" className="report-purpose" value={purpose} onChange={(event) => onPurpose(event.target.value)} maxLength={2000} placeholder="说明希望读者重点关注的内容" />
          <div className="report-run-picker"><div className="field-label">选择资料</div>
            <label className="report-run-option"><input type="checkbox" checked={allFiles} onChange={() => { (allFiles ? files : selectedFileIds.map((id) => ({ fileId: id } as FileSummary))).forEach((file) => onToggleFile(file.fileId)); }} /><span><strong>全部已处理资料</strong><small>包括当前目录中可查看的文件；内部自动排除未完成内容。</small></span></label>
            {!allFiles && files.map((file) => <label className="report-run-option" key={file.fileId}><input type="checkbox" checked={selectedFileIds.includes(file.fileId)} onChange={() => onToggleFile(file.fileId)} /><span><strong>{file.displayName}</strong><small>{file.relativePath} · {file.processingStatus}</small></span></label>)}
            {allFiles ? <div className="report-empty-note">默认使用全部资料。若要缩小范围，取消上面的选择后勾选当前目录页中的文件。</div> : null}
          </div>
          <div className="report-ai-note">模型不可用时自动生成离线基础报告；AI 失败不会让本地文件失效。</div>
          <button className="primary-button" disabled={loading || running} onClick={onStart}>{loading ? "准备中…" : running ? "报告生成中…" : "生成报告"}</button>
          {running ? <button className="secondary-button report-cancel" onClick={onCancel} disabled={task?.status === "cancelling"}>{task?.status === "cancelling" ? "正在取消…" : "取消生成"}</button> : null}
          {error ? <div className="analysis-error" role="alert">{error}</div> : null}
        </div>
      </section>
      <section className="reports-result-column">
        {running && task ? <section className="panel report-progress"><div className="panel-title"><div><h2>报告生成中</h2><span className="panel-note">当前阶段：{taskStageLabel(task.currentStage, task.currentSubstage)}</span></div><strong>{task.currentStage === "calling_model" || task.currentSubstage === "calling_model" ? "AI 已处理" : "已用时"} {number(task.elapsedSeconds)} 秒</strong></div><div className="progress-track"><span style={{ width: `${task.progress * 100}%` }} /></div><div className="analysis-progress-meta"><span>当前步骤：{task.currentStep ?? 0} / 1</span><span>已用时：{number(task.elapsedSeconds)} 秒</span><span>{task.status === "cancelling" ? "正在取消" : "后台处理中"}</span></div></section> : null}
        {selected ? <ReportDetail report={selected} onExport={onExport} onOpenAsset={onOpenAsset} /> : <div className="panel analysis-empty"><strong>{running ? "正在整理报告" : "选择一个报告查看详情"}</strong><span>报告会保存在本地，完成后可继续查看或导出。</span></div>}
        <section className="panel report-history"><div className="panel-title"><div><h2>最近报告</h2><span className="panel-note">报告保留内部证据链，页面只呈现资料选择、生成和结果。</span></div></div>{reports.length ? <div className="report-history-list">{reports.map((item) => <button className="report-history-item" type="button" key={item.report_id} onClick={() => onOpen(item.report_id)}><span><strong>{item.title}</strong><small>{formatDate(item.created_at)} · {item.generation_mode === "ai_enhanced" ? "AI 辅助整理" : "离线基础报告"}</small></span><span className="text-button">查看</span></button>)}</div> : <div className="report-empty-note">暂无报告。</div>}</section>
      </section>
    </div>
  </section>;
}

function ReportDetail({ report, onExport, onOpenAsset }: { report: Report; onExport: (format: "markdown" | "html") => void; onOpenAsset: (assetId: string) => void }) {
  const structured = report.structured_report;
  const evidence = report.evidence_snapshot;
  return <div className="report-detail-stack"><section className="panel report-detail"><div className="panel-title"><div><div className="eyebrow">SAVED REPORT</div><h2>{report.title}</h2></div><span className={`report-mode ${report.generation_mode}`}>{report.report_type === "overview" ? "文件综述" : "数据分析报告"}</span></div><div className="report-detail-meta">创建于 {formatDate(report.created_at)} · {report.generation_mode === "ai_enhanced" ? "AI 辅助整理" : "离线基础报告"}</div><div className="report-block"><h3>执行摘要</h3><p>{structured.executive_summary}</p></div></section><section className="panel report-block"><h3>主要发现</h3>{structured.key_findings.length ? <div className="report-finding-list">{structured.key_findings.map((finding, index) => <article className="report-finding" key={`${finding.statement}-${index}`}><strong>{finding.statement}</strong><ReportEvidenceList ids={finding.evidence_ids} evidence={evidence} onOpenAsset={onOpenAsset} /></article>)}</div> : <div className="report-empty-note">当前没有可验证的关键发现。</div>}</section>{structured.sections.map((section, index) => <details className="panel report-block report-collapsible" key={`${section.heading}-${index}`}><summary><h3>{section.heading}</h3></summary><p className="report-content">{section.content}</p><ReportEvidenceList ids={section.evidence_ids} evidence={evidence} onOpenAsset={onOpenAsset} /></details>)}<details className="panel report-block report-collapsible"><summary><h3>局限与待核实事项</h3></summary>{structured.limitations.length ? <div><h4>局限</h4><ul>{structured.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}{structured.items_to_verify.length ? <div className="report-verify"><h4>待核实事项</h4><ul>{structured.items_to_verify.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}{!structured.limitations.length && !structured.items_to_verify.length ? <div className="report-empty-note">无额外说明。</div> : null}</details>{report.generation_error ? <div className="report-fallback-note" role="status">AI 整理失败，已生成离线基础报告。</div> : null}<div className="report-export-actions"><button className="secondary-button" onClick={() => onExport("markdown")}>导出 Markdown</button><button className="secondary-button" onClick={() => onExport("html")}>导出 HTML</button></div></div>;
}

function ReportEvidenceList({ ids, evidence, onOpenAsset }: { ids: string[]; evidence: ReportEvidence[]; onOpenAsset: (assetId: string) => void }) {
  const values = reportSources(ids, evidence);
  if (!values.length) return null;
  return <div className="report-evidence-list">{values.map((item, index) => <details className="report-evidence-details" key={item.evidence_id}><summary>来源 {index + 1} · {reportEvidenceLabel(item)}</summary><div className="report-evidence">{item.asset_id ? <button className="text-button" onClick={() => onOpenAsset(String(item.asset_id))}>查看来源</button> : null}{item.kind === "sql_result" && item.rows?.length ? <div className="report-sql-summary">已保存 {number(item.row_count ?? item.rows.length)} 行 SQL 结果快照</div> : null}{item.snippet || item.text ? <small>{String(item.snippet || item.text).slice(0, 500)}</small> : null}</div></details>)}</div>;
}

function LegacyReportDetail({ report, onExport, onOpenAsset }: { report: Report; onExport: (format: "markdown" | "html") => void; onOpenAsset: (assetId: string) => void }) {
  const structured = report.structured_report;
  const evidence = report.evidence_snapshot;
  return <div className="report-detail-stack">
    <section className="panel report-detail"><div className="panel-title"><div><div className="eyebrow">SAVED REPORT</div><h2>{report.title}</h2></div><span className={`report-mode ${report.generation_mode}`}>{report.generation_mode === "ai_enhanced" ? "AI 辅助整理" : "基础报告"}</span></div><div className="report-detail-meta">创建于 {formatDate(report.created_at)} · 基于 {report.source_analysis_run_ids.length} 次分析</div><div className="report-block"><h3>分析概述</h3><p>{structured.executive_summary}</p></div></section>
    <section className="panel report-block"><h3>主要发现</h3>{structured.key_findings.length ? <div className="report-finding-list">{structured.key_findings.map((finding, index) => <article className="report-finding" key={`${finding.statement}-${index}`}><strong>{finding.statement}</strong><ReportEvidenceList ids={finding.evidence_ids} evidence={evidence} onOpenAsset={onOpenAsset} /></article>)}</div> : <div className="report-empty-note">当前没有可验证的关键发现。</div>}</section>
    {structured.sections.map((section, index) => <section className="panel report-block" key={`${section.heading}-${index}`}><h3>{section.heading}</h3><p className="report-content">{section.content}</p><ReportEvidenceList ids={section.evidence_ids} evidence={evidence} onOpenAsset={onOpenAsset} /></section>)}
    <section className="panel report-block"><h3>局限与待核实事项</h3>{structured.limitations.length ? <div><h4>局限</h4><ul>{structured.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}{structured.items_to_verify.length ? <div className="report-verify"><h4>待核实事项</h4><ul>{structured.items_to_verify.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}{!structured.limitations.length && !structured.items_to_verify.length ? <div className="report-empty-note">无额外说明。</div> : null}</section>
    {report.generation_error ? <div className="report-fallback-note" role="status">AI 整理失败，已生成基础报告。</div> : null}
    <div className="report-export-actions"><button className="secondary-button" onClick={() => onExport("markdown")}>导出 Markdown</button><button className="secondary-button" onClick={() => onExport("html")}>导出 HTML</button></div>
  </div>;
}

function LegacyReportEvidenceList({ ids, evidence, onOpenAsset }: { ids: string[]; evidence: ReportEvidence[]; onOpenAsset: (assetId: string) => void }) {
  const values = reportSources(ids, evidence);
  if (!values.length) return null;
  return <div className="report-evidence-list">{values.map((item) => <div className="report-evidence" key={item.evidence_id}><span>来源：{reportEvidenceLabel(item)}</span>{item.asset_id ? <button className="text-button" onClick={() => onOpenAsset(String(item.asset_id))}>查看来源</button> : null}{item.kind === "sql_result" && item.rows?.length ? <div className="report-sql-summary">已保存 {number(item.row_count ?? item.rows.length)} 行 SQL 结果快照</div> : null}{item.snippet || item.text ? <small>{String(item.snippet || item.text).slice(0, 500)}</small> : null}</div>)}</div>;
}

export default App;
