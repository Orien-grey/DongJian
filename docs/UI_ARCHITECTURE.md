# Local UI architecture

## Goal and references

The future local frontend makes Registry, extracted assets, provenance,
cleaning suggestions, and analysis understandable without exposing filesystem
or database internals. It borrows interaction ideas—not runtime dependencies—
from:

- Microsoft Data Formulator: Data Catalog, Data Thread, and AI data workflow;
- Rill: dataset profiling and dataset explorer;
- OpenRefine: cleaning suggestions with explicit human confirmation;
- DuckDB UI: table browsing and SQL exploration.

Phase 8 implements the local read-only product shell described here. The
production bundle is a self-contained React/Vite build served by the Python
localhost API; no CDN, remote font, external image, or generated demo data is
used.

## Navigation model

```text
Overview
  |
  +-- Data Catalog -- Asset Detail -- Provenance/Data Thread
  |
  +-- Quality Review -- Accept / Modify / Ignore
  |
  +-- Data Retrieval -- lexical local search over metadata/TextChunks
  |
  +-- Data Query -- selected-table read-only SQL
```

TableAsset and TextAsset are peers in one catalog. The UI must never imply that
a source file has exactly one asset or exactly one extraction type.

## Overview

The landing page presents operational and catalog state:

- total file count;
- processed and pending files;
- unsupported and failed files as distinct states;
- TableAsset count;
- TextAsset count;
- open/total QualityIssue count.

Secondary views group by format, category, run status, extraction policy, and
quality severity. Unsupported means known but outside the business scope;
failed means an attempted operation did not complete. They must not be merged.

## Data Catalog

The catalog lists TableAssets and TextAssets in one filterable surface. Each
row/card shows:

- AI/human display name, falling back to a deterministic source label;
- asset type and category;
- source filename plus sheet/page/section;
- table rows/columns or text/chunk size;
- quality status and open issue count;
- extraction/semantic confidence when available;
- extractor and last-updated/run context.

Filters include asset type, supported source format, category, quality,
confidence, extractor, source file, and semantic-review state. One source file
may expand to multiple table and text children.

## Asset Detail

The primary interaction is a synchronized comparison:

```text
Original source/evidence | Extracted result
```

The source pane displays the applicable original page/image/sheet/slide region
without changing it. Selection in either pane highlights corresponding page,
bbox, row/column, or character evidence when coordinates exist.

For a table asset, tabs eventually include:

- raw table;
- normalized table;
- profile and quality issues;
- semantic fields, display name, category, units, and descriptions;
- provenance and extraction-run diagnostics.

For a text asset, tabs include:

- original extracted text;
- deterministic chunks with offsets;
- semantic summary/keywords/category;
- quality issues;
- provenance and extraction-run diagnostics.

Raw is always visibly distinct from normalized and semantic. Editing a display
name or accepting a suggestion cannot silently alter raw content.

## Provenance and Data Thread

Every detail view exposes a compact lineage thread:

```text
source path + SHA-256
  -> scan run and detection/policy
  -> extraction run and extractor/version
  -> raw asset
  -> normalized artifact/version
  -> semantic model/prompt record
  -> review decision
```

Users can inspect timings, warnings, failure/retry history, route reason,
sheet/page/section/bbox, and artifact references. IDs remain accessible for
audit but are not used as the primary human label.

## Quality Review

The review queue follows the useful part of the OpenRefine model: suggestions
are explicit and reversible. Each item shows issue severity/type, evidence,
detector/model identity, confidence, affected raw/normalized preview, and
suggested action.

Primary controls are:

- **Accept**: mark the suggestion valid and schedule/record a separate
  deterministic transformation if one is required;
- **Modify**: edit the proposed interpretation/action before accepting it;
- **Ignore**: retain the issue/evidence with ignored status;
- **Resolve**: close an issue after a verified downstream action.

The status mapping is `open`, `accepted`, `ignored`, and `resolved`. Bulk review
requires a preview, affected-asset count, and rollback/reproduction metadata.
AI is never authorized to click/execute acceptance on behalf of the user.

## Search and Analysis

The delivered Phase 9 pages provide two local modes:

- dataset/table catalog lookup;
- text and TextChunk lookup;
- read-only SQL analysis in DuckDB over catalog/Parquet;
- Future natural-language questions over selected evidence (not implemented).

Structured flow:

```text
selected TableAssets -> validated read-only SQL -> private DuckDB -> result
```

Text flow:

```text
query -> lexical TextChunks -> cited evidence -> asset detail
```

Generated SQL is not implemented. User-entered SQL is visible, restricted to
read-only statements, validated, time/resource limited, and executed against
explicitly selected temporary relations. Search results link back to chunk and
source provenance. Vector search remains absent until a future retrieval
backend is deliberately implemented.

## Local application boundaries

The eventual frontend communicates with a local application layer that owns
DuckDB connections and filesystem access. Browser code never receives API keys,
arbitrary local paths, or unrestricted SQL/file-write capability. DuckDB remains
embedded and single-process ownership/concurrency is coordinated by the local
backend; there is no database server requirement.

All UI state, exports, thumbnails, and caches stay below the relocated project
root. The frontend must start through a root-derived Windows launcher and work
without admin rights or external CDNs. If the configured Qwen service is
offline, extraction/catalog browsing and deterministic review remain usable.

## Phase 8 and Phase 9 delivered surface

The first product navigation is **概览**, **数据目录**, **质量检查**, and
**处理任务**. Asset detail is opened from the catalog and provides 数据、画像、
质量、来源、AI语义 tabs. The data tab requests only bounded normalized/raw
table pages or a bounded normalized text excerpt. The source tab displays
provenance and artifact references rather than serving arbitrary local files.

The process dialog accepts a pasted existing directory path. A POST creates a
background task; the browser polls it once per second and never blocks on the
pipeline. Empty registry/catalog/quality/task states are explicit empty
states. Quality controls update only `open`, `accepted`, `ignored`, or
`resolved` review status. The AI tab reports the configured provider state. It
offers a single-asset AI整理 action only when configured, displays a concise
confirmation before sending a bounded summary/sample, and renders the
validated semantic metadata after success. Phase 7B acceptance used synthetic
assets; core processing and all ordinary read paths remain offline.

Phase 9 adds **数据检索** and **数据查询** to the navigation. Data retrieval
uses the local lexical Search API and displays bounded plain-text snippets,
match kind, quality, source location, and an asset-detail link. Data Query
requires explicit table selection, shows the service-generated `t1`/`t2`
relation mapping, and submits only the SQL text plus asset IDs. Its result
table is bounded and reports truncation, execution time, and sandbox identity.
There is no query rewrite, SQL generation, embedding, Chat, RAG, or model call.

## Delivery sequence

Phase 9 adds deterministic keyword/text retrieval and scoped read-only SQL.
Semantic acceptance remains an explicitly authorized separate operation and
does not gate the local frontend.
