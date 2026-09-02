# Architecture

## Product boundary

ChongZu is a fully relocatable Windows x64 workbench for organizing research
data in one local scientific project. The core has exactly two extraction
responsibilities: tables and text. File discovery, extraction, deterministic
normalization, semantic suggestions, persistence, and future retrieval remain
separate layers.

The source directory is immutable. All mutable state and derived artifacts are
root-relative below `workspace/`; runtime and cache state is root-relative
below `runtime/`, `cache/`, and `models/`. RapidOCR's portable ONNX model
bundle is under `runtime/models/ocr/` beside the package payload.

Phase 8 adds a product shell around these services without changing the data
contracts: a React/Vite build is served from `frontend/dist` by a Python
standard-library `ThreadingHTTPServer` bound to `127.0.0.1`. Browser requests
go through `/api/v1/` and application services; handlers do not contain
catalog SQL or extractor logic.

Phase 9 adds a `RetrievalService` for deterministic lexical search and a
separate `SqlQueryService` for explicitly selected TableAssets. Search reads
current Catalog metadata and bounded TextChunk content; it does not scan all
Parquet cells or install a DuckDB extension. SQL resolves asset IDs through the
Registry, copies only normalized Parquet data into temporary in-memory tables,
and then runs on a fresh DuckDB connection with external access disabled.

## System flow

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
       Deterministic Cleaning
                 |
                 v
          Profiling / Quality
                 |
                 v
            Data Catalog
         DuckDB + Parquet
             /            \\
            v              v
   Local Retrieval       Safe SQL
      Search API        Query API
            \\            /
             v          v
          Local Frontend
                 |
                 v
       Future Semantic Enrichment
```

The arrows from policy to extraction are independent. For every registered
file, table cardinality is 0..N and text cardinality is separately 0..N. A
PDF, image, Word document, or presentation can yield both types. No enum,
database uniqueness rule, worker route, or UI assumption may turn this into an
exclusive table-versus-text choice.

## Registry and processing policy

Phase 2 discovery collects path identity, immutable content SHA-256, stat
metadata, lightweight detected type, extension hint, MIME-like value,
detection evidence, and scan provenance. The extension is a hint, not truth.
The lightweight detector remains intentionally small and does not extract
business content.

`processing_policy.plan_processing()` consumes only Registry facts and emits:

- `support_status` (`supported` or `unsupported`);
- resolved first-stage business format when supported;
- independent table/text candidate booleans;
- possible OCR and visual-processing booleans;
- a machine-readable reason code.

This is deterministic rule code and never invokes an LLM. Unsupported files
stay in `files`, preserve their fingerprint and type evidence, and do not fail
the scan or get deleted/moved.

| Real/detected format | Table candidate | Text candidate | OCR possible | Visual possible |
| --- | --- | --- | --- | --- |
| CSV / TSV | yes | no default pass | no | no |
| XLS / XLSX | yes | no default pass | no | no |
| PDF | yes | yes | yes | yes |
| JPEG / PNG | yes | yes | yes | yes |
| DOC / DOCX | yes | yes | future evidence only | future evidence only |
| PPT / PPTX | yes | yes | future evidence only | future evidence only |
| TXT | no | yes | no | no |
| HTML / CSS / XML / JS | no | no | no | no; unsupported |
| ZIP content / unknown binary / unlisted | no | no | no | no; unsupported |

OLE detection cannot identify legacy Excel/Word/PowerPoint from bytes alone;
the policy uses `.xls`, `.doc`, or `.ppt` as a bounded format hint. Ambiguous
OLE containers remain unsupported until a future benchmark proves a safe
project-local detector. A JPEG/PNG web screenshot remains supported because
its real type is an image, not HTML.

## Extraction contracts

The canonical Python models live in `src/chongzu/assets.py`; the schema mapping
is documented in [DATA_MODEL.md](DATA_MODEL.md).

`TableAsset` records provenance, a zero-based half-open source row/column
range, dimensions and columns, raw/normalized/metadata artifact paths,
extraction confidence, and quality status. Large row data is not copied into
the catalog: Phase 3 stores it in Parquet below `workspace/artifacts/`.

Phase 3 adapters are separated under `src/chongzu/extract/`: strict delimited
validation, Calamine workbook access, conservative table-region detection,
atomic artifact publication, and the bounded registry-backed coordinator.
`extract structured` first performs an incremental scan, then submits at most
twice the bounded worker count. Workers never write DuckDB; the coordinator is
the single catalog writer.

Phase 4A keeps PDF logic in `src/chongzu/extract/pdf/`. The PyMuPDF route opens
one registered PDF at a time, inventories every page, extracts only native text
blocks, and records page dimensions, rotation, image/drawing signals, block
bounding boxes, timings, and a PDF profile. A PDF can therefore produce many
`TextAsset`/`TextChunk` rows while remaining eligible for a future table route;
no PDF page is converted to a `TableAsset` in this phase. The default PDF
coordinator is serial, with a bounded process pool available for explicit
`--workers 2..4`; DuckDB remains a central single writer.

The profile classification is conservative: `native_text` means every page has
effective native text; `mixed` means native and blank/suspected-scanned evidence
coexist; `suspected_scanned` requires image evidence on a majority of pages; and
`unknown` means no native text without enough scan evidence. These are routing
facts, not OCR decisions. Weak `possible_table_candidate` hints are stored as
quality issues/profile evidence only and never create table rows.

`TextAsset` records source relative path, provenance, page/section/bounding box,
normalized extracted text, language, and separate raw/normalized/metadata
artifact paths. `TextChunk` is a deterministic searchable slice with character
offsets and explicit source/extraction provenance. Embeddings are deliberately
absent.

### Phase 4B native PDF table candidate

`src/chongzu/extract/pdf/table_runner.py` is a second, independent coordinator.
It first ensures the Phase 4A PyMuPDF profile exists, then routes only pages
with native-text evidence to the `img2table` candidate with `ocr=None` and
`pdf_text_extraction=True`. `native_text` PDFs use all pages; `mixed` PDFs use
only `pages_with_text`; `suspected_scanned` pages are recorded as
`deferred_to_ocr`; unknown profiles are probed only when a page count is known.
The profile is the single routing source of truth—no second scan heuristic is
implemented.

Every detected page/table pair becomes its own `TableAsset`. Raw and
normalized Parquet use the same artifact contract as CSV/Excel:

```text
workspace/artifacts/tables/<table_id>/
  raw.parquet
  normalized.parquet
  metadata.json
