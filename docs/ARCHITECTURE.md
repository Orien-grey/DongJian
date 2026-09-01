# Architecture

## 1. System objective

The system processes one local scientific-project directory of roughly 1,500 heterogeneous files on Windows x64. It optimizes for throughput without sacrificing provenance, restartability, or extraction quality. The key design rule is to use the cheapest parser that can produce an adequate result and to make every more expensive escalation explainable.

The processing core must operate offline after preparation. All executable runtimes, native libraries, models, and mutable caches are rooted inside the project directory.

## 2. Logical pipeline

```text
immutable input
    -> discovery + metadata
    -> SHA-256 fingerprint + incremental registry lookup
    -> actual type detection
    -> fast / medium / slow route
    -> canonical text + tables + provenance
    -> profiling
    -> deterministic cleaning
    -> Parquet + DuckDB + quality reports
```

Each arrow is a recorded stage with start/end time, status, warnings, and structured failure details. The coordinator commits a file result only after required artifacts have been written successfully.

## 3. Discovery, fingerprints, and incremental identity

Discovery walks an explicitly supplied source root (for example,
`python -m chongzu scan "E:\some\data"`) without modifying it and records at least:

- normalized absolute source path and source-root-relative path;
- byte size and filesystem timestamps;
- SHA-256 content digest;
- observed extension and detected MIME/type;
- pipeline version, configuration hash, and relevant extractor/model versions.

Size and modification time may be used as a cheap candidate check, but SHA-256 is the authoritative content identity. The Phase 2 registry reuses a
previous digest only when the same path instance is still present and its size
and `mtime_ns` match; `--rehash` forces a streaming hash. A stat-before/stat-
after mismatch is recorded as `changed_during_scan` and is never accepted as a
stable digest. Missing paths remain historical rows. Rename handling can later
reuse content-derived artifacts while preserving a new source-path observation.

Discovery does not follow directory symlinks or Windows junction/reparse
points, keeps a visited-directory guard, and records permission, broken-link,
and filesystem errors per entry. A single error cannot terminate the batch.

The coordinator uses a bounded `ThreadPoolExecutor` for stat, streaming hash,
and lightweight detection work. It submits only a small multiple of the worker
count and performs all DuckDB writes on the coordinator connection. Per-file
hash/detection timings and run-level stage timings are persisted.

Suggested file state transitions are `discovered -> identified -> routed -> extracting -> canonicalized -> profiled -> cleaned -> persisted -> complete`, with terminal per-attempt states such as `unsupported`, `failed`, and `quarantined`. Interrupted nonterminal states are eligible for safe retry.

## 4. Lightweight type identification (Phase 2)

Extensions are hints, never ground truth. Phase 2 uses only standard-library
signature/header checks and ZIP central-directory inspection. It recognizes
PDF, JPEG, PNG, ZIP, OOXML XLSX/DOCX/PPTX, OLE compound storage, basic
HTML/XML/text, CSS, and Zone.Identifier metadata. It records the observed
extension, detected type, MIME-like type, method, confidence, evidence, and a
conservative provisional routing class. A `.下载` file is classified by its
bytes when the magic/container is clear; uncertain binary remains `unknown`.

Corrupt ZIP inspection is isolated as `corrupted_zip`. No archive member is
extracted in this phase.

Apache Tika is intentionally not installed yet. In Phase 5 it will be a
project-local Java service for authoritative MIME detection and legacy/unknown
or parser-failure fallback, with the same bounded and auditable lifecycle.

Detection output includes detected MIME, confidence or evidence where available, extension mismatch, container/encryption indicators, and the selected route. A password-protected, unsupported, truncated, or suspicious file is recorded as a per-file outcome rather than aborting the batch.

## 5. Routing tiers

### 5.1 Fast path

Fast adapters avoid heavyweight model initialization and should handle the majority of files.

| Input family | Primary implementation | Fallback/escalation signal |
| --- | --- | --- |
| CSV/TSV and delimited text | streaming decoding plus Polars | ambiguous encoding/dialect, malformed structure, or resource limit |
| JSON/JSONL | standard library/streamed normalization, then Polars where tabular | invalid/truncated content or excessive nesting |
| HTML | lxml/BeautifulSoup | malformed or unsupported embedded content |
| XML | lxml with safe parser settings | malformed, huge, or schema-specific content |
| TXT/Markdown/log-like text | bounded decoding with recorded encoding | low decode confidence or binary mismatch |
| XLS/XLSX | python-calamine | required styles, formulas, comments, merged-cell semantics, or unsupported workbook feature |
| XLSX detail fallback | openpyxl, read-only/data-only modes where appropriate | corrupt/encrypted workbook or unsupported feature |
| DOCX | python-docx plus zip/XML inspection where needed | corrupt package, embedded legacy object, or layout needs outside scope |
| PPTX | python-pptx plus zip/XML inspection where needed | corrupt package, embedded legacy object, or unsupported content |

The fast path returns text segments, tables, metadata, warnings, and quality signals. It does not attempt visual reconstruction when that is unnecessary for downstream research analysis.

### 5.2 Medium path

The medium path is more expensive but remains deterministic and locally bounded.

- **Ordinary PDF:** PyMuPDF extracts metadata, page text, blocks, coordinates, links, and images as required. It is always attempted before Docling for a normal PDF.
- **Images and selected scanned pages:** RapidOCR with ONNX Runtime handles JPG/PNG and page images that genuinely need OCR. OCR workers are separately bounded because memory and CPU cost differ from text parsing.
- **Legacy/unknown/failure fallback:** Tika parsing handles legacy Office formats, unusual containers, unknown formats, or primary-parser failures when its detected type supports extraction.

