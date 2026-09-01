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

This phase contains design only. No frontend framework, server, package, or
generated demo data is added.

## Navigation model

```text
Overview
  |
  +-- Data Catalog -- Asset Detail -- Provenance/Data Thread
  |
  +-- Quality Review -- Accept / Modify / Ignore
  |
  +-- Search / Analysis -- Dataset / Text / SQL / Questions
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

The future page offers four modes:

- dataset/table catalog lookup;
- text and TextChunk lookup;
- read-only SQL analysis in DuckDB over catalog/Parquet;
- natural-language questions over selected evidence.

Structured flow:

```text
question -> candidate TableAssets -> validated read-only SQL -> DuckDB -> result
```

Text flow:

```text
question -> keyword/future vector TextChunks -> cited evidence -> Qwen response
```

Generated SQL must be visible, restricted to read-only statements, validated,
time/resource limited, and executed against an explicitly scoped catalog. Text
answers link back to chunk and source provenance. Vector search is hidden or
disabled until an embedding provider has been deliberately configured.

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

## Delivery sequence

Phase 9 should first deliver read-only Overview, Catalog, Asset Detail, and
provenance. Quality decisions come next with durable audit records. Search and
analysis arrive in Phase 10 after extraction/catalog contracts and restricted
query rules are stable.
