# Local Data Catalog (Phase 6 / Phase 7A / Phase 8)

The Catalog is the read model over current extracted assets. It is stored in
the embedded DuckDB file `workspace/state/registry.duckdb`; large table values
remain in Parquet. There is no MySQL service and no network dependency.

Phase 8 exposes this read model through `CatalogService` and `/api/v1/catalog`.
The browser receives bounded metadata and preview windows only; it never sees
DuckDB schema or arbitrary filesystem access. `effective_display_name` remains
the semantic name when available, otherwise the source-derived fallback.

## Schema v5

Phase 6 and Phase 7A add these structures without rewriting extraction or
cleaning history:

| Structure | Purpose |
| --- | --- |
| `cleaning_runs` | Versioned raw identity, cleaner/config identity, status, paths, timings, profile JSON, and errors. |
| `table_profiles` | Columnar table dimensions, null/duplicate/shape/OCR/provenance profile and profile JSON. |
| `text_profiles` | Text size/page/block/chunk/native-OCR/low-content profile and profile JSON. |
| `quality_issues.cleaning_run_id` | Separates deterministic cleaning issues from extraction issues. |
| `semantic_runs` | Historical semantic attempts, cache identity, bounded-input audit metadata, and sanitized errors. |
| `semantic_metadata` history fields | `semantic_run_id`, `input_hash`, and `current`; prior model/prompt results are retained. |
| `quality_issues.semantic_run_id` | Links a semantic review suggestion without changing issue status. |
| `catalog_assets` | Unified view over current TableAssets and TextAssets. |

The migration from schema v3 is additive and idempotent. Existing
`files`, `contents`, `scan_runs`, `extraction_runs`, raw assets, chunks, and
issues remain available. Registry schema changes do not enter extraction
identity, so opening the v5 Catalog does not invalidate raw extraction or
Phase 6 cleaning caches.

## `catalog_assets` view

The view returns one row per current asset with:

```text
asset_id, asset_type, file_id, source_root, content_sha256,
source_file, source_format, extractor, extractor_version, source_kind,
sheet_name, page_number, fallback_display_name, semantic_display_name,
effective_display_name, category,
rows/chars, columns/chunks, quality_status, quality_issue_count,
semantic_status, semantic_model, semantic_confidence, semantic_run_id,
cleaning_status, cleaning_run_id, cleaning_identity,
cleaner, cleaner_version, raw_artifact_path, normalized_artifact_path,
cleaned_normalized_artifact_path, extraction_normalized_artifact_path,
metadata_artifact_path, cleaning_manifest_path, profile_artifact_path,
extraction_run_id, created_at
```

Table and text assets are a union, not mutually exclusive rows. A scanned PDF
page or image may therefore have both a text row and one or more table rows.
`fallback_display_name` is generated only from filename/sheet/page/table
provenance, for example `report.pdf / Page 3 / Table 2`; it is not
`SemanticMetadata` and never affects stable IDs. `semantic_display_name` is
present only for the current successful result, and `effective_display_name`
selects it over the fallback. `semantic_status` is `pending` until an
explicit semantic pass.

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
.\chongzu.cmd semantic status
.\chongzu.cmd semantic enrich --provider fake --asset <asset-id>
```

`summary` reports files, TableAssets, TextAssets, TextChunks, ready/review/
unusable counts, quality issues, and semantic-pending count. `list` supports
only the small type/quality/format/limit filters. `show` includes source and
provenance, metadata, profile, current quality issues, and a bounded preview
(20 table rows or 2,000 text characters by default); it never prints a whole
large Parquet table. `show` also includes semantic history and effective-name
selection. The Fake Provider is an explicit offline test path; the HTTP
provider is disabled in Phase 7A.

## Local API and UI read model

The production UI uses these bounded operations:

```text
GET  /api/v1/overview
GET  /api/v1/catalog?type=&quality=&format=&q=&limit=&offset=
GET  /api/v1/assets/{asset-id}
GET  /api/v1/assets/{asset-id}/table-preview?layer=raw|normalized&limit=&offset=
GET  /api/v1/assets/{asset-id}/text-preview?limit=&offset=
GET  /api/v1/quality/issues?status=&severity=&asset_id=&limit=&offset=
PATCH /api/v1/quality/issues/{issue-id}  {"status":"open|accepted|ignored|resolved"}
```

Table preview is capped at 200 rows and text preview at 20,000 characters.
Quality updates change only the review status in DuckDB; they do not alter
source files or raw/normalized artifacts. The fallback name, provenance,
profile, artifact references, and semantic-pending state are all returned as
separate fields.

## Boundaries

Catalog metadata remains local and queryable. Phase 7A may add Fake Provider
semantic metadata, but no real LLM response, embedding, or vector database is
created. Unsupported files remain registered in `files` with their source
fingerprint but have no asset row. All source and raw/normalized hashes remain
stable across cleaning, semantic validation, and Catalog queries.
