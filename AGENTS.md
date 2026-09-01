# Repository instructions

These instructions apply to the entire repository.

## Product mission and platform

ChongZu is a fully relocatable, Windows x64 local workbench for organizing one
scientific research project. Its two extraction jobs are **table extraction**
and **text extraction**. A user-configured OpenAI-compatible service may later
add semantic naming, categorization, field explanations, summaries, and
complex quality judgments. Extraction and deterministic correctness must not
depend on an LLM.

- The fixed development root is `E:\Desktop\ChongZu`. Every launcher and
  internal path must still derive the current root from the launcher's own
  location so a copied bundle re-anchors itself.
- Production supports Windows x64 only, without administrator rights, WSL,
  Docker, Conda, or required system services.
- CPython, uv, Python packages, native tools, and model artifacts must live
  below this repository. Never fall back to a system executable, the caller's
  `PATH`, `%USERPROFILE%`, or a user cache.
- The core workflow is offline. The only future processing-time network target
  is an LLM base URL explicitly configured by the user. There is no public
  endpoint fallback and no implicit package/model download.
- Missing project-local runtime components must produce actionable failures.

## Product processing boundary

The first-stage business formats are CSV, TSV, XLS, XLSX, PDF, JPG, JPEG, PNG,
DOC, DOCX, PPT, PPTX, and TXT.

The registry may discover any file. HTML, CSS, XML, JavaScript, archive
contents, executable/binary unknowns, and other formats outside the explicit
list remain registered but are marked `unsupported`. Do not delete, move, or
turn one unsupported file into a batch failure. A web-page screenshot is an
image and therefore remains supported.

The processing policy independently decides table and text work:

```text
FILE REGISTRY
      |
      v
Processing Policy
      |
      +----------------------+
      |                      |
      v                      v
TABLE EXTRACTION        TEXT EXTRACTION
      |                      |
      v                      v
TableAsset              TextAsset/TextChunk
      |                      |
      +----------+-----------+
                 |
                 v
         Semantic Enrichment
                 |
                 v
            Data Catalog
         DuckDB + Parquet
```

One file may produce independently 0..N `TableAsset` values and 0..N
`TextAsset` values. Never model table versus text as mutually exclusive. PDF,
images, DOC/DOCX, and PPT/PPTX are dual-extraction candidates.

## Canonical contracts and provenance

- Keep file registry, processing policy, extraction, deterministic cleaning,
  semantic enrichment, catalog persistence, and future search as separate
  layers.
- Stable asset IDs derive from extraction provenance, never an AI display
  name. Every table/text asset must reference the source `file_id`, content
  SHA-256, sheet/page/section coordinates as applicable, extractor and version,
  and extraction run.
- Preserve `raw -> normalized -> semantic` boundaries. Normalized data never
  overwrites raw extraction. Semantic metadata is a separate suggestion layer
  and never overwrites either raw or normalized content.
- Publish derived artifacts atomically below `workspace/`. Do not write beside
  or modify source files. `workspace/input/` is immutable evidence.
- Persist per-file and per-stage timings, route/policy reasons, warnings,
  errors, output references, byte/page/row counts, and extractor versions.
- Isolate corrupt and unsupported files. Interrupted work must be resumable,
  and unchanged content must be skippable using content plus pipeline/config
  versions.

## Cleaning and AI boundary

Deterministic cleaning is local code: Unicode normalization, trimming, empty
rows/columns, duplicates, null handling, numeric/date inference, obvious
encoding repair, and mechanical column-name normalization.

Semantic cleaning is future Qwen-assisted review: multi-row headers, actual
field meaning, dataset names/categories, synonymous fields, unit semantics,
related-table judgments, anomaly explanation, and difficult OCR/visual review.
The expected initial model is `Qwen3.6-35B-A3B`, but model selection must be
configuration-driven. The LLM may create `SemanticMetadata`, `QualityIssue`,
or suggested actions only. It cannot directly mutate extracted data.

Do not add embedding fields to core asset contracts. Embedding and vector
retrieval remain future injected interfaces; do not assume an embedding
endpoint or install a vector database/local embedding model.

## Data layer

- Keep the embedded registry/catalog at `workspace/state/registry.duckdb`.
  ChongZu does not require MySQL or a database service process.
- DuckDB holds catalog metadata, provenance, run state, and query state.
  Large table data belongs primarily in Parquet and is queried directly by
  DuckDB.
- The catalog contract includes `files`, `contents`, `scan_runs`,
  `extraction_runs`, `table_assets`, `text_assets`, `text_chunks`,
  `semantic_metadata`, and `quality_issues`. Never populate demo business rows
  in a real registry.

## Runtime containment

- Production Python is
  `runtime/python/cpython-3.11.15-windows-x86_64-none/python.exe`.
- Production packages live in `runtime/packages/` and are installed by the
  project-local `runtime/uv/uv.exe` from pinned `uv.lock` versions.
- `runtime/venv/` is development-only and is not part of the relocatable
  runtime contract.
- Launchers set `PYTHONPATH` to relocated `src/` and `runtime/packages/` and
  explicitly isolate Python/uv/pip/Hugging Face/OCR/temporary state below the
  repository.
- Real inputs, runtimes, packages, caches, models, secrets, logs, databases,
  Parquet, and generated outputs are never committed.

## Technology direction and exclusions

Phase 3 uses pinned project-local python-calamine and Polars wheels for
CSV/TSV/XLS/XLSX and Parquet. Phase 4A uses the pinned PyMuPDF wheel only for
native PDF page/text facts and profiling; it does not perform table extraction
or OCR. img2table remains a Phase 4B candidate, RapidOCR is deferred to
images/scans, and GMFT/Docling remain difficult-table benchmark candidates
rather than default dependencies. Do not add Pandas, NumPy, PyArrow, or
OpenPyXL without representative-corpus evidence.

Apache Tika, a Java runtime, Unstructured, Data Prep Kit, NiFi, NeMo Curator,
OpenRefine runtime/server, WSL, Docker, Kubernetes, Spark, and Ray are not part
of the current plan. Keep the stable Phase 2 lightweight detector for registry
facts and policy decisions; do not add HTML/CSS/XML business extractors.

## Development rules

- Keep adapters small and behind stable interfaces. Avoid importing heavy
  libraries in workers that do not need them.
- Bound cheap parsing, OCR, visual, and future heavy benchmark concurrency
  separately with backpressure.
- Prefer deterministic rules and structured error/status enums.
- Add only small synthetic fixtures. Tests cover corruption, extension/type
  mismatch, interruption, unchanged skipping, unsupported policy, independent
  multi-asset output, provenance, fallback decisions, and bounded concurrency.
- Provisioning is an explicit future operation. Do not install or download
  extraction dependencies/models while doing architecture-only work.
