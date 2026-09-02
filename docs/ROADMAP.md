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

Status: completed and committed 2026-09-01 as
`f5a929b53854669f887729ebf13ee1cf952348a1`.

- Reframes the product around independent table and text extraction.
- Defines `TableAsset`, `TextAsset`, `TextChunk`, `SemanticMetadata`,
  `QualityIssue`, stable IDs, and provenance.
- Adds deterministic business-format policy and retained `unsupported` state.
- Adds ordered schema v1-to-v3 migrations, policy fields, and empty catalog
  contract tables without fake extraction data.
- Defines provider-neutral Qwen and future search/embedding interface positions.
- Replaces the default Tika/Java/general-parser roadmap with benchmark-driven
  extraction phases.

## Phase 3 - Native structured extraction benchmark

Status: implementation and synthetic/relocation acceptance complete; awaiting
the user-selected representative real corpus benchmark before final tuning.

- Benchmark CSV/TSV ingestion, encoding/error behavior, and bounded streaming.
- Benchmark XLS/XLSX with `python-calamine` as the primary reader.
- Use Polars for normalization/profiling and publish raw/normalized Parquet.
- Preserve complete Sheet evidence and conservatively detect 0..N table
  regions; one workbook may yield many `TableAsset` records.
- Defer `openpyxl` until representative files prove that formatting, formulas,
  comments, merged cells, or similar details are required.

Acceptance: representative structured fixtures/corpus files yield traceable
TableAssets and Parquet with measured accuracy, speed, memory, and failures.

## Phase 4A - Native PDF text extraction and profiling

Status: implementation and synthetic/relocation acceptance complete; committed
2026-09-01 as `32df48d4a36849dda52841c23d3e4a9150e1ba82`; awaiting a user-selected
representative PDF corpus.

- Use PyMuPDF to inventory every page and extract native text blocks only.
- Persist page/block bounding boxes, page dimensions/rotation, image counts,
  profile metadata, deterministic TextAssets/TextChunks, and extraction timings.
- Classify PDFs conservatively as `native_text`, `mixed`,
  `suspected_scanned`, or `unknown`.
- Store weak table-candidate hints as quality evidence, never as TableAssets.

Acceptance: native text and scan/mixed signals are reproducible, source hashes
are unchanged, and relocation loads PyMuPDF only from `runtime\\packages`.

## Phase 4B - Native PDF table benchmark

- Status: native-text candidate implementation, synthetic ground-truth scoring,
  and relocation acceptance complete; real-corpus decision pending.
- Compare the Phase 4A profile facts with `img2table==2.0.0` on 30--100
  representative, sanitized PDFs selected by the user.
- Keep native text and table extraction independent: one PDF may publish both
  TextAssets and multiple page-level TableAssets.
- Measure table detection recall, false positives, row/column correctness,
  merged-cell handling, header preservation, borderless tables, speed, memory,
  and runtime-size increase.
- Image-only/suspected-scanned pages are `deferred_to_ocr`; no OCR engine is
  installed or called in this phase.
- Add no heavy document stack until measured corpus evidence requires it.

Acceptance: a measured native candidate produces traceable TableAssets and
explicitly records pages that need a later visual/OCR benchmark. The retention
decision is KEEP (default native route), FALLBACK (simple tables only), or
REMOVE (benefit does not justify runtime cost); no synthetic score alone makes
that decision.

## Phase 4C - Real PDF baseline

Status: baseline run completed on the user-supplied read-only substitute corpus;
human table ground truth remains pending.

- Profile the complete PDF source with Registry, SHA-256, and PyMuPDF native
  extraction before any table candidate run.
- Select a deterministic stratified sample and publish only ignored-by-Git
  manifests, review CSV, provenance, previews, and relevant page renders below
  `workspace/benchmark/pdf-real-v1/`.
- Measure native/mixed/scanned/unknown distribution, candidate distribution,
  img2table throughput, zero/multiple-table counts, and reuse. Do not infer
  KEEP/FALLBACK/REMOVE without human review.

## Phase 5A - RapidOCR local OCR foundation

- Provision RapidOCR 3.9.2 and ONNX Runtime 1.29.0 from Windows wheels, with
  project-local models under `runtime/models/ocr/` and runtime downloads
  disabled.
- Extract page titles, body text, table text, source notes, and annotations from
  JPG/JPEG/PNG, including webpage screenshots.
- Apply OCR only to scanned/image-only PDF pages selected by recorded signals.
- Keep OCR/visual workers lazy, bounded, offline, and independently timed.

