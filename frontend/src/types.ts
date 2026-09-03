export type Page = "overview" | "catalog" | "search" | "query" | "quality" | "analysis" | "reports" | "tasks" | "settings" | "detail";
export type AssetType = "table" | "text";
export type QualityStatus = "ready" | "needs_review" | "unusable";
export type TaskStatus = "queued" | "running" | "cancelling" | "succeeded" | "failed" | "cancelled" | "interrupted";
export type VisionMode = "local" | "ai_vision";

export interface Overview {
  files: number;
  supported: number;
  unsupported: number;
  failed: number;
  deferred: number;
  tableAssets: number;
  textAssets: number;
  textChunks: number;
  ready: number;
  needsReview: number;
  unusable: number;
  qualityIssues: number;
  openQualityIssues: number;
  semanticPending: number;
  semanticEnriched: number;
  formats: Record<string, number>;
  timings?: CatalogTimings;
}

export interface CatalogTimings {
  registry_open_ms: number;
  query_ms: number;
  materialize_ms: number;
  serialize_ms: number;
}

export interface HealthResponse {
  app: { name: string; version: string; apiVersion: string };
  portableRuntime: { status: string; projectRoot: string };
  registry: { status: string; path: string };
  server: { pid: number | null; instanceId: string | null };
  llm: {
    status: "CONFIGURED" | "NOT_CONFIGURED" | string;
    configured: boolean;
    enabled: boolean;
    source: "ui" | "env" | "offline" | string;
    apiKeyConfigured: boolean;
    visionEnabled: boolean;
    configPath: string;
    optional: boolean;
    networkCalls: string;
  };
}

export interface AISettings {
  baseUrl: string;
  model: string;
  timeout: number;
  apiKeyConfigured: boolean;
  source: "ui" | "env" | "offline" | string;
  status: string;
  configured: boolean;
  enabled: boolean;
  visionEnabled: boolean;
  configPath: string;
}

export interface AISettingsResponse {
  settings: AISettings;
}

export interface SemanticField {
  source_column: string;
  semantic_name: string;
  description: string;
  semantic_type: string;
  unit: string | null;
  aliases: string[];
  confidence: number;
}

export interface SemanticMetadata {
  display_name: string;
  category: string;
  description: string;
  keywords: string[];
  summary: string;
  semanticFields?: SemanticField[];
  confidence: number;
  model: string;
  prompt_version: string;
  generated_at: string;
  semantic_run_id: string;
  input_hash: string;
}

export interface SemanticEnrichmentResponse {
  assetId: string;
  status: "enriched" | "reused";
  reused: boolean;
  providerCalls: number;
  asset: AssetDetail;
}

export interface AssetSource {
  fileId: string;
  root: string;
  relativePath: string;
  format: string;
  sha256: string;
}

export interface AssetSummary {
  assetId: string;
  assetType: AssetType;
  fallbackDisplayName: string;
  semanticDisplayName: string | null;
  effectiveDisplayName: string;
  source: AssetSource;
  extractor: string;
  extractorVersion: string;
  sourceKind: string;
  sheetName: string | null;
  pageNumber: number | null;
  rows: number | null;
  columns: number | null;
  chars: number | null;
  chunks: number | null;
  qualityStatus: QualityStatus;
  qualityIssueCount: number;
  semanticStatus: "pending" | "enriched";
  semanticModel: string | null;
  semanticConfidence: number | null;
  cleaningStatus: string;
  cleaningRunId: string | null;
  createdAt: string;
}

export interface Pagination {
  limit: number;
  offset: number;
  total: number;
  hasNext: boolean;
}

export interface CatalogResponse {
  items: AssetSummary[];
  pagination: Pagination;
  timings?: CatalogTimings;
}

export interface SearchResult {
  resultId: string;
  assetId: string;
  assetType: AssetType;
  chunkId: string | null;
  displayName: string;
  sourceFile: string;
  sourceFormat: string | null;
  pageNumber: number | null;
  sheetName: string | null;
  matchKind: string;
  snippet: string;
  score: number;
  qualityStatus: QualityStatus;
  matchOffsets: number[][];
  provenance: Record<string, unknown>;
}

export interface SearchResponse {
  query: string;
  total: number;
  limit: number;
  offset: number;
  results: SearchResult[];
  backend: string;
  indexVersion: string;
}

export interface SqlColumn {
  name: string;
  physicalType: string;
}

