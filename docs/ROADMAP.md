# Roadmap

The phases are ordered to establish containment and incremental correctness before adding expensive document intelligence. A phase is complete only when its acceptance criteria are automated or documented with reproducible evidence.

## Phase 0 - Baseline and environment audit

Status: completed as repository scaffolding on 2026-09-01.

- Record the current Windows, PowerShell, Git, uv, Python, and Java environment without changing it.
- Establish source, test, documentation, runtime, cache, model, and workspace directories.
- Document Windows-only constraints, fixed component boundaries, routing rules, and prohibited platforms/stacks.
- Ignore all real data, downloaded runtimes/models, caches, generated outputs, logs, and large binaries.
- Do not install dependencies, download artifacts, create a UI, or make a Git commit.

Acceptance: the repository contains only baseline documents and placeholders, and Git reports no downloaded runtime, model, cache, or real input payload.

## Phase 1 - Reproducible project-local Python foundation

- Define a minimal `pyproject.toml`, locked dependency groups, package metadata, and command-line entry point.
- Create explicit Windows scripts to provision a pinned CPython 3.11.x distribution into `runtime/python/` and install only small foundational dependencies locally.
- Configure uv/pip/temp caches below `cache/`; disable Python user-site and prevent global interpreter fallback.
- Implement root/path resolution, typed configuration, structured logging, version reporting, and environment diagnostics.
- Add tests proving launch failure is clear when the local runtime is absent and proving execution does not select the PATH Python.
- Record artifact source, version, license, SHA-256, and reproducible provisioning steps.

Acceptance: a non-admin Windows x64 user can prepare and run a small `doctor` command from the project-local Python, with caches contained in the repository. No Java, OCR model, Torch, or Docling is required yet.

## Phase 2 - Discovery, fingerprints, registry, and canonical contracts

- Implement safe recursive discovery, path normalization, source immutability checks, and input policy limits.
- Stream SHA-256 calculation and create DuckDB schemas for runs, files, content identities, attempts, stages, routes, errors, and artifacts.
- Implement the resumable per-file state machine, compatible-result lookup, unchanged-file skipping, and atomic artifact publication.
- Define and version canonical document, text-segment, table/cell, provenance, and error schemas.
- Add bounded queues, backpressure, per-stage timers, and corrupt-file isolation using synthetic fixtures.

Acceptance: interrupted runs resume, unchanged files skip, changed content reprocesses, and one failed file cannot stop a batch.

## Phase 3 - Fast native extraction

- Add text/CSV/TSV, JSON/JSONL, HTML, and XML adapters.
- Add XLS/XLSX extraction with python-calamine and narrowly triggered openpyxl fallback.
- Add DOCX and PPTX adapters with python-docx/python-pptx and safe zip/XML inspection.
- Normalize all results to canonical records and persist bulk data to Parquet with Polars/DuckDB validation.
- Create mismatch, corruption, encoding, oversize, and archive-safety tests.

Acceptance: representative fast-path fixtures process without Java, OCR, or Docling and emit deterministic canonical outputs.

## Phase 4 - PDF medium path

- Add PyMuPDF extraction for metadata, text, pages, blocks, coordinates, links, and selected images.
- Implement measurable PDF quality signals: text coverage, empty/image-only pages, image dominance, block ordering indicators, and table/layout heuristics.
- Define versioned thresholds and explainable reason codes for accepting PyMuPDF output or requesting a later slow path.
- Benchmark time, memory, page throughput, and output quality on synthetic/sanitized fixtures.

Acceptance: normal text-layer PDFs never require Docling and every rejected/low-quality PDF has recorded evidence.

## Phase 5 - Project-local Java and Tika

- Provision a pinned Windows x64 Java runtime and pinned Tika artifact below `runtime/`, with hashes and licenses.
- Add a bounded, supervised Tika lifecycle for true type detection and legacy/unknown/parser-failure fallback.
- Redirect Java/Tika temporary and cache state into project directories and prohibit PATH Java fallback.
- Add extension/MIME mismatch, legacy-format, timeout, crash-restart, and malformed-input tests.

Acceptance: Tika works offline from local Java, cannot escape to a system Java/cache, and a Tika failure remains isolated.

## Phase 6 - OCR medium path

- Provision pinned RapidOCR/ONNX Runtime dependencies and OCR models into controlled local directories.
- Add JPG/PNG extraction and selected scanned-PDF-page OCR, with page batching and independent concurrency/memory limits.
- Record OCR model/version, page selection reason, confidence, timing, and provenance.
- Test rotation, resolution, blank pages, corrupt images, and offline model loading.

Acceptance: image OCR is fully offline, model paths are explicit, and only required pages are rasterized/OCRed.

## Phase 7 - Docling slow path

- Pin and provision Docling, its compatible heavy dependencies, and required artifacts/models locally.
- Implement lazy worker initialization and very low bounded concurrency.
- Enforce routing gates for scanned PDFs, inadequate text coverage, complex layouts/tables, explicit policy, or prior extractor failure.
- Compare slow-path output with PyMuPDF/OCR results and retain the selected result plus decision evidence.
- Test prepared-bundle offline execution and ensure no import or inference step triggers a download.

Acceptance: Docling processes only justified files, every escalation has a stable reason code, and the default PDF path remains PyMuPDF.

## Phase 8 - Profiling, cleaning, and quality analysis

- Implement Polars-based profiling and deterministic normalization.
- Add RapidFuzz-assisted candidate matching with thresholds, review states, and reversible transformation logs.
- Materialize raw canonical, profiled, and cleaned Parquet layers and query them through DuckDB.
- Generate batch quality reports for extraction coverage, failures, duplicates, anomalies, route distribution, and performance.
- Keep any future LLM semantic cleaning optional, isolated, disabled by default, and outside core acceptance criteria.

Acceptance: all transformations are traceable, raw canonical data remains unchanged, and quality reports reconcile to the registry.

## Phase 9 - Performance hardening and portable release

- Benchmark a representative approximately 1,500-file corpus and tune bounded concurrency per workload class.
- Add resource budgets, timeouts, cancellation, recovery drills, and performance regression thresholds.
- Verify a clean prepared copy on Windows x64 with no admin rights, no system Python/Java, restricted PATH, clean user caches, and networking disabled.
- Produce an artifact/license inventory, integrity manifest, backup/restore guidance, and operator runbook.

Acceptance: the full prepared project runs offline and incrementally, unchanged files are skipped, corrupt files do not stop the batch, and all runtime/cache writes remain under the project root.

