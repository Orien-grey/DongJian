export type Page = "overview" | "catalog" | "quality" | "tasks" | "detail";
export type AssetType = "table" | "text";
export type QualityStatus = "ready" | "needs_review" | "unusable";
export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

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
  semantic: Record<string, unknown> | null;
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
  status: TaskStatus;
  progress: number;
  currentStage: string;
  counts: Record<string, number>;
  startedAt: string | null;
  finishedAt: string | null;
  errorSummary: string | null;
  summary: Record<string, unknown> | null;
}

export interface TasksResponse {
  items: Task[];
}
