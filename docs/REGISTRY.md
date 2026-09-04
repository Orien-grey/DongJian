# Registry and embedded catalog

The source of truth is the embedded DuckDB file
`workspace/state/registry.duckdb`. ChongZu does not use MySQL and does not need
a database service process. Scans write only below `workspace/state/` and
`workspace/logs/`; source directories are read-only.

## Schema version 5

`registry_meta` stores `chongzu_file_registry = 5`. Opening schema v1, v2, v3,
or v4 performs ordered, restartable migrations through v5. A database
newer than the supported version is rejected. No silent destructive rebuild is
allowed.

The migration:

- preserves every Phase 2 row and table;
- adds deterministic processing-policy fields to `files`;
- backfills those fields from stored detector/extension facts;
- creates empty catalog contract tables and indexes;
- adds cleaning/profile tables and the unified `catalog_assets` view;
- adds semantic run history and current-result fields without replacing older
  semantic records;
- does not create demo/synthetic extraction rows.

DuckDB does not permit the required ALTER and backfill on the same old table in
one explicit transaction. DDL steps are therefore idempotent and restartable;
policy backfill and the schema-version update commit atomically, so an
interrupted partial migration continues to advertise v1 until it can finish.

## Phase 2 registry tables

| Table | Purpose |
| --- | --- |
| `scan_runs` | Run identity, source root, timestamps/status, counts, timings, log path, and pipeline/schema versions. |
| `files` | Stable path identity, stat/fingerprint, detected type, current presence/error, policy support, candidate routes, and run references. |
| `contents` | One row per stable content SHA-256 with size and first/last seen run. |
| `file_attempts` | Per-scan file observation, hash reuse, before/after stats, detector evidence, error, and timing. |
| `run_errors` | Discovery/run errors that are not a successful file observation. |
| `registry_meta` | Schema name/version metadata. |

Path identity and content identity remain separate. `file_id` is deterministic
for `(source_root, relative_path)`; `contents.sha256` is immutable content
identity. Duplicate paths retain distinct file rows and share a content digest.

## Processing policy fields in `files`

| Field | Meaning |
| --- | --- |
| `support_status` | `supported` or `unsupported` business processing status. |
| `business_format` | Resolved supported format, or null when unsupported/ambiguous. |
| `table_candidate` | Policy says a table extraction branch should be attempted. |
| `text_candidate` | Policy says a text extraction branch should be attempted. |
| `may_require_ocr` | OCR may be required after measurable evidence. |
| `may_require_visual_processing` | Visual/layout processing may be required. |
| `policy_reason` | Stable machine-readable explanation of the decision. |

These are independent booleans. PDF/image/DOC/DOCX/PPT/PPTX can be both table
and text candidates. Unsupported files stay present with their SHA-256 and
detection evidence; they are not moved/deleted and do not fail the scan.

## Catalog and structured extraction state

| Table | Purpose |
| --- | --- |
| `extraction_runs` | Content/extraction identity, source, force flag, route/config, timings, extractor versions, counts, outcome, and structured error. |
| `table_assets` | Current/historical TableAsset metadata, source row/column range, extractor/run, dimensions, artifact paths, confidence, and quality state. |
| `text_assets` | Current/historical TextAsset content, source/extractor/run provenance, and raw/normalized/metadata artifact paths. |
| `text_chunks` | Searchable deterministic chunks with offsets and provenance JSON. |
| `semantic_metadata` | Separate model-generated names/categories/descriptions/fields/summaries with model/prompt/time/confidence. |
| `semantic_runs` | Every semantic attempt, cache identity, model/prompt/config, input audit counts, status, and sanitized error. |
| `quality_issues` | Deterministic/AI/human issues and `open/accepted/ignored/resolved` review status. |
| `cleaning_runs` | Independent cleaning identity, raw artifact identity, status, output paths, timings, and errors. |
| `table_profiles` | Bounded deterministic table profile and quality signals for a cleaning run. |
| `text_profiles` | Deterministic text size/source/confidence profile for a cleaning run. |
| `catalog_assets` | View joining current table/text assets, source/provenance, cleaning, quality, and semantic state. |

One `file_id` is intentionally non-unique in both asset tables. There can be
zero, one, or many table rows and independently zero, one, or many text rows.
Semantic metadata has its own versioned identity tuple and cannot replace an
asset row.

Schema v5 extends `semantic_metadata` with `semantic_run_id`, `input_hash`, and
`current`; `quality_issues.semantic_run_id` identifies review-only semantic
suggestions. `catalog_assets` joins only the current successful semantic result
and exposes fallback/effective display names. Historical results remain in
DuckDB. Semantic migrations are additive and do not enter extraction or
cleaning identities.