export interface SqlRelation {
  alias: string;
  assetId: string;
  displayName: string;
  sourceFile: string;
  sourceFormat: string | null;
  columns: SqlColumn[];
  rowCount: number | null;
}

export interface SqlSchemaResponse {
  relations: SqlRelation[];
  limits: {
    maxSelectedAssets: number;
    maxInputRows: number;
    maxResultRows: number;
    maxSqlChars: number;
    timeoutSeconds: number;
  };
  sandbox: string;
}

export interface SqlQueryResponse {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  rowCount: number;
  truncated: boolean;
  executionMs: number;
  relations: SqlRelation[];
  sandbox: string;
}

export interface QualityIssue {
  issue_id: string;
  extraction_run_id: string | null;
  cleaning_run_id: string | null;
  semantic_run_id: string | null;
  asset_id: string;
  severity: "info" | "warning" | "error" | "critical";
  issue_type: string;
  description: string;
  evidence: unknown;
  detected_by: string;
  suggested_action: string;
  status: "open" | "accepted" | "ignored" | "resolved";
  created_at: string;
  asset_type?: AssetType | null;
  effective_display_name?: string | null;
  fallback_display_name?: string | null;
  source_file?: string | null;
  source_format?: string | null;
  sheet_name?: string | null;
  page_number?: number | null;
}

export interface QualityResponse {
  items: QualityIssue[];
  pagination: Pagination;
}

export interface AssetDetail {
  assetId: string;
  assetType: AssetType;
  displayName: string;
  fallbackDisplayName: string;
  semanticDisplayName: string | null;
  qualityStatus: QualityStatus;
  qualityIssueCount: number;
  cleaningStatus: string;
  semanticStatus: "pending" | "enriched";
  source: AssetSource;
  provenance: {
    extractor: string;
    extractorVersion: string;
    sourceKind: string;
    sheetName: string | null;
    pageNumber: number | null;
    extractionRunId: string;
    sourceRange: unknown;
    bbox: unknown;
    renderMetadata?: unknown;
    provider?: string | null;
    providerContract?: string | null;
    model?: string | null;
  };
  artifacts: {
    raw: string | null;
    normalized: string | null;
    extractionNormalized: string | null;
    metadata: string | null;
    cleaningManifest: string | null;
    profile: string | null;
  };
  dimensions: { rows: number | null; columns: number | null; chars: number | null; chunks: number | null };
  extractorMetadata: Record<string, unknown> | null;
  profile: Record<string, unknown> | null;
  qualityIssues: QualityIssue[];
  semantic: SemanticMetadata | null;
  semanticHistory: Array<Record<string, unknown>>;
  createdAt: string;
}

export interface TablePreview {
  assetId: string;
  assetType: "table";
  layer: "raw" | "normalized";
  columns: string[];
  rows: Array<Record<string, unknown>>;
  pagination: Pagination;
}

export interface TextPreview {
  assetId: string;
  assetType: "text";
  layer: "raw" | "normalized";
  text: string;
  offset: number;
  limit: number;
  hasNext: boolean;
  chars: number | null;
  chunks: number | null;
}

export interface Task {
  taskId: string;
  source: string;
  taskType?: "process" | "ai_analysis" | "report_generation" | string;
  visionMode: VisionMode;
  status: TaskStatus;
  progress: number;
  currentStage: string;
  currentFile: string | null;
  currentPage: number | null;
  completed: number;
  total: number;
  currentSubstage: string | null;
  currentStep?: number;
  maxSteps?: number;
  elapsedSeconds: number;
  analysisRunId?: string | null;
  reportId?: string | null;
  counts: Record<string, number>;
  startedAt: string | null;
  finishedAt: string | null;
  errorSummary: string | null;
  error: TaskError | null;
  summary: Record<string, unknown> | null;
}

export interface TaskError {
  code: string;
  stage: string;
  message: string;
  retryable: boolean;
  affectedFile: string | null;
  runId: string | null;
  requestId: string | null;
  scope: "file" | "directory" | "analysis" | "report";
  technicalDetail?: string;
}

export interface TasksResponse {
  items: Task[];
}

export type AnalysisScopeKind = "all" | "selected";
export type AnalysisRunStatus = "running" | "completed" | "insufficient_evidence" | "failed" | "cancelled" | string;
export type AnalysisSupportLevel = "direct" | "inference" | "unconfirmed";

export interface AnalysisScope {
  kind: AnalysisScopeKind;
  asset_ids: string[];
}

