# Local frontend

The Phase 8/9 UI is a compact, Chinese-first desktop catalog for 1366×768 and
larger screens. It uses React `18.3.1`, ReactDOM `18.3.1`, TypeScript
`5.7.3`, Vite `6.0.7`, and `@vitejs/plugin-react` `4.3.4`. There is no UI
component framework, chart library, CDN, remote font, external image, or
browser-side filesystem access.

## Build boundary

`frontend/src` and the package manifests are source-controlled. `frontend/node_modules`
and `frontend/dist` are ignored by Git. A release or relocated product bundle
must include `frontend/dist`; Node/npm/Vite are development-only tools and are
not required to run the product.

On a machine without Node, the development tool may be provisioned as a
project-local portable runtime under `runtime/node-dev`. Its npm cache must be
under `cache/npm`, and no system PATH or user cache is changed:

```powershell
$env:Path = "E:\Desktop\ChongZu\runtime\node-dev;$env:Path"
& E:\Desktop\ChongZu\runtime\node-dev\npm.cmd install --cache E:\Desktop\ChongZu\cache\npm --no-audit --no-fund
& E:\Desktop\ChongZu\runtime\node-dev\npm.cmd run typecheck
& E:\Desktop\ChongZu\runtime\node-dev\npm.cmd run build
```

Vite uses a relative asset base. The finished output contains only local JS,
CSS, and HTML and is served by the Python backend.

## Pages

- **概览**: file/support/quality state, table/text composition, formats, and
  recent tasks.
- **数据目录**: bounded type/quality/format filters and source-name search;
  each row represents one TableAsset or TextAsset.
- **资产详情**: 数据、画像、质量、来源、AI语义 tabs. Tables page raw or
  normalized Parquet; text is a bounded normalized excerpt.
- **质量检查**: open issues with evidence, suggested action, and review-only
  accept/ignore/resolve controls.
- **处理任务**: directory input, asynchronous stage progress, counts, and
  failure summary.
- **数据检索**: explicit lexical search over catalog metadata and TextChunks,
  with type/format/quality filters, bounded snippets, match offsets, and links
  to the originating asset.
- **数据查询**: explicit TableAsset selection, visible `t1`/`t2` relation
  mapping, a plain SQL textarea, bounded results, execution time, and sandbox
  status. SQL is never generated automatically.

The AI semantic tab is intentionally disabled in practice and says that a
model is not configured. Search is lexical-only. Chat, RAG, SQL Agent, Vision,
Embedding, and real model calls are not present.

Search distinguishes three empty states: no query has been entered, a submitted
query has no matching result, and the Catalog has not been populated. A text
result opens its asset detail and retains the page/chunk provenance in the API
contract. The UI highlights match offsets in the returned plain-text snippet;
the backend never returns HTML.

The SQL workbench only sends selected asset IDs and SQL to the local API. It
never sends a filesystem path, opens DuckDB itself, or exposes the Registry
schema. It displays the service-generated relation mapping and a truncation
notice when the 500-row or response-size limit is reached.

## UI/data boundary

The browser knows the stable API DTOs only. It does not know DuckDB tables,
Parquet paths, PyMuPDF, RapidOCR, or img2table. Artifact references are shown
for provenance, but no arbitrary local-file route is provided. A quality
button changes only the issue review status in DuckDB; it never edits source,
raw, normalized, or semantic content.