Bulk table cells will be Parquet-first. DuckDB stores catalog metadata,
provenance, processing/query state, and directly queries Parquet rather than
duplicating every large table cell in catalog rows.

Phase 5B image/scanned-page rows use the same catalog tables as every other
route. `img2table-image` TableAssets are current independently from
`rapidocr-onnx` TextAssets; both can reference the same file and page. OCR
warnings record the stable `OCRBlock` contract and whether OCR was reused by
the table adapter. No OCR-specific table schema is introduced.

## Incremental scan semantics

The first scan streams SHA-256 for every readable regular file. Later scans may
reuse a digest only when path, size, and `mtime_ns` match; `--rehash` disables
this metadata fast candidate. A before/after stat guards against files changing
during hashing. Missing paths remain in `files` with
`current_presence_state='missing'`.

`scan_runs.status='complete'` means the coordinator finalized the run and may
still have isolated failed file attempts. An interrupted open run is marked
`interrupted` on the next scan of that source. Registry write failure is the
condition that can fail a whole run.

Structured extraction reuse requires exact path-instance `file_id`, content
SHA-256, extractor name and version, structured configuration version, the
stable extraction identity schema, and business format.
PDF native-text reuse uses the same identity discipline plus
`pdf-native-text-v1` and `text-chunk-v1`. A successful or partial PDF run stores
its profile JSON path and profile payload in `extraction_runs.warnings_json`;
current TextAsset rows remain independently queryable. Missing text/profile
artifacts invalidate reuse. A failed rerun records the error and leaves prior
successful artifacts available for audit.
PDF table reuse is a separate identity (`content_sha256` plus
`img2table-candidate` version, `pdf-table-v1` configuration, and the stable
extraction identity schema). Its `phase4b-pdf-table-candidate` /
`pdf_table_candidate` run can be `successful`, `partial`,
`deferred_to_ocr`, or `failed`; table assets are current independently from
TextAssets. The runner consumes the Phase 4A profile instead of recalculating
PDF class heuristics. A table-version/force rerun therefore does not invalidate
a valid PyMuPDF text run, and a failed candidate does not pre-delete prior
artifacts.
Missing artifacts invalidate reuse. `--force` bypasses reuse. A changed file or
changed extractor/config version creates another run; reuse never depends on a
filename or AI-generated display name.

The formal `extract SOURCE` coordinator shares one scan result with all
applicable routes. Structured, native PDF, native PDF-table, OCR/image-table,
and TXT identities remain separate, so reuse is stage-local and a failure in
one route does not erase an independent asset type. A unified summary reports
file counts plus current catalog TableAsset/TextAsset/TextChunk and issue
counts.

Cleaning reuse is independent from every extraction route. Its identity uses
the asset ID, source SHA-256, raw artifact hash, cleaner/version, configuration
and profile versions, and cleaning options. A cleaner version bump therefore
creates a new `cleaning_runs` result without invalidating a valid OCR, PDF, or
structured extraction run. `catalog_assets` selects the latest cleaning result
for the current asset/content pair; previous runs remain in history and raw
artifacts remain intact.

Semantic reuse is independent from extraction and cleaning. Its identity uses
asset/type, normalized-artifact identity, provider, model, prompt version,
semantic config version, and the bounded input hash. The input hash covers the
bounded request envelope. Model or prompt changes create a semantic run only. A failed or
invalid semantic response leaves assets, artifacts, and prior issue statuses
intact.

## Lightweight detection versus business support

The detector reads bounded headers, extension hints, and OOXML ZIP central
directories without extracting archive members. It recognizes PDF, JPEG, PNG,
ZIP, OOXML XLSX/DOCX/PPTX, OLE, HTML/XML/text, CSS, Zone.Identifier, and unknown
binaries. Extension/type mismatches are retained as evidence.

Detection is not business support. The rule policy maps CSV, TSV, XLS, XLSX,
PDF, JPG/JPEG, PNG, DOCX, and TXT into supported work. DOC, PPTX, and PPT are
retained as deferred formats for a later extraction stage.
HTML/CSS/XML/JS, ZIP contents, ambiguous/unknown binaries, and unlisted formats
are registered as unsupported. Apache Tika/Java is no longer a planned default
detector or fallback; the Phase 2 lightweight detector remains the Registry
input until benchmark evidence justifies a narrowly scoped addition.

The product Catalog aggregates child assets under the existing `files.file_id`;
it does not create a second file identity. Page, sheet, paragraph, and table
coordinates remain on the underlying asset and chunk provenance.

## Source safety

Discovery does not follow symlink/reparse-point directory traversal. Hashing
and detection open sources for reads only. No rename, move, copy, delete,
timestamp update, archive expansion, or sidecar write is performed below the
source root. Derived data, state, and logs stay below `workspace/`.