```

The candidate never consumes or replaces TextAssets. A PDF containing a title,
body, table, and footer therefore retains both asset types linked to the same
file/page. Candidate status, OCR-disabled configuration, table index, optional
bbox, row/column shape, source SHA, run ID, and limitations are stored in the
metadata artifact and catalog. `table_id` is provenance-derived and never an AI
display name. Quality issues are emitted only when evidence warrants review;
unmatched/empty/ambiguous tables are not silently discarded.

`benchmark pdf-table` accepts an optional JSON ground truth sidecar and reports
table detection, shape, row/column, and exact/normalized cell scores separately.
Synthetic scores validate the machinery; retention of img2table is deferred to
a 30--100-file sanitized real-corpus benchmark. The candidate may be KEEP,
FALLBACK, or REMOVE after that measurement. OCR, GMFT, Docling, Qwen, and
embedding remain outside this phase.

### Phase 4C real-corpus baseline

`benchmark pdf-real` composes the existing Phase 4A and Phase 4B routes without
changing either extractor. It snapshots source SHA-256 values, profiles the
complete read-only PDF directory, selects a deterministic stratified sample,
and writes only manifest, profile, table-preview, provenance, page-render, and
blank human-review artifacts below `workspace/benchmark/pdf-real-v1/`. It never
copies or writes beside a source PDF. `review.csv` leaves expected counts and
quality fields empty until a human reviewer fills them; no KEEP/FALLBACK/REMOVE
decision is inferred from candidate output.

### Phase 5A offline OCR foundation

`src/chongzu/extract/ocr/` contains a lazy RapidOCR adapter and a bounded
coordinator. Images are one OCR target each; a PDF first reuses/establishes the
Phase 4A profile and sends only pages without reliable native text to OCR. A
native page and a scanned page in one PDF therefore publish separate
`pymupdf-native-text` and `rapidocr-onnx` TextAssets. OCR blocks retain pixel or
PDF-point bounding boxes and per-block confidence in metadata; normalized text
is mechanical NFC/line-ending/control cleanup only. The raw OCR text, normalized
text, metadata, and chunks use the same text artifact contract as native PDF
text.

RapidOCR is local and CPU-oriented. The production bundle pins RapidOCR 3.9.2,
ONNX Runtime 1.29.0, and wheel-only transitive dependencies; three PP-OCR ONNX
files plus a SHA-256 manifest live in `runtime/models/ocr/`. The adapter passes
explicit string model paths (including the Windows OmegaConf compatibility
workaround) and never invokes RapidOCR's download command. Missing or altered
models are a doctor failure. No OCR backend is enabled in the Phase 4B
native-text candidate; image/scanned-page table reconstruction belongs to the
Phase 5B adapter below.

### Phase 5B image/scanned-PDF dual extraction

`src/chongzu/extract/ocr/rapidocr_engine.py` maps engine output immediately to
the stable internal `OCRBlock` contract: text, polygon bbox, confidence,
one-based page or image provenance, block index, extractor, and version. Raw
RapidOCR return objects do not cross into the registry, asset builders, or
table adapter. This keeps a future local OCR replacement behind one boundary.

`img2table==2.0.0` has a RapidOCR backend, but using it directly would run a
second OCR pass. `img2table_adapter.py` instead injects the already-produced
blocks as the library's `OCRData` shape and calls `extract_tables(ocr=None)`.
The same decoded image is used for OCR and table reconstruction. Run warnings
and table metadata record `ocr_reused=true` and
`ocr_backend_calls=0`; any adapter failure is a partial file result so the OCR
TextAsset remains usable.

Standalone images always run both independent branches. PDF routing reuses the
Phase 4A page profile: reliable native pages stay on PyMuPDF plus the native
candidate, while only pages without reliable native text are rendered and sent
through RapidOCR plus the image table adapter. A mixed PDF can therefore have
native TextAssets on page 1, OCR TextAssets and image TableAssets on page 2,
and native assets again on page 3. There is no whole-file OCR fallback.

Image/scanned-page tables use the common `TableAsset`/raw-Parquet/
normalized-Parquet/metadata contract. Mechanical review signals include sparse
OCR, suspicious one-row/one-column shapes, likely column shifts, header loss,
merged-cell evidence, and long-paragraph cells. These signals set quality
status/issues only; they do not silently delete candidate tables. The profile's
`possible_table_candidate` remains a weak heuristic hint and is explicitly
marked `heuristic_hint_not_ground_truth` in profile/metadata evidence.

The extraction coordinator is `extract_unified()` and the expert command is
`chongzu extract SOURCE`. It scans once, invokes the independent structured,
native PDF, native PDF-table, OCR/image-table, and TXT routes as applicable,
then reads current catalog counts. Each route keeps its own identity and reuse
key, so one file failure does not cancel other files or erase an independent
asset type. The formal user workflow is `chongzu process SOURCE`, which calls
that extractor and then the Phase 6 cleaning/profile/catalog coordinator. See
[UNIFIED_EXTRACTION.md](UNIFIED_EXTRACTION.md).

## Phase 8 local application boundary

The production path is:

```text
start.cmd
  -> project-local standalone Python
  -> ThreadingHTTPServer (127.0.0.1:18765)
  -> /api/v1 application services
       -> CatalogService -> Registry + bounded Parquet/text preview
       -> QualityService -> review-only status updates
       -> ProcessTaskManager -> process_source(source)
  -> frontend/dist static React application
