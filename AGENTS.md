# Repository instructions

These instructions apply to the entire repository.

## Mission and platform boundary

Build a local, Windows-native pipeline for extracting, structuring, cleaning, and profiling roughly 1,500 heterogeneous files from one scientific research project directory.

- The fixed root of this working project is `E:\Desktop\ChongZu`. Launchers should still derive the root from their own location so every internal path is controlled and testable.
- The only supported production platform is Windows x64.
- The prepared system must run without administrator rights and without WSL, Docker, Conda, or a network connection during the core processing workflow.
- Python, Java, native tools, models, and Python packages must live below this repository root. Production launchers must not depend on a system Python, a system Java, `%USERPROFILE%`, or globally configured `PATH` entries.
- If a required project-local runtime or artifact is missing, fail with an actionable message. Never silently fall back to a global executable or user cache.
- Provisioning may use the network only in an explicit future preparation step. Processing must never download packages, models, or artifacts implicitly.

## Fixed technology direction

- CPython 3.11.x: project-local, independently runnable, and ultimately portable.
- Apache Tika: authoritative MIME/type identification plus legacy Office, unknown-format, and parser-failure fallback.
- PyMuPDF: primary extractor for ordinary PDFs with usable text layers.
- Docling: slow path only for scanned PDFs, complex layout, or complex tables. Never route every PDF to Docling.
- `python-calamine`: primary XLS/XLSX reader.
- `openpyxl`: fallback only when formatting, formula, comments, merged-cell, or similar workbook details are actually required.
- RapidOCR with ONNX Runtime: image OCR and necessary scanned-page OCR.
- `python-docx`, `python-pptx`, `lxml`/BeautifulSoup, and standard-library `zipfile`: lightweight native format paths.
- Polars: primary tabular computation engine.
- DuckDB plus Parquet: registry, structured persistence, and querying.
- RapidFuzz and similarly lightweight deterministic algorithms: matching and cleaning.
- LLM support, if ever added, is optional tier-three semantic cleaning. Core execution and correctness must not depend on an LLM.

Do not introduce WSL, Docker, Kubernetes, Spark, Ray, NiFi, NeMo Curator, the full Unstructured stack, Data Prep Kit runtime, or OpenRefine Server.

## Routing contract

Maintain explicit fast/medium/slow routing with recorded reasons:

1. Discover the file, collect metadata, compute a stable SHA-256 fingerprint, and identify its real type. Treat the extension only as a hint.
2. Prefer fast native parsers for Excel, delimited text, JSON, HTML, XML, DOCX, PPTX, and similar formats.
3. Send ordinary text-layer PDFs to PyMuPDF. Images and image-only pages may use RapidOCR.
4. Escalate to Docling only when measurable signals show that a cheaper result is missing or inadequate, such as image-only PDF pages, very low text coverage, complex layout, or complex tables.
5. Use Tika parsing for legacy Office, unknown formats, and failed primary paths. Tika detection remains part of type identification.
6. Normalize every successful extractor result into the canonical text/table representation before profiling, cleaning, and Parquet/DuckDB persistence.

Every escalation must store a machine-readable reason, the attempted route, timings, warnings, and extractor versions.

## Data safety, resilience, and performance

- Never edit source files in place. Treat `workspace/input/` as immutable input.
- Isolate each file's work and errors. A corrupt or unsupported file must be recorded and must not terminate the whole batch.
- Use a persistent per-file state machine and atomic output publication so interrupted runs can resume safely.
- Skip unchanged files using the stable fingerprint together with pipeline/configuration version information.
- Bound concurrency separately for cheap parsing, Java/Tika, OCR, and Docling. Do not create unbounded task or process queues; apply backpressure.
- Record wall time per file and per stage, route decisions, byte/page/row counts, outcome, warning/error category, and output references.
- Keep the fast path cheap. Do not import or initialize heavyweight OCR/Docling stacks in workers that do not need them.
- Prefer streaming or bounded batches and avoid loading arbitrarily large inputs wholly into memory.
- Write derived artifacts only below `workspace/`; keep original evidence and provenance traceable.

## Runtime and cache containment

All mutable or large runtime assets belong in the designated repository directories:

- `runtime/python/`, `runtime/packages/`, `runtime/java/`, `runtime/tika/`
- `cache/uv/`, `cache/pip/`, `cache/huggingface/`, `cache/docling/`, `cache/ocr/`, `cache/tika/`, `cache/temp/`
- `models/ocr/`, `models/docling/`
- `workspace/input/`, `workspace/staging/`, `workspace/output/`, `workspace/quarantine/`, `workspace/state/`, `workspace/logs/`

Future Windows launchers must derive the repository root from their own location and explicitly set applicable cache/model variables before starting Python or Java. At minimum, isolate Python user packages and bytecode behavior, uv/pip caches, Hugging Face caches, Docling artifacts, OCR artifacts, Tika state, Java temporary files, and generic temporary files. Validate each third-party component's supported environment variables before relying on them.

### Portable runtime contract

- `runtime/python/cpython-3.11.15-windows-x86_64-none/python.exe` is the
  production interpreter. Formal launchers and the production CLI must invoke
  it by an absolute path derived from the launcher location.
- `runtime/packages/` is the production package directory. Runtime packages
  are installed there with the project-local `runtime/uv/uv.exe` and pinned
  versions from `uv.lock`; no global or user site-packages are valid inputs.
- `runtime/venv/` is a development environment for pytest and provisioning
  only. **Development venv is not part of the portable runtime contract.** A
  normal Windows venv may retain absolute interpreter metadata and may need to
  be rebuilt after relocation.
- Every future production dependency must be installed and verified in
  `runtime/packages/` as part of the portable bundle, not only in the
  development venv. Launchers must set `PYTHONPATH` to the relocated `src/`
  and `runtime/packages/` directories and must not rely on the caller's PATH.

## Development rules

- Keep format adapters small and behind stable interfaces. Routing, extraction, canonicalization, profiling, cleaning, and persistence must remain separate layers.
- Prefer deterministic, testable rules. Store schema and pipeline versions explicitly.
- Use structured error categories rather than swallowing exceptions or storing only free-form messages.
- Add small synthetic fixtures only; never commit real research inputs, extracted data, credentials, models, runtimes, logs, or generated databases.
- Pin runtime and dependency versions in the future provisioning/lock files. Record licenses and hashes for downloaded binaries and models.
- Tests must cover corrupt files, extension/MIME mismatches, interrupted runs, unchanged-file skipping, parser fallback, and bounded concurrency.
- Phase 0 is documentation and directory scaffolding only. Do not install dependencies, download runtimes/models, or create a UI during this phase.
