# Roadmap

The roadmap builds a Windows x64 portable research-data workbench around two
independent outputs: table assets and text assets. A phase advances only with
reproducible tests/benchmarks and without weakening source immutability,
provenance, or relocation.

## Phase 0 - Baseline and environment audit

Status: completed 2026-09-01.

- Recorded the Windows/PowerShell/Git/uv/Python/Java environment read-only.
- Established source, test, config, runtime, cache, model, and workspace trees.
- Fixed Windows x64, non-admin, offline-core, and source-immutability rules.
- Added ignore rules so data, runtime payloads, caches, models, logs, and
  generated stores cannot be committed accidentally.

## Phase 1 - Project-local Python foundation

Status: completed 2026-09-01.

- Pinned CPython 3.11.15 and project-local uv.
- Added repository-derived path/environment control and doctor diagnostics.
- Created a development venv and locked minimal DuckDB/pytest dependencies.
- Prevented Conda, PATH Python, user site-packages, and external cache fallback.

## Phase 2 - Discovery, fingerprint, registry, lightweight detection

Status: completed and committed 2026-09-01.

- Added safe recursive discovery, streaming SHA-256, content/path identities,
  incremental scan state, interruption recovery, and bounded hashing.
- Added DuckDB registry tables for scans, files, contents, attempts, and errors.
- Added lightweight magic/container/text detection and isolated corrupt inputs.
- Preserved source files and recorded new/changed/unchanged/missing states.

## Phase 2.5 - Portable runtime contract

Status: completed, verified, and committed 2026-09-01 as
`8507ed3ed2753d98985769e68e3c6f2417879695`.

- Separated the development venv from the production runtime contract.
- Installed the locked DuckDB runtime package in `runtime/packages/` beside
  standalone CPython.
- Added root-derived `doctor.cmd` and `chongzu.cmd` launchers.
- Verified runtime/package/source/cache/registry relocation with a copied
  runnable subset.

## Architecture Refactor - Asset and policy foundation

Status: current working phase; intentionally not committed in this round.

- Reframes the product around independent table and text extraction.
- Defines `TableAsset`, `TextAsset`, `TextChunk`, `SemanticMetadata`,
  `QualityIssue`, stable IDs, and provenance.
- Adds deterministic business-format policy and retained `unsupported` state.
- Adds schema v1-to-v2 migration, policy fields, and empty catalog contract
  tables without fake extraction data.
- Defines provider-neutral Qwen and future search/embedding interface positions.
- Replaces the default Tika/Java/general-parser roadmap with benchmark-driven
  extraction phases.

## Phase 3 - Native structured extraction benchmark

- Benchmark CSV/TSV ingestion, encoding/error behavior, and bounded streaming.
- Benchmark XLS/XLSX with `python-calamine` as the primary reader.
- Use Polars for normalization/profiling and publish raw/normalized Parquet.
- Preserve sheets as independent table sources; one workbook may yield many
  `TableAsset` records.
- Defer `openpyxl` until representative files prove that formatting, formulas,
  comments, merged cells, or similar details are required.

Acceptance: representative structured fixtures/corpus files yield traceable
TableAssets and Parquet with measured accuracy, speed, memory, and failures.

## Phase 4 - PDF native text/table benchmark

- Benchmark PyMuPDF for ordinary text-layer PDF text and layout coordinates.
- Benchmark img2table for native PDF table candidates.
- Measure page text coverage, empty/image-only pages, layout/table signals,
  timing, memory, and extraction confidence on a real sanitized corpus.
- Emit both TableAssets and TextAssets when present.

Acceptance: ordinary PDFs follow measured lightweight paths; inadequate pages
carry explicit evidence for Phase 5/6 escalation.

## Phase 5 - Image and scanned-document extraction

- Benchmark RapidOCR with pinned ONNX Runtime and project-local models.
- Extract page titles, body text, table text, source notes, and annotations from
  JPG/JPEG/PNG, including webpage screenshots.
- Apply OCR only to scanned/image-only PDF pages selected by recorded signals.
- Keep OCR/visual workers lazy, bounded, offline, and independently timed.

Acceptance: images and scanned pages can emit both text and table candidates
with coordinates, confidence, model/version, and source provenance.

## Phase 6 - Complex table benchmark

- Compare img2table, GMFT, and Docling on the actual corpus's difficult tables.
- Score structure fidelity, merged/multi-row headers, false positives, runtime,
  memory, bundle size, portability, and offline artifact requirements.
- Retain only heavy components whose measured benefit justifies their cost.
- Keep GMFT/Docling behind explicit evidence-based escalation gates.

Acceptance: the selected complex-table path has reproducible superiority on
defined cases; no heavy candidate becomes a universal/default route.

## Phase 7 - Qwen semantic enrichment

- Implement a provider adapter for a user-configured OpenAI-compatible base URL.
- Target Qwen3.6-35B-A3B initially while keeping model selection configurable.
- Generate naming, category, descriptions, semantic field mappings, summaries,
  quality suggestions, and optional difficult visual-review results.
- Version prompts, validate structured responses, redact secrets, isolate
  request failures, and prohibit automatic public fallback.
- Never allow model output to overwrite raw or normalized extraction.

Acceptance: semantic metadata and review issues are reproducible/auditable,
optional, and separable from core extraction correctness.

## Phase 8 - Cleaning and Data Catalog

- Implement deterministic Unicode/null/row/column/type/name normalization.
- Add profiling, reversible cleaning operations, issue detection, and human
  accept/modify/ignore/resolve workflows.
- Catalog raw, normalized, and semantic layers in DuckDB while large tables
  remain Parquet-first.
- Reconcile catalog counts and lineage back to source files and extraction runs.

Acceptance: every transformation is traceable and raw assets remain immutable.

## Phase 9 - Local frontend

- Implement Overview, Data Catalog, Asset Detail, Quality Review, and initial
  Search/Analysis surfaces described in `UI_ARCHITECTURE.md`.
- Support raw-versus-extracted comparison and human confirmation of AI advice.
- Keep the frontend local, relocatable, and independent of a database service.

Acceptance: users can inspect provenance and review suggestions without direct
database manipulation or source-file mutation.

## Phase 10 - Search, SQL, and text retrieval

- Add structured catalog selection and validated read-only DuckDB SQL.
- Add deterministic keyword retrieval over TextChunks.
- Evaluate a user-supplied embedding service only after its contract is known;
  add vector retrieval without making it a core correctness dependency.
- Combine retrieved evidence with configured Qwen answers and citations back to
  assets/chunks.

Acceptance: structured and text answers are read-only, evidence-linked, and
safe against unrestricted generated SQL.

## Release - Full offline Windows bundle

- Benchmark the representative approximately 1,500-file project and tune
  bounded queues/resource limits.
- Build and verify the complete Windows x64 portable bundle with integrity and
  license manifests, backup/recovery guidance, and an operator runbook.
- Test relocation with no admin rights, no system Python/Java/Conda/WSL/Docker,
  restricted PATH, clean user caches, and networking disabled for core work.

Acceptance: the copied bundle processes incrementally offline, contains every
required dependency/model locally, isolates failures, preserves provenance,
and writes all mutable state below its relocated root.
