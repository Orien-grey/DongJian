import type {
  AssetDetail,
  CatalogResponse,
  Overview,
  QualityResponse,
  SearchResponse,
  SqlQueryResponse,
  SqlSchemaResponse,
  TablePreview,
  Task,
  TasksResponse,
  TextPreview,
} from "./types";

interface ApiErrorBody {
  error?: { code?: string; message?: string; requestId?: string };
}

export class ApiClientError extends Error {
  readonly code: string;
  readonly requestId?: string;

  constructor(code: string, message: string, requestId?: string) {
    super(message);
    this.name = "ApiClientError";
    this.code = code;
    this.requestId = requestId;
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
    );
  }
  return payload as T;
}

export const api = {
  health: () => request<Record<string, unknown>>("/api/v1/health"),
  overview: () => request<Overview>("/api/v1/overview"),
  catalog: (params: URLSearchParams) => request<CatalogResponse>(`/api/v1/catalog?${params.toString()}`),
  search: (params: URLSearchParams) => request<SearchResponse>(`/api/v1/search?${params.toString()}`),
  asset: (assetId: string) => request<AssetDetail>(`/api/v1/assets/${encodeURIComponent(assetId)}`),
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
  process: (source: string) =>
    request<{ taskId: string; task: Task }>("/api/v1/process", {
      method: "POST",
      body: JSON.stringify({ source }),
    }),
  tasks: () => request<TasksResponse>("/api/v1/tasks?limit=20"),
  task: (taskId: string) => request<Task>(`/api/v1/tasks/${encodeURIComponent(taskId)}`),
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
