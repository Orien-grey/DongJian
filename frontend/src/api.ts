import type {
  AnalysisRunResponse,
  AnalysisRunsResponse,
  AnalysisStartResponse,
  ReportResponse,
  ReportsResponse,
  ReportStartResponse,
  AssetDetail,
  AIConnectionTestResponse,
  AISettingsResponse,
  CatalogResponse,
  FileDetail,
  FileContentResponse,
  FileSearchResponse,
  FileInsight,
  FileInsightBulkResponse,
  FileInsightQueueSummary,
  FileCatalogResponse,
  HealthResponse,
  Overview,
  QualityResponse,
  SearchResponse,
  SemanticEnrichmentResponse,
  SqlQueryResponse,
  SqlSchemaResponse,
  TablePreview,
  Task,
  TasksResponse,
  WorkspaceSnapshot,
  TextPreview,
} from "./types";

interface ApiErrorBody {
  error?: { code?: string; message?: string; retryable?: boolean; requestId?: string; diagnostic?: string; category?: string; stage?: string; details?: Record<string, unknown> };
}

export class ApiClientError extends Error {
  readonly code: string;
  readonly requestId?: string;
  readonly retryable: boolean;
  readonly diagnostic?: string;
  readonly category?: string;
  readonly stage?: string;
  readonly details?: Record<string, unknown>;