```

`BackendApp` is HTTP-independent and is tested in-process. `CatalogService`
owns safe asset-ID-to-artifact resolution, `QualityService` owns the four
review statuses, and `ProcessTaskManager` owns one background process queue.
The core coordinator receives a small `(stage, progress)` callback seam; it
does not import HTTP code. A task exposes coarse `scan`, `extract`, `clean`,
`profile`, and `catalog` events plus final counts instead of streaming per-file
logs.

The API only accepts an existing non-empty directory for processing. It never
accepts a browser-supplied arbitrary file path for reading. Static resolution
is contained below `frontend/dist`, and missing frontend output produces an
actionable `frontend_not_built` error. JSON failures have a request ID while
tracebacks are written to `workspace/logs/server.log`.

Stable table/text/chunk IDs are derived from extraction provenance. Semantic
display names, categories, model responses, and UI edits never participate in
ID generation.

## Extraction runs and lineage

An extraction run must record the source file and content SHA-256, pipeline and
configuration versions, attempted route, route reason, stage timings, warnings,
extractor versions, outcome, and structured error category. Assets refer to the
run that created them.

The minimum lineage chain is:

```text
TableAsset/TextAsset
  -> extraction_run_id
  -> file_id + content_sha256
  -> source_root + relative_path
  -> sheet/page/section/bbox
  -> extractor + extractor_version