PDF assessment records page count, text characters and coverage per page, image dominance, empty-page ratio, extraction exceptions, and table/layout indicators. These signals determine whether the PyMuPDF result is adequate.

### 5.3 Slow path

Docling is opt-in by routing evidence, never the default PDF parser. Valid escalation reasons include:

- a PDF is image-only or has materially inadequate text coverage;
- page reading order is demonstrably unusable for the configured quality threshold;
- complex multi-column layout requires layout reconstruction;
- complex tables cannot be represented adequately by the cheaper path;
- an explicit project rule requests high-fidelity processing for a known document class;
- cheaper extractors failed and Docling supports the detected input.

The registry stores a stable reason code such as `pdf_image_only`, `pdf_low_text_coverage`, `complex_layout`, `complex_table`, `policy_override`, or `primary_extract_failed`, plus supporting metrics. Docling concurrency and memory budgets are independent and normally much lower than fast-path concurrency. Required artifacts must be pre-provisioned under project-local model/cache directories; processing cannot download them.

## 6. Canonical representation

All extractors emit a shared logical model rather than format-specific downstream objects.

### Document record

- stable document/content identifiers and source observation;
- source path, size, timestamps, SHA-256, detected MIME, and extension mismatch;
- selected route, attempted extractors, version information, and escalation reason;
- processing status, warnings/errors, stage timings, and output references;
- aggregate quality metrics such as text coverage, OCR use, and table counts.

### Text segment

- document identifier, segment identifier, and deterministic order;
- text, page/sheet/slide/section locator, block type, and optional coordinates;
- language/encoding evidence where measured;
- extractor, confidence/quality indicators, and provenance.

### Table and cell data

- document/table identifiers and source locator;
- table title/caption where available;
- stable row/column order, normalized column names, inferred logical types, and raw values;
- optional cell coordinates, formula/format/merged-cell metadata only when requested;
- extraction method, warnings, and confidence/quality indicators.

Large normalized records are written as Parquet datasets. DuckDB holds the processing registry and queries Parquet; large document bodies should not be duplicated unnecessarily inside the registry database. Schemas are versioned and migrations are explicit.

## 7. Profiling and cleaning

Profiling follows canonicalization and records, without changing raw evidence:

- null/blank rates, row and distinct counts, type consistency, numeric ranges, and date distributions;
- encoding/language indicators and abnormal text/control characters;
- duplicate and near-duplicate candidates;
- column-name similarity and candidate entity/value matches using RapidFuzz or deterministic rules;
- extraction-quality and route-specific warnings.

Cleaning produces a new derived layer with a transformation log. It never overwrites the source or uncleaned canonical representation. Tier one is exact deterministic normalization; tier two is bounded fuzzy/rule-based matching; a future tier-three LLM step is optional, separately enabled, fully auditable, and never required for the core pipeline.

## 8. Persistence, recovery, and idempotency

DuckDB stores run, file observation, content identity, attempt, stage timing, route decision, error, and artifact metadata. Parquet stores bulk canonical/profiling/cleaned datasets. Writes use staging paths followed by atomic publication where Windows filesystem semantics permit.

Each file is an isolation boundary. An exception is categorized, recorded, and the batch continues. Retry policies are bounded and distinguish deterministic parse errors from transient process failures. Re-running after interruption reconciles staged artifacts with registry state and never assumes a partially written output is complete.

## 9. Concurrency and resource control

The coordinator uses bounded queues and backpressure. Configuration exposes separate limits for:

- filesystem hashing and cheap parsing;
- PyMuPDF workers;
- the Tika/Java service or client requests;
- OCR workers and page batching;
- Docling workers;
- persistence writers.

Defaults must be conservative on unknown hardware. Never submit all files or pages to an unbounded executor. Enforce per-file size/page limits, bounded in-memory batches, timeouts, and graceful worker termination. Heavy components are initialized lazily only in their assigned workers.

## 10. Observability and explainability

For every run and file, capture:

- discovery, hashing, detection, extraction, canonicalization, profiling, cleaning, and persistence durations;
- selected tier/adapter and all attempted fallbacks;
- escalation reason code and evidence metrics;
- input/output counts such as bytes, pages, sheets, rows, text characters, tables, and OCR pages;
- success, skip, unsupported, warning, and categorized failure outcomes;
- peak-resource measurements where practical and component versions.

Logs are structured and stored under `workspace/logs/`; operational state belongs under `workspace/state/`. User-facing summaries must distinguish extraction failure from low-quality success.

## 11. Runtime and cache containment

Production launchers invoke explicit executables below `runtime/`, construct a minimal environment, and set all supported cache/model/temp locations below the repository. Python user-site loading is disabled. Java/Tika temporary state is redirected locally. Hugging Face, Docling, OCR, uv, and pip caches are explicitly configured and tested with a clean user profile and an intentionally restricted global `PATH`.

Offline acceptance testing must prove that a prepared copy can process fixtures with network access disabled and without system Python or Java. Any component that attempts an implicit download fails the acceptance test.

## 12. Security and data governance

- Do not execute macros, embedded programs, or active document content.
- Use safe XML parsing and bounded archive expansion; defend against zip bombs and path traversal.
- Treat filenames and document content as untrusted input.
- Do not send research data to remote services.
- Do not place secrets, real inputs, derived research outputs, runtime binaries, or models in Git.
- Record tool/model licenses and verified artifact hashes during future provisioning.
