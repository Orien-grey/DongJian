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
| `source_relative_path` | `str` | Original path relative to the scanned source root. |
| `sheet_name` | `str | None` | Workbook sheet when applicable. |
| `page_number` | `int | None` | One-based page/slide page when applicable. |
| `bbox` | `BoundingBox | None` | Source region when available. |
| `source_row_start`, `source_row_end` | `int` | Zero-based half-open source row range. |
| `source_column_start`, `source_column_end` | `int` | Zero-based half-open source column range. |
| `row_count` | `int` | Non-negative extracted row count. |
| `column_count` | `int` | Non-negative count matching `columns`. |
| `columns` | `tuple[str, ...]` | Mechanical/raw column labels at this layer. |
| `raw_artifact_path` | `str` | Repository-relative raw payload reference below `workspace/`. |
| `normalized_artifact_path` | `str | None` | Separate normalized payload; never replaces raw. |
| `metadata_artifact_path` | `str` | JSON column/row mapping and extraction provenance. |
| `extraction_confidence` | `float | None` | Extractor confidence in `[0, 1]` when available. |
| `quality_status` | `AssetQualityStatus` | `not_assessed`, `pass`, `review`, or `fail`. |
| `created_at` | `datetime` | Asset creation time. |

Large table rows are Parquet-first. Phase 3 and Phase 4B metadata maps Parquet
row zero to a source logical CSV/Sheet row or PDF table row and maps every
normalized column to its original coordinate/header. CSV quoted multiline cells
count as one logical record. Full raw Sheet snapshots are retained separately
from region assets. PDF table coordinates are optional bboxes in PDF points;
when img2table cannot provide a reliable bbox the field remains null and the
limitation is recorded rather than guessed.

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
| `source_relative_path` | `str | None` | Original path relative to the scanned source root when available. |
| `page_number` | `int | None` | One-based source page when applicable. |
| `section` | `str | None` | Section/heading/logical region when available. |
| `bbox` | `BoundingBox | None` | Source region when available. |
| `text` | `str` | Extracted local text. |
| `language` | `str | None` | Detected/declared language when available. |
| `created_at` | `datetime` | Asset creation time. |
| `raw_artifact_path` | `str | None` | Workspace-relative verbatim extractor text artifact. |
| `normalized_artifact_path` | `str | None` | Workspace-relative deterministic normalized text artifact. |
| `metadata_artifact_path` | `str | None` | Workspace-relative block/page provenance metadata. |

Phase 5A/5B OCR TextAssets use `source_kind=image` for standalone images and
`source_kind=page` for rendered scanned PDF pages. Their metadata artifact
stores ordered `OCRBlock` records, pixel/PDF-point coordinate space, and
per-block confidence when available. `OCRBlock` is the stable internal
contract (`text`, `bbox`, `confidence`, page/image, block index, extractor, and
version); RapidOCR's raw return shape is not persisted as a business contract.
The `extractor` value is `rapidocr-onnx`, so these rows remain independent from
native `pymupdf-native-text` rows for the same file/page.

Phase 5B image/scanned-page TableAssets use the same fields and artifact paths
as structured and native PDF candidates. Their extractor is
`img2table-image`, their `source_kind` is `image` or `page`, and their metadata
records the image/PDF bbox, OCR block indexes, `ocr_reused=true`, and
`ocr_backend_calls=0`. A single image/page may therefore publish both one or
more TextAssets and zero or more TableAssets. Candidate quality signals are
stored as `QualityIssue` rows and `quality_status`; they do not replace raw or
normalized Parquet. `possible_table_candidate` is a weak heuristic hint, not
ground truth.

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

## Deterministic cleaning and profiles

Phase 6 keeps the asset contracts independent and adds derived catalog state;
it does not add a second OCR/table contract. Every clean result is keyed by
`asset_id`, source `content_sha256`, the SHA-256 of the raw artifact, cleaner
name/version, configuration version, profile version, and options. The result
is recorded in `cleaning_runs` and writes below:

```text
workspace/artifacts/cleaning/
  tables/<table_id>/<cleaning_identity-prefix>/
    normalized.parquet
    cleaning.json
    profile.json
  text/<text_asset_id>/<cleaning_identity-prefix>/
    normalized.txt
    cleaning.json
    profile.json
```