```

Raw and normalized paths are stable IDs rather than display names. Each file
is written to a unique temporary sibling and atomically replaced only after a
complete Parquet/JSON write. A failed rerun therefore does not pre-delete the
previous successful artifact. Historical extraction runs remain in DuckDB;
only successfully published assets become current catalog rows.

## Raw, normalized, and semantic layers

The three layers have different authority:

1. **Raw** is the extractor result plus exact provenance.
2. **Normalized** is deterministic mechanical transformation of raw data.
3. **Semantic** is model/human interpretation linked to an asset ID.

Deterministic cleaning includes Unicode normalization, trimming, removing or
flagging empty rows/columns, duplicate-row handling, null normalization,
numeric/date inference, obvious encoding repair, and mechanical column-name
normalization. Every transformation must be versioned and reversible or
reproducible.

Semantic cleaning includes multi-row header interpretation, field meaning,
dataset naming/category, synonym and unit judgments, related-table decisions,
anomaly explanation, and difficult OCR/visual review. It creates
`SemanticMetadata`, `QualityIssue`, or a suggested transformation for review.
It cannot mutate raw or normalized data directly.

### Phase 6 cleaning, profiling, and catalog

`src/chongzu/clean/` is a deterministic post-extraction layer. It reads only
the published raw artifact, writes a new artifact below
`workspace/artifacts/cleaning/`, and records a machine-readable manifest with
the raw identity, cleaner/config/profile versions, column mapping, actions, and
inference hints. Table cleaning is deliberately conservative: NFC/newline/
whitespace mechanics, explicit null tokens, empty row/column removal, safe
column-name deduplication, exact-duplicate marking, and type inference that
keeps leading-zero/long numeric identifiers as strings. Text cleaning only
normalizes Unicode/newlines/control characters/trailing whitespace and
compresses excessive blank lines.

`profile_table()` and `profile_text()` use bounded samples and columnar
operations. They persist null/distinct/type/statistical, OCR/provenance,
duplicate, text-size, page/block/chunk, and low-content facts. Quality status is
`ready`, `needs_review`, or `unusable`; candidate/OCR/PDF provenance is not
treated as accuracy, and `needs_review` assets remain cataloged. A cleaner
failure records `cleaning_status=failed` and an issue while retaining the raw
asset.

Schema v4 added `cleaning_runs`, `table_profiles`, `text_profiles`, and the
`catalog_assets` view. Schema v5 adds `semantic_runs` and the additive
`semantic_metadata` history fields (`semantic_run_id`, `input_hash`, and
`current`), plus semantic issue provenance. The migration is additive and does
not rewrite extraction or cleaning history. The view joins source/provenance,
raw and cleaned paths, profile/quality state, fallback name, and the current
semantic result. Cleaning and semantic reuse each have identities independent
of extraction reuse.

## Semantic provider boundary

`src/chongzu/semantic/` defines `SemanticRequest`/`SemanticResponse`, the
provider protocol, versioned prompts, bounded input builders, strict local
validation, the deterministic Fake Provider, and the standard-library
OpenAI-compatible adapter. The Fake Provider remains the default test path;
the HTTP adapter is available only through explicit authorization (the CLI
flag or the confirmed single-asset API action). There is no vendor-specific
branch: the configured service remains a provider/model choice rather than a
ChongZu dependency or contract type.

The formal configuration source is the project-root `.env`, represented by
`.env.example`; the tracked `config/llm.example.json` is documentation-only
compatibility material. Missing values produce `LLM_STATUS=NOT_CONFIGURED` and
are normal. No endpoint is guessed and no public fallback exists. The adapter
supports bounded timeout/retry, JSON and response-size checks, and sanitized
error categories. Phase 7B's two real requests used synthetic assets only;
normal tests and core processing never contact a live endpoint.

Semantic input contains only controlled summaries of normalized assets:
provenance, profile, quality hints, bounded representative table rows or text
excerpts, and audit counts. File contents are explicitly marked untrusted
reference data. Validated output is metadata/history or an open review
suggestion; it cannot mutate raw/normalized artifacts, physical columns, issue
status, or stable IDs. See [SEMANTIC_ENRICHMENT.md](SEMANTIC_ENRICHMENT.md).

## Embedded data catalog

ChongZu uses the embedded file
`workspace/state/registry.duckdb`; it does not need MySQL or a database service
process. DuckDB stores catalog metadata, provenance, policy and run state, and
future query state. It directly queries large Parquet table artifacts.

Schema v5 preserves Phase 2 `files`, `contents`, `scan_runs`, `file_attempts`,
and `run_errors`, plus the v2/v3 policy/extraction foundation. It adds
structured extraction identity, source ranges, metadata paths, current-asset
state, and real run/table/issue metrics to:

- `extraction_runs`
- `table_assets`
- `text_assets`
- `text_chunks`
- `semantic_metadata`
- `semantic_runs`
- `quality_issues`
- `cleaning_runs`
- `table_profiles`
- `text_profiles`
- `catalog_assets` (view)

The migration writes no synthetic asset rows. It deterministically backfills
policy status for existing file rows. See [REGISTRY.md](REGISTRY.md).

## Resilience and bounded work

- One corrupt, unsupported, or failed file produces a local outcome and does
  not terminate the batch.
- A cleaning failure never deletes or replaces the raw extraction artifact;
  other assets continue and the failed asset remains visible in the Catalog.
- File/run state is durable; interrupted nonterminal work is retryable.
- Unchanged checks incorporate stable content identity plus pipeline/config
  versions before an extractor result may be reused.
- Cheap structured/document parsing, OCR, visual processing, and future heavy
  benchmark workers have separate bounded queues and concurrency.
- Heavy libraries/models are imported and initialized only in workers that
  need them. Processing never downloads missing artifacts.
- Arbitrarily large inputs are streamed or processed in bounded pages/batches.

## Phase 9 local retrieval boundary

`SearchService` implements lexical-only retrieval over the live
`catalog_assets` view, bounded table metadata/profile samples, semantic metadata
when present, and current `text_chunks`. It normalizes queries with Unicode NFC,
uses case-insensitive Latin matching, preserves no-space Chinese substrings,
and treats whitespace-separated input as deterministic tokens. Ranking weights
are centralized in `src/chongzu/search.py`; scores are ordinal local ranking
values, not semantic probabilities. Results retain file, SHA, asset, page/sheet,
chunk, extractor, and extraction-run provenance. Each asset is capped at three
results so a document's chunks cannot flood the result page.

The current backend is `duckdb-live-catalog` (`lexical-live-v1`), not a
materialized full-text index. There is no `INSTALL fts`, `LOAD fts`, embedding,
vector database, query rewrite, rerank, or model call. New Catalog rows become
searchable on the next query without rebuilding extraction or cleaning.

`SqlQueryService` accepts only one bounded `SELECT`/`WITH ... SELECT` statement
and a list of at most eight selected table asset IDs. It exposes those assets as
service-generated `t1`, `t2`, ... temporary relations. The Registry connection
is closed before user SQL runs. `enable_external_access=false`, allowlisted
relations, forbidden-operation scanning, a 32 KiB SQL cap, 500-row response
cap, 2 MiB JSON cap, 512 MiB memory setting, one thread, and a 10 second
interruptible worker bound the query. See [safe SQL](SQL_QUERY.md).

`TextRetriever` and `EmbeddingProvider` remain provider-neutral future
interfaces; Phase 9 uses only the lexical backend. `RetrievalReference` is a
stable evidence object for future semantic/RAG consumers, but no prompt or
model request is created here.

## Removed and benchmark-only routes

Apache Tika and Java are no longer planned default detector/parser paths.
Unstructured, Data Prep Kit, NiFi, NeMo Curator, OpenRefine runtime/server,
WSL, Docker, Kubernetes, Spark, and Ray are outside the product architecture.
HTML/CSS/XML remain discoverable but have no business extractor.

GMFT and Docling are not part of the next phase and are not default
dependencies. They may be reopened only if a later representative complex-table
benchmark demonstrates that the current native/OCR candidate paths are
insufficient and the measured benefit justifies their cost.

## Phase 10 acceptance and release boundary

Phase 10 is release engineering around the deterministic product, not a new
business extraction layer. The acceptance harness creates only synthetic
inputs, starts the production server from a copied bundle, and verifies the
same API journey again after ZIP extraction. It checks the empty registry,
independent table/text assets, unsupported and corrupt-file isolation,
incremental extraction/cleaning reuse, source SHA/size/mtime invariants,
quality review, lexical Search, safe SQL, restart persistence, and lifecycle
edges.

`scripts/build_release.ps1` is an allowlist assembler. Normal runtime needs
standalone CPython, `runtime/packages`, OCR models, `src`, scripts, and
`frontend/dist`; it does not need uv, the development venv, Node, npm, or a
system service. The generated manifest and ZIP hash are build evidence, not
catalog data. Release output, acceptance sources, and mutable workspace state
remain outside Git.
