# Registry and embedded catalog

The source of truth is the embedded DuckDB file
`workspace/state/registry.duckdb`. ChongZu does not use MySQL and does not need
a database service process. Scans write only below `workspace/state/` and
`workspace/logs/`; source directories are read-only.

## Schema version 3

`registry_meta` stores `chongzu_file_registry = 3`. Opening schema v1 or v2
performs the ordered, restartable v1-to-v2 and v2-to-v3 migrations. A database
newer than the supported version is rejected. No silent destructive rebuild is
allowed.

The migration:

- preserves every Phase 2 row and table;
- adds deterministic processing-policy fields to `files`;
- backfills those fields from stored detector/extension facts;
- creates empty catalog contract tables and indexes;
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
| `quality_issues` | Deterministic/AI/human issues and `open/accepted/ignored/resolved` review status. |

One `file_id` is intentionally non-unique in both asset tables. There can be
zero, one, or many table rows and independently zero, one, or many text rows.
Semantic metadata has its own versioned identity tuple and cannot replace an
asset row.

Bulk table cells will be Parquet-first. DuckDB stores catalog metadata,
provenance, processing/query state, and directly queries Parquet rather than
duplicating every large table cell in catalog rows.

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
SHA-256, extractor name and version, structured configuration version, schema
version, and business format.
PDF native-text reuse uses the same identity discipline plus
`pdf-native-text-v1` and `text-chunk-v1`. A successful or partial PDF run stores
its profile JSON path and profile payload in `extraction_runs.warnings_json`;
current TextAsset rows remain independently queryable. Missing text/profile
artifacts invalidate reuse. A failed rerun records the error and leaves prior
successful artifacts available for audit.
Missing artifacts invalidate reuse. `--force` bypasses reuse. A changed file or
changed extractor/config version creates another run; reuse never depends on a
filename or AI-generated display name.

## Lightweight detection versus business support

The detector reads bounded headers, extension hints, and OOXML ZIP central
directories without extracting archive members. It recognizes PDF, JPEG, PNG,
ZIP, OOXML XLSX/DOCX/PPTX, OLE, HTML/XML/text, CSS, Zone.Identifier, and unknown
binaries. Extension/type mismatches are retained as evidence.

Detection is not business support. The rule policy maps only CSV, TSV, XLS,
XLSX, PDF, JPG/JPEG, PNG, DOC, DOCX, PPT, PPTX, and TXT into supported work.
HTML/CSS/XML/JS, ZIP contents, ambiguous/unknown binaries, and unlisted formats
are registered as unsupported. Apache Tika/Java is no longer a planned default
detector or fallback; the Phase 2 lightweight detector remains the Registry
input until benchmark evidence justifies a narrowly scoped addition.

## Source safety

Discovery does not follow symlink/reparse-point directory traversal. Hashing
and detection open sources for reads only. No rename, move, copy, delete,
timestamp update, archive expansion, or sidecar write is performed below the
source root. Derived data, state, and logs stay below `workspace/`.