export interface AnalysisEvidence {
  evidence_id: string;
  kind: string;
  asset_id?: string;
  asset_ids?: string[];
  asset_type?: AssetType | string;
  chunk_id?: string | null;
  display_name?: string;
  source?: {
    fileId?: string | null;
    relativePath?: string | null;
    format?: string | null;
    sha256?: string | null;
    pageNumber?: number | null;
    sheetName?: string | null;
    [key: string]: unknown;
  };
  provenance?: Record<string, unknown>;
  snippet?: string;
  text?: string;
  columns?: string[];
  rows?: Array<Record<string, unknown>>;
  row_count?: number;
  truncated?: boolean;
  execution_ms?: number;
  sql?: string;
  [key: string]: unknown;
}

export interface AnalysisFinding {
  statement: string;
  evidence_ids: string[];
  support_level: AnalysisSupportLevel;
}

export interface AnalysisStep {
  step: number;
  action: string;
  evidence_ids?: string[];
  asset_ids?: string[];
  query?: string;
  sql?: string;
  result?: unknown;
}

export interface AnalysisRunError {
  code: string;
  message: string;
  retryable?: boolean;
}

export interface AnalysisRun {
  analysis_run_id: string;
  created_at: string;
  finished_at: string | null;
  status: AnalysisRunStatus;
  question: string;
  scope: AnalysisScope;
  scope_asset_ids: string[];
  model_identity: Record<string, unknown>;
  answer: string;
  findings: AnalysisFinding[];
  unverified_findings: AnalysisFinding[];
  limitations: string[];
  grounding_summary: { grounded: number; unverified: number };
  evidence_manifest: Record<string, AnalysisEvidence>;
  executed_safe_sql: Array<Record<string, unknown>>;
  source_asset_ids: string[];
  steps_used: number;
  max_steps: number;
  steps: AnalysisStep[];
  provider_calls: number;
  error: AnalysisRunError | null;
}

export interface AnalysisRunSummary extends Pick<AnalysisRun, "analysis_run_id" | "created_at" | "finished_at" | "status" | "question" | "scope" | "model_identity" | "steps_used" | "source_asset_ids" | "answer" | "error"> {}

export interface AnalysisRunsResponse {
  items: AnalysisRunSummary[];
  limit: number;
}

export interface AnalysisRunResponse {
  run: AnalysisRun;
}

export interface AnalysisStartResponse {
  analysisRunId: string;
  taskId: string;
  task: Task;
}

export interface ReportSection {
  heading: string;
  content: string;
  evidence_ids: string[];
}

export interface ReportFinding {
  statement: string;
  evidence_ids: string[];
}

export interface StructuredReport {
  title: string;
  executive_summary: string;
  sections: ReportSection[];
  key_findings: ReportFinding[];
  limitations: string[];
  items_to_verify: string[];
}

export interface ReportEvidence {
  evidence_id: string;
  kind: string;
  asset_id?: string | null;
  asset_ids?: string[];
  asset_type?: string | null;
  display_name?: string;
  source?: {
    fileId?: string | null;
    relativePath?: string | null;
    format?: string | null;
    sha256?: string | null;
    pageNumber?: number | null;
    sheetName?: string | null;
    [key: string]: unknown;
  };
  snippet?: string;
  text?: string;
  columns?: string[];
  rows?: Array<Record<string, unknown>>;
  row_count?: number;
  truncated?: boolean;
  [key: string]: unknown;
}

export interface Report {
  report_id: string;
  created_at: string;
  updated_at: string;
  title: string;
  purpose: string;
  source_analysis_run_ids: string[];
  generation_mode: "ai_enhanced" | "deterministic_fallback" | string;
  model_identity: Record<string, unknown>;
  structured_report: StructuredReport;
  evidence_snapshot: ReportEvidence[];
  source_asset_ids: string[];
  render_metadata: Record<string, unknown>;
  schema_version: string;
  status: string;
  generation_error: { code: string; message: string; retryable?: boolean } | null;
}

export interface ReportSummary {
  report_id: string;
  created_at: string;
  updated_at: string;
  title: string;
  source_analysis_run_ids: string[];
  generation_mode: string;
  status: string;
  executive_summary: string;
}

export interface ReportsResponse {
  items: ReportSummary[];
  limit: number;
}

export interface ReportStartResponse {
  reportId: string;
  taskId: string;
  task: Task;
}

export interface ReportResponse {
  report: Report;
}
