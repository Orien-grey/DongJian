# Local API v1

Phases 8, 9, and Vision M2 ship a small localhost API implemented with Python's standard
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
| GET | `/api/v1/settings/ai` | Project-local AI status and editable fields; never returns the key. |
| PUT | `/api/v1/settings/ai` | Atomically write `config/llm.json` from the Settings page. |
| POST | `/api/v1/settings/ai/test` | Explicit connection test; does not run automatically during extraction. |
| GET | `/api/v1/overview` | File, asset, quality, semantic-pending, and format counts. |
| GET | `/api/v1/catalog` | Metadata list with `type`, `quality`, `format`, `q`, `limit`, `offset`. |
| GET | `/api/v1/assets/{asset-id}` | Unified asset metadata, provenance, profile, issues, and semantic history. |
| POST | `/api/v1/assets/{asset-id}/semantic-enrich` | Explicitly enrich exactly one table/text asset; requires configured provider and UI confirmation. |
| GET | `/api/v1/assets/{asset-id}/table-preview` | Bounded Parquet preview with `layer=raw\|normalized`, `limit`, `offset`. |
| GET | `/api/v1/assets/{asset-id}/text-preview` | Bounded text window with `limit`, `offset`. |
| GET | `/api/v1/search` | Offline lexical search with `q`, `type`, `format`, `quality`, `match`, `limit`, `offset`. |
| POST | `/api/v1/analysis/context` | Bounded metadata, schema, samples, profiling, chunks, and provenance for selected assets. |
| POST | `/api/v1/analysis/search` | Thin adapter over the existing local lexical Search service. |
| POST | `/api/v1/analysis/sql` | Thin adapter over the existing bounded read-only SQL service. |
| POST | `/api/v1/query/schema` | Return aliases and bounded schemas for explicitly selected table asset IDs. |
| POST | `/api/v1/query/sql` | Execute one bounded read-only SQL statement over the selected temporary relations. |
| GET | `/api/v1/quality/issues` | Review queue with `status`, `severity`, `asset_id`, `limit`, `offset`. |
| PATCH | `/api/v1/quality/issues/{issue-id}` | Set review status to `open`, `accepted`, `ignored`, or `resolved`. |
| POST | `/api/v1/process` | Validate a directory and queue `{ "source": "..." }`. Returns `202` and `taskId`. |
| GET | `/api/v1/tasks` | Recent in-memory process tasks. |
| GET | `/api/v1/tasks/{task-id}` | One task's status, stage, progress, counts, and error summary. |

Catalog and issue list responses contain `items` and `pagination`. Table
previews are hard capped at 200 rows; text previews are hard capped at 20,000
characters. The service resolves artifact paths only from a Registry asset ID;
there is no `file?path=` endpoint.

## Search

`GET /api/v1/search` is lexical-only. An empty `q` returns zero results and
does not scan the full text store. `type` is `all`, `table`, or `text`; `match`
is `all` or `phrase`. Queries use Unicode NFC, case-insensitive Latin matching,
continuous substring matching for no-space Chinese, and whitespace token
matching when the user supplies spaces. Results contain `resultId`, `assetId`,
optional `chunkId`, display/source/location fields, `matchKind`, a bounded plain
text `snippet`, match offsets, a local ordinal `score`, and complete
provenance. Scores are not relevance probabilities. The live backend is
`duckdb-live-catalog` / `lexical-live-v1`; no DuckDB FTS extension or rebuild is
required. A single asset contributes at most three results.

The endpoint caps `q` at 512 characters, `limit` at 100, and `offset` at
10,000,000. It searches catalog names, source fields, sheet names, columns,
bounded profile samples, semantic metadata when present, and normalized
TextChunks. It does not scan every cell in a large Parquet table.

## Safe SQL

`POST /api/v1/query/schema` accepts `{ "assetIds": ["..."] }` and maps the
selected current TableAssets to `t1`, `t2`, ... in request order. It returns
column names/types and row counts without exposing Parquet paths.

`POST /api/v1/query/sql` accepts the same IDs and a SQL string. The response
contains `columns`, bounded object `rows`, `rowCount`, `truncated`,
`executionMs`, relation mappings, and the sandbox version. The service loads
only selected normalized artifacts with Polars, copies them by parameterized
batch insertion into a fresh in-memory DuckDB connection, and closes the
Registry connection before user SQL executes. This avoids the PyArrow bridge,
which is intentionally not a dependency.

The current limits are at most 8 assets, 50,000 selected input rows, 32 KiB SQL,
500 returned rows, 2 MiB JSON, 512 MiB DuckDB memory, one worker thread, and a
10 second interruptible execution bound. Input above the row limit is rejected
before loading; it is not silently truncated. Only one `SELECT` or `WITH ...
SELECT` statement is accepted.
DDL, DML, `COPY`, `ATTACH`, `INSTALL`, `LOAD`, `PRAGMA`, `SET`, `CALL`,
`VACUUM`, generator functions, external file functions, catalog/system
relations, and unselected relations are rejected. DuckDB also runs with
`enable_external_access=false`; adversarial tests execute real file functions
on a temporary connection and verify that they fail. SQL never receives the
Registry connection and cannot read arbitrary local paths, extensions, or the
network.

## Task model

`POST /process` never runs a subprocess or blocks on extraction. A bounded
single-worker `ProcessTaskManager` calls the existing `process_source()` core
function directly. It receives stage callbacks for `scan`, `extract`, `clean`,
`profile`, and `catalog`; explicit Vision PDF work additionally reports page
substages and current page. It exposes `queued`, `running`, `succeeded`,
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

The process endpoint never invokes semantic enrichment. The single-asset
semantic endpoint is the only API route that may construct the configured
provider, and only for an explicit POST. It sends bounded summary/sample data,
never returns the API key, and maps provider failures to stable codes such as
`SEMANTIC_NOT_CONFIGURED`, `SEMANTIC_TIMEOUT`, `SEMANTIC_UNAVAILABLE`,
`SEMANTIC_AUTH_FAILED`, and `SEMANTIC_INVALID_RESPONSE`. It does not expose
`force` or support bulk enrichment. No vision, embedding, public endpoint,
model download, pip, or uv operation is performed by the local product.

The analysis routes do not add chat, RAG, embeddings, or a vector database.
`context` accepts at most eight explicitly selected TableAsset/TextAsset values;
table context includes schema, row count, sample rows, profiling, and
provenance, while text context includes a bounded text excerpt, chunks, and
provenance. Search and SQL retain their existing local bounds and SQL remains
limited to one read-only `SELECT`/`WITH ... SELECT` statement over selected
temporary relations.