`cleaning.json` is a machine-readable manifest. It records the raw artifact
identity/path, original-to-mechanical column mapping, normalization actions,
conservative inference hints, and explicit `raw`/`normalized`/`semantic`
layer references. Semantic remains null/pending in this phase. Raw Parquet,
raw text, source files, and extraction rows are never overwritten.

Table profiles in `table_profiles`/`profile.json` contain row/column counts,
null counts/ratios, distinct counts and bounded samples per column, safe
numeric/date statistics, exact duplicate count, empty-cell and long-text
ratios, irregular-width and OCR confidence signals, identifier/constant/high-
cardinality hints, and provenance completeness. Text profiles in
`text_profiles`/`profile.json` contain character/line/page/block/chunk counts,
language hint when available, native/OCR source, OCR confidence, empty/low
content flags, and provenance completeness.

The catalog quality status is deterministic and source-aware:
`ready` means no material deterministic signal was found; `needs_review`
retains an asset with candidate/PDF/OCR or structural uncertainty; and
`unusable` is reserved for an empty/clearly failed result. These statuses are
quality signals, not accuracy claims. Source uncertainty may set
`needs_review` without a generic issue; concrete `QualityIssue` rows carry the evidence
and are linked to `cleaning_run_id` when produced by Phase 6; older extraction
issues remain independently attributable.

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
| `semantic_run_id` | `str | None` | Versioned semantic attempt that produced this record. |
| `input_hash` | `str | None` | Hash of the bounded provider-neutral input envelope. |
| `current` | `bool` | Catalog projection marker; historical records remain stored. |

Multiple model/prompt/time records may refer to the same asset. Stable asset
IDs do not change when display names or categories change.

Phase 7A table output uses strict JSON keys `display_name`, `category`,
`description`, `keywords`, `summary`, `semantic_fields`, and `confidence`.
Each semantic field contains `source_column`, `semantic_name`, `description`,
`semantic_type`, nullable `unit`, `aliases`, and a `[0, 1]` confidence. Text
output uses the same common keys without `semantic_fields`. Optional
`quality_suggestions` are review-only records. These values are never applied
to Parquet columns or text.

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

DuckDB schema v5 maps tuples/mappings/bounding boxes to JSON catalog columns
and stores text directly in the initial contract. Phase 3 writes real table
catalog rows whose payload paths reference Parquet and metadata below
`workspace/artifacts/`. Phase 4A writes real PDF TextAsset/TextChunk rows and
profile JSON under the same workspace root; semantic metadata remains empty and
no demo business data is generated.

The immutable layer sequence is:

```text
source file -> raw asset/artifact -> normalized artifact -> semantic metadata
```

No downstream layer replaces or deletes the upstream evidence it interprets.

Phase 4B/5B `img2table` output is a candidate only. It uses the same
`TableAsset`/Parquet contract as CSV and Excel, while `TextAsset` and
`TextChunk` rows from Phase 4A remain independent for the same PDF/page.

The `catalog_assets` view is the unified read model for current assets. It
exposes `asset_id`, asset/source format, fallback source display name,
extractor/version, rows or chars, columns or chunks, quality status and issue
count, raw/cleaned artifact paths, extraction/cleaning provenance, and
`semantic_status` (`pending` until an explicit semantic enrichment). Fallback
names such as `report.pdf / Page 3 / Table 2` are presentation helpers only;
they are not semantic metadata and never participate in stable IDs.

`semantic_runs` preserves every attempt, model, prompt version, config version,
normalized-artifact identity, input hash, status, bounded-input audit metadata,
warnings, and sanitized failure code. `semantic_metadata.current` identifies
the current successful result; older model/prompt results remain queryable.

## Local retrieval contracts

Phase 9 does not add an embedding or vector field to any asset contract.
`SearchResult` is a read-only result envelope with an asset ID, optional
TextChunk ID, bounded snippet, local ordinal score, location, and complete
source/extraction provenance. `RetrievalReference` is the corresponding future
evidence envelope. Search results never become new assets and never alter
normalized artifacts.

The SQL workbench also creates no persistent asset table. It resolves selected
TableAsset IDs and exposes temporary aliases in a private in-memory connection
for the duration of one query. Raw/normalized paths, registry tables, and
source files remain outside the user SQL scope.
