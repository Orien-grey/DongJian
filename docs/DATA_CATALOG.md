# Phase 6 local Data Catalog

The Catalog is the read model over current extracted assets. It is stored in
the embedded DuckDB file `workspace/state/registry.duckdb`; large table values
remain in Parquet. There is no MySQL service and no network dependency.

## Schema v4

Phase 6 adds these structures without rewriting extraction history:

| Structure | Purpose |
| --- | --- |
| `cleaning_runs` | Versioned raw identity, cleaner/config identity, status, paths, timings, profile JSON, and errors. |
| `table_profiles` | Columnar table dimensions, null/duplicate/shape/OCR/provenance profile and profile JSON. |
| `text_profiles` | Text size/page/block/chunk/native-OCR/low-content profile and profile JSON. |
| `quality_issues.cleaning_run_id` | Separates deterministic cleaning issues from extraction issues. |
| `catalog_assets` | Unified view over current TableAssets and TextAssets. |

The migration from schema v3 is additive and idempotent. Existing
`files`, `contents`, `scan_runs`, `extraction_runs`, raw assets, chunks, and
issues remain available. Registry schema changes do not enter extraction
identity, so opening the v4 Catalog does not invalidate raw extraction caches.

## `catalog_assets` view

The view returns one row per current asset with:

```text
asset_id, asset_type, file_id, source_root, content_sha256,
source_file, source_format, extractor, extractor_version, source_kind,
sheet_name, page_number, fallback_display_name,
rows/chars, columns/chunks, quality_status, quality_issue_count,
semantic_status, cleaning_status, cleaning_run_id, cleaning_identity,
cleaner, cleaner_version, raw_artifact_path, normalized_artifact_path,
cleaned_normalized_artifact_path, extraction_normalized_artifact_path,
metadata_artifact_path, cleaning_manifest_path, profile_artifact_path,
extraction_run_id, created_at
```

Table and text assets are a union, not mutually exclusive rows. A scanned PDF
page or image may therefore have both a text row and one or more table rows.
`fallback_display_name` is generated only from filename/sheet/page/table
provenance, for example `report.pdf / Page 3 / Table 2`; it is not
`SemanticMetadata` and never affects stable IDs. `semantic_status` is
`pending` until a future explicitly authorized semantic pass.

Before cleaning, the view falls back to extraction paths and source-aware
quality state. After a successful cleaning run it exposes the cleaned
normalized path while retaining the extraction normalized path beside it. A
failed cleaning run stays visible with `cleaning_status=failed` and does not
remove the raw path.

## CLI

```text
.\chongzu.cmd catalog summary
.\chongzu.cmd catalog summary --source "D:\Research Data\Project"
.\chongzu.cmd catalog list --type table --quality needs_review --format pdf --limit 20
.\chongzu.cmd catalog list --type text --format png --limit 20
.\chongzu.cmd catalog show <asset-id> --rows 20 --chars 2000
```

`summary` reports files, TableAssets, TextAssets, TextChunks, ready/review/
unusable counts, quality issues, and semantic-pending count. `list` supports
only the small type/quality/format/limit filters. `show` includes source and
provenance, metadata, profile, current quality issues, and a bounded preview
(20 table rows or 2,000 text characters by default); it never prints a whole
large Parquet table.

## Boundaries

Catalog metadata is deterministic and local. No semantic display name,
category, summary, embedding, vector database, or LLM response is created by
Phase 6. Unsupported files remain registered in `files` with their source
fingerprint but have no asset row. All source and raw hashes remain stable
across cleaning and Catalog queries.