  constructor(code: string, message: string, requestId?: string, retryable = false, diagnostic?: string, category?: string, stage?: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiClientError";
    this.code = code;
    this.requestId = requestId;
    this.retryable = retryable;
    this.diagnostic = diagnostic;
    this.category = category;
    this.stage = stage;
    this.details = details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const payload = (await response.json().catch(() => ({}))) as ApiErrorBody & T;
  if (!response.ok) {
    const error = payload.error;
    throw new ApiClientError(
      error?.code ?? "http_error",
      error?.message ?? `请求失败（${response.status}）`,
      error?.requestId,
      error?.retryable ?? false,
      error?.diagnostic,
      error?.category,
      error?.stage,
      error?.details,
    );
  }
  return payload as T;
}

export const api = {
  health: () => request<HealthResponse>("/api/v1/health"),
  aiSettings: () => request<AISettingsResponse>("/api/v1/settings/ai"),
  saveAiSettings: (value: { baseUrl: string; apiKey?: string; clearApiKey?: boolean; model: string; timeout: number; visionEnabled: boolean; fileInsightEnabled?: boolean }) =>
    request<AISettingsResponse & { saved: boolean }>("/api/v1/settings/ai", {
      method: "PUT",
      body: JSON.stringify(value),
    }),
  testAiConnection: (value: { baseUrl: string; apiKey?: string; clearApiKey?: boolean; model: string; timeout: number; visionEnabled: boolean }) =>
    request<AIConnectionTestResponse>("/api/v1/settings/ai/test", {
      method: "POST",
      body: JSON.stringify(value),
    }),
  overview: () => request<Overview>("/api/v1/overview"),
  workspaceSnapshot: () => request<WorkspaceSnapshot>("/api/v1/workspace/snapshot"),
  catalog: (params: URLSearchParams) => request<CatalogResponse>(`/api/v1/catalog?${params.toString()}`),
  files: (params: URLSearchParams) => request<FileCatalogResponse>(`/api/v1/catalog?${params.toString()}`),
  search: (params: URLSearchParams) => request<SearchResponse>(`/api/v1/search?${params.toString()}`),
  asset: (assetId: string) => request<AssetDetail>(`/api/v1/assets/${encodeURIComponent(assetId)}`),
  file: (fileId: string, signal?: AbortSignal) => request<FileDetail>(`/api/v1/files/${encodeURIComponent(fileId)}`, { signal }),
  fileContent: (fileId: string, locator: { page?: number; sheet?: string } = {}, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (locator.page != null) params.set("page", String(locator.page));
    if (locator.sheet) params.set("sheet", locator.sheet);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return request<FileContentResponse>(`/api/v1/files/${encodeURIComponent(fileId)}/content${suffix}`, { signal });
  },
  fileSearch: (fileId: string, query: string, signal?: AbortSignal, offset = 0) => request<FileSearchResponse>(`/api/v1/files/${encodeURIComponent(fileId)}/search?q=${encodeURIComponent(query)}&limit=100&offset=${offset}`, { signal }),
  fileInsight: (fileId: string, signal?: AbortSignal) => request<FileInsight>(`/api/v1/files/${encodeURIComponent(fileId)}/insight`, { signal }),
  queueFileInsight: (fileId: string, force = false) => request<{ fileId: string; taskId?: string; task?: Task; status?: string }>(`/api/v1/files/${encodeURIComponent(fileId)}/insight`, { method: "POST", body: JSON.stringify({ force }) }),
  fileInsightQueue: () => request<FileInsightQueueSummary>("/api/v1/file-insights/queue"),
  reprocessFile: (fileId: string) => request<{ fileId: string; taskId: string; task: Task }>(`/api/v1/files/${encodeURIComponent(fileId)}/reprocess`, { method: "POST", body: JSON.stringify({}) }),
  bulkFileInsights: (confirmed = true) => request<FileInsightBulkResponse>("/api/v1/file-insights/bulk", { method: "POST", body: JSON.stringify({ confirmed }) }),
  resetWorkspace: (confirmation: string) =>
    request<{ accepted: boolean; restarting: boolean; requestId: string; statusUrl: string }>("/api/v1/workspace/reset", {
      method: "POST",
      body: JSON.stringify({ confirmation }),
    }),
  resetStatus: (requestId: string) => request<{ request_id: string; phase: string; started_at: string; finished_at: string | null; result: "running" | "succeeded" | "failed"; safe_error: string | null; completed_phases?: string[] }>(`/api/v1/workspace/reset?requestId=${encodeURIComponent(requestId)}`),
  semanticEnrich: (assetId: string) =>
    request<SemanticEnrichmentResponse>(`/api/v1/assets/${encodeURIComponent(assetId)}/semantic-enrich`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  tablePreview: (assetId: string, layer: "raw" | "normalized", limit: number, offset: number) =>
    request<TablePreview>(
      `/api/v1/assets/${encodeURIComponent(assetId)}/table-preview?layer=${layer}&limit=${limit}&offset=${offset}`,
    ),
  textPreview: (assetId: string, limit: number, offset: number) =>
    request<TextPreview>(
      `/api/v1/assets/${encodeURIComponent(assetId)}/text-preview?limit=${limit}&offset=${offset}`,
    ),
  quality: (params: URLSearchParams) => request<QualityResponse>(`/api/v1/quality/issues?${params.toString()}`),
  updateQuality: (issueId: string, status: string) =>
    request<{ item: unknown }>(`/api/v1/quality/issues/${encodeURIComponent(issueId)}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  process: (source: string, visionMode: "local" | "ai_vision" = "local", autoFileInsight = false) =>
    request<{ taskId: string; task: Task }>("/api/v1/process", {
      method: "POST",
      body: JSON.stringify({ source, visionMode, autoFileInsight }),
    }),
  tasks: () => request<TasksResponse>("/api/v1/tasks?limit=20"),
  task: (taskId: string) => request<Task>(`/api/v1/tasks/${encodeURIComponent(taskId)}`),
  cancelTask: (taskId: string) =>
    request<{ task: Task }>(`/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  analysisStart: (question: string, scope: "all" | "selected", assetIds: string[]) =>
    request<AnalysisStartResponse>("/api/v1/analysis/runs", {
      method: "POST",
      body: JSON.stringify({ question, scope, assetIds }),
    }),
  analysisRuns: (limit = 20) => request<AnalysisRunsResponse>(`/api/v1/analysis/runs?limit=${limit}`),
  analysisRun: (runId: string) => request<AnalysisRunResponse>(`/api/v1/analysis/runs/${encodeURIComponent(runId)}`),
  reports: (limit = 50) => request<ReportsResponse>(`/api/v1/reports?limit=${limit}`),
  report: (reportId: string) => request<ReportResponse>(`/api/v1/reports/${encodeURIComponent(reportId)}`),
  reportStart: (fileIds: string[], title: string, purpose: string, reportType: "overview" | "analysis", selectAllCurrentFilter = false, filters: Record<string, string> = {}) =>
    request<ReportStartResponse>("/api/v1/reports", {
      method: "POST",
      body: JSON.stringify({ fileIds, title, purpose, reportType, selectAllCurrentFilter, filters }),
    }),
  reportExport: async (reportId: string, format: "markdown" | "html"): Promise<{ content: string; contentType: string }> => {
    const response = await fetch(`/api/v1/reports/${encodeURIComponent(reportId)}/export?format=${format}`);
    if (!response.ok) {
      const payload = (await response.json().catch(() => ({}))) as ApiErrorBody;
      const error = payload.error;
      throw new ApiClientError(
        error?.code ?? "http_error",
        error?.message ?? `请求失败（${response.status}）`,
        error?.requestId,
        error?.retryable ?? false,
        error?.diagnostic,
        error?.category,
      );
    }
    return { content: await response.text(), contentType: response.headers.get("Content-Type") ?? "text/plain; charset=utf-8" };
  },
  querySchema: (assetIds: string[]) =>
    request<SqlSchemaResponse>("/api/v1/query/schema", {
      method: "POST",
      body: JSON.stringify({ assetIds }),
    }),
  querySql: (assetIds: string[], sql: string) =>
    request<SqlQueryResponse>("/api/v1/query/sql", {
      method: "POST",
      body: JSON.stringify({ assetIds, sql }),
    }),
};
