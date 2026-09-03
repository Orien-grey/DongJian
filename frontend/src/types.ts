export type Page = "overview" | "catalog" | "search" | "query" | "quality" | "tasks" | "settings" | "detail";
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
  visionMode: VisionMode;
  status: TaskStatus;
  progress: number;
  currentStage: string;
  currentFile: string | null;
  completed: number;
  total: number;
  currentSubstage: string | null;
  elapsedSeconds: number;
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
  scope: "file" | "directory";
  technicalDetail?: string;
}

export interface TasksResponse {
  items: Task[];
}
