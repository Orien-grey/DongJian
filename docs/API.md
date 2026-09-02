# Local API v1

Phase 8 ships a small localhost API implemented with Python's standard
library `ThreadingHTTPServer`. It binds to `127.0.0.1` by default on port
`18765`; it is not a network service and does not listen on `0.0.0.0`.

The frontend is the only intended client. The handler validates routing and
JSON, then delegates to catalog, quality, and process services. DuckDB schema,
Parquet internals, and OCR/PDF implementation details are not part of the
browser contract.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/health` | App, portable runtime, registry, and optional LLM status. No key is returned. |
| GET | `/api/v1/overview` | File, asset, quality, semantic-pending, and format counts. |
| GET | `/api/v1/catalog` | Metadata list with `type`, `quality`, `format`, `q`, `limit`, `offset`. |
| GET | `/api/v1/assets/{asset-id}` | Unified asset metadata, provenance, profile, issues, and semantic history. |
| GET | `/api/v1/assets/{asset-id}/table-preview` | Bounded Parquet preview with `layer=raw\|normalized`, `limit`, `offset`. |
| GET | `/api/v1/assets/{asset-id}/text-preview` | Bounded text window with `limit`, `offset`. |
| GET | `/api/v1/quality/issues` | Review queue with `status`, `severity`, `asset_id`, `limit`, `offset`. |
| PATCH | `/api/v1/quality/issues/{issue-id}` | Set review status to `open`, `accepted`, `ignored`, or `resolved`. |
| POST | `/api/v1/process` | Validate a directory and queue `{ "source": "..." }`. Returns `202` and `taskId`. |
| GET | `/api/v1/tasks` | Recent in-memory process tasks. |
| GET | `/api/v1/tasks/{task-id}` | One task's status, stage, progress, counts, and error summary. |

Catalog and issue list responses contain `items` and `pagination`. Table
previews are hard capped at 200 rows; text previews are hard capped at 20,000
characters. The service resolves artifact paths only from a Registry asset ID;
there is no `file?path=` endpoint.

## Task model

`POST /process` never runs a subprocess or blocks on extraction. A bounded
single-worker `ProcessTaskManager` calls the existing `process_source()` core
function directly. It receives stage callbacks for `scan`, `extract`, `clean`,
`profile`, and `catalog`, and exposes `queued`, `running`, `succeeded`,
`failed`, and reserved `cancelled` states. A task failure is isolated from
the files and assets already handled by the core pipeline.

## Errors and safety

Every API error uses:

```json
{
  "error": {
    "code": "asset_not_found",
    "message": "asset was not found",
    "requestId": "req_..."
  }
}
```

Tracebacks are written to `workspace/logs/server.log` and never sent to the
browser. The process endpoint accepts only a non-empty existing directory; it
does not mutate that directory. Static files are contained under
`frontend/dist`, with client-route fallback to its `index.html`. Missing build
output returns `frontend_not_built` rather than attempting a development
server or a download.

The API never invokes semantic enrichment. With the current Phase 7A state,
health and UI report `NOT_CONFIGURED` / `AI semantic: Not configured`, and no
LLM, vision, embedding, public endpoint, model download, pip, or uv operation
is performed.