Acceptance: images and scanned pages emit traceable OCR TextAssets/TextChunks
and block evidence with coordinates, confidence, model/version, and source
provenance. OCR-to-table integration is implemented in Phase 5B.

## Phase 5B - Image/scanned-PDF dual extraction and unified pipeline

- Status: implementation complete for this round; changes remain intentionally
  uncommitted for review.
- Reuse one RapidOCR pass as the internal `OCRBlock` evidence contract for
  TextAssets and the `img2table==2.0.0` image adapter.
- Route JPG/JPEG/PNG and scanned PDF pages to independent text and table
  outputs. Mixed PDFs remain page-local: native pages use PyMuPDF and scanned
  pages use render plus OCR.
- Publish image/scanned-page tables through the shared `TableAsset` contract,
  apply only conservative mechanical quality signals, and retain weak table
  candidate hints as routing evidence rather than truth.
- Provide `chongzu extract SOURCE`, one-scan orchestration, incremental reuse,
  per-file isolation, offline/network guards, and a unified summary.
- Compare a small selection of Phase 4C rendered pages with the image route for
  count/shape/cell-overlap consistency only. Native candidate output is not
  ground truth and cannot establish accuracy.

Acceptance: synthetic images, mixed/scanned PDFs, unsupported files, relocation,
failure isolation, source SHA invariants, and offline reuse are verified. No
GMFT, Docling, LLM, embedding, or frontend work is included.

## Phase 6 - Deterministic Cleaning and Data Catalog

- Implement deterministic Unicode/null/row/column/type/name normalization.
- Add profiling, reversible cleaning operations, issue detection, and human
  accept/modify/ignore/resolve workflows.
- Catalog raw, normalized, and semantic layers in DuckDB while large tables
  remain Parquet-first.
- Reconcile catalog counts and lineage back to source files and extraction runs.

Acceptance: every transformation is traceable and raw assets remain immutable.

## Phase 7 - LLM Semantic Enrichment

- Implement a provider adapter only for a user-configured OpenAI-compatible
  base URL after explicit configuration and explicit test authorization.
- Keep model selection configuration-driven; Qwen3.6-35B-A3B is the expected
  company service and DeepSeek is development-only when explicitly authorized.
- Generate naming, category, descriptions, semantic field mappings, summaries,
  quality suggestions, and optional difficult visual-review results.
- Version prompts, validate structured responses, redact secrets, isolate
  request failures, and prohibit public endpoint fallback.
- Never allow model output to overwrite raw or normalized extraction.

Acceptance: semantic metadata and review issues are reproducible/auditable,
optional, and separable from core extraction correctness.

## Phase 8 - Local Frontend

- Implement Overview, Data Catalog, Asset Detail, and Quality Review surfaces
  described in `UI_ARCHITECTURE.md`.
- Support raw-versus-extracted comparison and human confirmation of semantic
  advice.
- Keep the frontend local, relocatable, and independent of a database service.

Acceptance: users can inspect provenance and review suggestions without direct
database manipulation or source-file mutation.

## Phase 9 - Search, SQL, and Text Retrieval

- Add structured catalog selection and validated read-only DuckDB SQL.
- Add deterministic keyword retrieval over TextChunks.
- Evaluate a user-supplied embedding service only after its contract is known;
  add vector retrieval without making it a core correctness dependency.
- Combine retrieved evidence with configured semantic answers and citations
  back to assets/chunks.

Acceptance: structured and text answers are read-only, evidence-linked, and
safe against unrestricted generated SQL.

## Phase 10 - End-to-End Acceptance

- Exercise the complete supported-format matrix on the representative project.
- Verify incremental reruns, interruption recovery, failure isolation,
  relocation, source immutability, network policy, license/integrity manifests,
  and operator-facing summaries.
- Freeze release evidence and document known limitations before packaging.

Acceptance: all release gates are reproducible from a clean copied bundle.

## Release - Offline Windows Bundle

- Benchmark the representative approximately 1,500-file project and tune
  bounded queues/resource limits.
- Build and verify the complete Windows x64 portable bundle with integrity and
  license manifests, backup/recovery guidance, and an operator runbook.
- Test relocation with no admin rights, no system Python/Java/Conda/WSL/Docker,
  restricted PATH, clean user caches, and networking disabled for core work.

Acceptance: the copied bundle processes incrementally offline, contains every
required dependency/model locally, isolates failures, preserves provenance,
and writes all mutable state below its relocated root.
