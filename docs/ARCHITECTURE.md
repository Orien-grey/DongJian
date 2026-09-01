# Architecture

## Product boundary

ChongZu is a fully relocatable Windows x64 workbench for organizing research
data in one local scientific project. The core has exactly two extraction
responsibilities: tables and text. File discovery, extraction, deterministic
normalization, semantic suggestions, persistence, and future retrieval remain
separate layers.

The source directory is immutable. All mutable state and derived artifacts are
root-relative below `workspace/`; runtime and cache state is root-relative
below `runtime/`, `cache/`, and `models/`.

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
         Semantic Enrichment
            Qwen API
                 |
         +-------+---------+
         |       |         |
       naming category semantic schema
         |       |         |
         +-------+---------+
                 |
                 v
            Data Catalog
                 |
          DuckDB + Parquet
                 |
                 v
            Future Search
         SQL + Text Retrieval
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

## Semantic provider boundary

`src/chongzu/semantic.py` defines a provider-neutral configuration and
`SemanticEnrichmentProvider` protocol. The expected first model is
Qwen3.6-35B-A3B, but the model is a config value, not a hard-coded exclusive
choice. `config/llm.example.json` contains no credential. Real LLM config files
are ignored by Git.

No network client is implemented in this refactor. A future adapter may contact
only the base URL the user explicitly configures, with no automatic public
fallback. It must record model, prompt version, confidence, generation time,
request/result status, and enough non-secret evidence for audit. API keys must
never enter logs, DuckDB, prompt captures, or Git.

## Embedded data catalog

ChongZu uses the embedded file
`workspace/state/registry.duckdb`; it does not need MySQL or a database service
process. DuckDB stores catalog metadata, provenance, policy and run state, and
future query state. It directly queries large Parquet table artifacts.

Schema v3 preserves Phase 2 `files`, `contents`, `scan_runs`, `file_attempts`,
and `run_errors`, plus the v2 policy/catalog foundation. It adds structured
extraction identity, source ranges, metadata paths, current-asset state, and
real run/table/issue metrics to:

- `extraction_runs`
- `table_assets`
- `text_assets`
- `text_chunks`
- `semantic_metadata`
- `quality_issues`

The migration writes no synthetic asset rows. It deterministically backfills
policy status for existing file rows. See [REGISTRY.md](REGISTRY.md).

## Resilience and bounded work

- One corrupt, unsupported, or failed file produces a local outcome and does
  not terminate the batch.
- File/run state is durable; interrupted nonterminal work is retryable.
- Unchanged checks incorporate stable content identity plus pipeline/config
  versions before an extractor result may be reused.
- Cheap structured/document parsing, OCR, visual processing, and future heavy
  benchmark workers have separate bounded queues and concurrency.
- Heavy libraries/models are imported and initialized only in workers that
  need them. Processing never downloads missing artifacts.
- Arbitrarily large inputs are streamed or processed in bounded pages/batches.

## Future search boundary

Structured search will locate candidate table assets in the catalog, ask the
configured model for restricted read-only SQL, validate it, and execute it in
DuckDB against catalog/Parquet data.

Text search will locate `TextChunk` candidates through deterministic keyword
retrieval and, later, an injected vector-retrieval implementation before Qwen
synthesis. `src/chongzu/search.py` reserves `TextRetriever` and
`EmbeddingProvider` interfaces only. No embedding endpoint, local model, vector
database, or embedding field is assumed.

## Removed and benchmark-only routes

Apache Tika and Java are no longer planned default detector/parser paths.
Unstructured, Data Prep Kit, NiFi, NeMo Curator, OpenRefine runtime/server,
WSL, Docker, Kubernetes, Spark, and Ray are outside the product architecture.
HTML/CSS/XML remain discoverable but have no business extractor.

GMFT and Docling are future complex-table benchmark candidates only. They are
not default dependencies and will be retained only if representative corpus
evidence demonstrates a material advantage that lighter paths cannot provide.
