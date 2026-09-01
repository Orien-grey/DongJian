# Data model

The canonical Python contracts are frozen standard-library dataclasses in
`src/chongzu/assets.py`. They describe real future extraction/catalog rows; the
Architecture Refactor does not create sample business assets.

## Cardinality

`FileAssetSet` encodes the central invariant:

```text
File (one file_id)
  +-- TableAsset[0..N]
  +-- TextAsset[0..N]
         +-- TextChunk[0..N]
```

The two asset collections are independent. It is valid to have neither, either,
or both. `file_id` is not unique in either catalog asset table.

## Shared source rules

- `file_id` points to Registry path identity.
- `content_sha256` is the exact source content identity and is a 64-character
  lower-case hexadecimal digest.
- `extraction_run_id` points to the route/config/timing/error evidence that
  produced the asset.
- `extractor` and `extractor_version` identify the producing implementation.
- Page numbers are one-based; chunk indexes and character offsets are
  zero-based.
- `bbox` is nullable and uses `BoundingBox(x0, y0, x1, y1)` in the extractor's
  documented coordinate system.
- Timestamps are `datetime`; producers should write UTC.

## TableAsset

| Field | Type | Contract |
| --- | --- | --- |
| `table_id` | `str` | Stable provenance-derived ID; never an AI name. |
| `file_id` | `str` | Registry source path identity. |
| `content_sha256` | `str` | Source-content fingerprint. |
| `extraction_run_id` | `str` | Extraction-run lineage link. |
| `extractor` | `str` | Extractor implementation name. |
| `extractor_version` | `str` | Exact extractor version/config family. |
| `source_kind` | `SourceKind` | File, sheet, page, image, slide, or section. |
| `sheet_name` | `str | None` | Workbook sheet when applicable. |
| `page_number` | `int | None` | One-based page/slide page when applicable. |
| `bbox` | `BoundingBox | None` | Source region when available. |
| `row_count` | `int` | Non-negative extracted row count. |
| `column_count` | `int` | Non-negative count matching `columns`. |
| `columns` | `tuple[str, ...]` | Mechanical/raw column labels at this layer. |
| `raw_artifact_path` | `str` | Repository-relative raw payload reference below `workspace/`. |
| `normalized_artifact_path` | `str | None` | Separate normalized payload; never replaces raw. |
| `extraction_confidence` | `float | None` | Extractor confidence in `[0, 1]` when available. |
| `quality_status` | `AssetQualityStatus` | `not_assessed`, `pass`, `review`, or `fail`. |
| `created_at` | `datetime` | Asset creation time. |

Large table rows are Parquet-first. `TableAsset` is catalog metadata and points
to raw/normalized artifacts rather than embedding an unbounded matrix in every
Python object or DuckDB catalog row.

## TextAsset

| Field | Type | Contract |
| --- | --- | --- |
| `text_asset_id` | `str` | Stable provenance-derived ID. |
| `file_id` | `str` | Registry source path identity. |
| `content_sha256` | `str` | Source-content fingerprint. |
| `extraction_run_id` | `str` | Extraction-run lineage link. |
| `extractor` | `str` | Extractor implementation name. |
| `extractor_version` | `str` | Exact extractor version/config family. |
| `source_kind` | `SourceKind` | File/page/image/slide/section origin. |
| `page_number` | `int | None` | One-based source page when applicable. |
| `section` | `str | None` | Section/heading/logical region when available. |
| `bbox` | `BoundingBox | None` | Source region when available. |
| `text` | `str` | Extracted local text. |
| `language` | `str | None` | Detected/declared language when available. |
| `created_at` | `datetime` | Asset creation time. |

## TextChunk

| Field | Type | Contract |
| --- | --- | --- |
| `chunk_id` | `str` | Stable ID from text asset, index, and offsets. |
| `text_asset_id` | `str` | Parent TextAsset. |
| `file_id` | `str` | Direct source lookup without losing parent linkage. |
| `chunk_index` | `int` | Non-negative deterministic order. |
| `text` | `str` | Chunk content. |
| `char_start` | `int` | Inclusive zero-based character offset. |
| `char_end` | `int` | Exclusive zero-based character offset. |
| `provenance` | `ChunkProvenance` | Content SHA, run, extractor/version, source kind/page/section. |

Chunking is deterministic and versioned. Embeddings are not fields on
`TextChunk`; a later index may reference `chunk_id` externally.

## SemanticMetadata

Semantic metadata is a separate AI/human interpretation record, not an asset
mutation.

| Field | Type | Contract |
| --- | --- | --- |
| `asset_id` | `str` | Existing TableAsset or TextAsset ID. |
| `asset_type` | `AssetType` | `table` or `text`. |
| `display_name` | `str` | Human-facing proposed/accepted name. |
| `category` | `str` | Semantic catalog category. |
| `description` | `str` | Asset interpretation. |
| `keywords` | `tuple[str, ...]` | Search/browse terms. |
| `summary` | `str` | Concise semantic summary. |
| `semantic_fields` | mapping | Field meanings, units, aliases, or structured semantic schema. |
| `model` | `str` | Configured model that generated the record. |
| `prompt_version` | `str` | Versioned prompt/response contract. |
| `confidence` | `float | None` | Confidence in `[0, 1]` when supplied/derived. |
| `generated_at` | `datetime` | Generation time. |

Multiple model/prompt/time records may refer to the same asset. Stable asset
IDs do not change when display names or categories change.

## QualityIssue

| Field | Type | Contract |
| --- | --- | --- |
| `issue_id` | `str` | Stable issue identity. |
| `asset_id` | `str` | Affected table or text asset. |
| `severity` | `QualityIssueSeverity` | `info`, `warning`, `error`, or `critical`. |
| `issue_type` | `str` | Machine-readable category. |
| `description` | `str` | Human-readable problem statement. |
| `evidence` | mapping | Coordinates, samples, metrics, or other review evidence. |
| `detected_by` | `str` | Deterministic rule, extractor, model, or reviewer. |
| `suggested_action` | `str` | Non-destructive proposed next action. |
| `status` | `QualityIssueStatus` | `open`, `accepted`, `ignored`, or `resolved`. |

`accepted` means the user accepts the issue/suggestion as valid; it does not
authorize overwriting raw data. A later cleaning operation must have its own
versioned transformation record.

## Stable IDs

`make_table_id()` and `make_text_asset_id()` hash canonical extraction identity:

- file ID and content SHA-256;
- extractor and extractor version;
- source kind and deterministic source locator;
- ordinal within that locator.

Display name, category, description, semantic fields, model, prompt, and
quality-review status are deliberately excluded. `make_chunk_id()` uses its
parent text asset plus deterministic index and offsets.

## Persistence mapping

DuckDB schema v2 maps tuples/mappings/bounding boxes to JSON catalog columns
and stores text directly in the initial contract. Table payload paths reference
Parquet/other raw artifacts below `workspace/output/`. Catalog writes will be
implemented with extractor phases; the current empty tables assert names,
cardinality, and provenance without fake data.

The immutable layer sequence is:

```text
source file -> raw asset/artifact -> normalized artifact -> semantic metadata
```

No downstream layer replaces or deletes the upstream evidence it interprets.
