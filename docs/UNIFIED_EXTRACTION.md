# Unified extraction and processing (Phase 5B/6 + Vision M2)

The expert extraction entry point is:

```text
.\chongzu.cmd extract "D:\Research Data\Project" [--workers 1..2] [--force]
```

`extract SOURCE` performs one Registry scan and then invokes the applicable
deterministic routes. The named commands (`extract structured`, `extract pdf`,
`extract pdf-table`, and `extract ocr`) remain available for diagnostics and
focused benchmarks.

The formal end-user workflow after Phase 6 is:

```text
.\chongzu.cmd process "D:\Research Data\Project" [--workers 1..4] [--force]
```

It runs the same incremental extraction coordinator, then deterministic
cleaning, profiling, quality assessment, and Catalog publication. `extract`
remains useful when only raw extraction is desired.

```text
FILE
  -> Registry / Processing Policy
       -> structured CSV/TSV/XLS/XLSX -> TableAsset
       -> native PDF PyMuPDF         -> TextAsset/TextChunk
       -> native PDF img2table       -> TableAsset candidate
       -> image/scanned PDF render
            -> one RapidOCR pass -> TextAsset/TextChunk
            -> OCRBlock adapter  -> 0..N image TableAssets
       -> TXT                        -> TextAsset/TextChunk
       -> deterministic cleaning -> profiles / QualityIssue -> catalog_assets
```

Table and text cardinality is independent. A screenshot or scanned page with a
title, body, table, and footer can publish text plus one or more candidate
tables. A no-table image publishes only text. The coordinator never turns a
weak table hint into a table by itself.

## Image and scanned-PDF route

The default/local route above remains offline. When the user explicitly selects
`ai_vision` and project `config/llm.json` is complete with
`vision_enabled=true`, the existing native PDF route still runs first. Only
pages marked by the PyMuPDF profile as clearly image-only are rendered locally
and sent one page at a time to the OpenAI-compatible Vision contract:

```text
scanned PDF page -> local PNG render -> Vision JSON -> strict validator
                 -> existing TextAsset/TableAsset -> deterministic cleaning
```

Native-text pages are never resent to Vision. A page produces at most one
user-visible Vision TextAsset; all useful text returned for that page is
aggregated into its TextChunks. Vision table output is bounded and validated
for shape, scalar values, headers, rows, cells, and size before publication.
The provider supplies neither file identity nor provenance: the local runner
injects file ID, source SHA-256, page, extraction run, model/contract, and render
metadata into the existing asset/artifact contracts.

The process task reports `vision_extraction` substages
`inspecting_pdf`, `rendering_page`, `calling_model`, `validating_response`, and
`writing_assets`, followed by `cleaning_assets`. Cancellation stops subsequent
page calls, recovers any running Registry marker, and retains committed page
results. A malformed or failed page records a structured issue and lets later
pages continue.

RapidOCR output is converted immediately to the internal `OCRBlock` contract:

```text
text, bbox, confidence, page_number/image, block_index,
extractor, extractor_version
```

The `img2table-image` adapter converts these blocks into img2table's `OCRData`
shape and calls `extract_tables(ocr=None)`. It receives the same decoded image
used by RapidOCR. Metadata and run warnings record `ocr_reused=true` and
`ocr_backend_calls=0`, making the no-double-OCR performance property
inspectable. If table reconstruction fails, the OCR TextAsset is kept and the
file is recorded as a partial result with `image_table_extractor_error`.

PDF pages are routed independently from the Phase 4A profile:

| Page evidence | Route |
| --- | --- |
| Reliable native text | PyMuPDF TextAsset + native img2table candidate |
| No reliable native text | PyMuPDF render + RapidOCR TextAsset + image table candidate |
| Mixed PDF | Apply the two rules page by page |

With Vision explicitly selected, the no-native-text row becomes
`PyMuPDF render + Vision TextAsset/TableAsset`; local OCR/img2table is not run
for those image/PDF targets. With Vision disabled, the RapidOCR/img2table
fallback remains the route.

One scanned page does not cause a native page to be OCR'd. All resulting assets
retain the same `file_id` and source SHA-256, with page/image provenance and
extractor versions.

## Summary and isolation

The command prints:

```text
Files discovered / Supported / Unsupported / Processed / Reused / Failed
TableAssets / TextAssets / TextChunks / QualityIssues
Structured files / Native PDF pages / OCR pages/images / Deferred / Wall time
```

Unsupported files remain registered and do not fail the batch. A corrupt XLSX,
PDF, or PNG produces an isolated failed file result while other files continue.
The registry and asset writes are parent-coordinated; derived artifacts are
atomic and remain below `workspace/`.

Each implemented route has its own content/version identity. A second
`extract SOURCE` therefore reuses scan facts, native PDF facts, native table
candidates, OCR TextAssets, image TableAssets, and TXT assets independently.
`--force` republishes the selected route identities. Vision PDF identities also
include the render and provider contract versions, so a changed source or
configuration does not silently reuse a previous page result.
Because one file can have several independent routes, the unified
`Reused extraction` value is a route-stage count and can exceed the file count.

## Phase 6 process and Catalog

`process SOURCE` never cleans source files or extraction artifacts in place.
For each current TableAsset/TextAsset it hashes the raw artifact, derives a
separate cleaning identity from the asset/source/raw identity plus cleaner,
profile, configuration, and options, and then reads the raw artifact once for
the deterministic post-pass. Successful output is written below
`workspace/artifacts/cleaning/`; `cleaning_runs`, `table_profiles`, and
`text_profiles` are committed by the single DuckDB writer. A later cleaner
version or option change re-runs only cleaning/profile; it does not invalidate
the extraction identity or force OCR/PyMuPDF/Calamine work.

The unified Catalog view exposes raw and cleaned paths together, so a cleaned
preview cannot hide its extraction origin. `catalog summary`, `catalog list`,
and `catalog show <asset-id>` provide bounded operator views. Table previews
default to 20 rows and text previews to 2,000 characters. Unsupported files
remain in `files` with `support_status=unsupported`; assets from OCR/PDF
candidate routes remain `needs_review` when their provenance or structure is
uncertain. `semantic_status` is `pending` until the explicit semantic command;
`process` itself never invokes semantic enrichment.

```text
.\chongzu.cmd catalog summary
.\chongzu.cmd catalog list --type table --quality needs_review --limit 20
.\chongzu.cmd catalog show <asset-id> --rows 20 --chars 2000
.\chongzu.cmd benchmark cleaning "D:\Research Data\Project"
.\chongzu.cmd semantic status
.\chongzu.cmd semantic enrich --provider fake --asset <asset-id>
```

## Quality signals

Image-table and PDF-candidate assets remain `needs_review` because their source
provenance is weaker than structured data. A QualityIssue is emitted only for
concrete evidence such as low OCR confidence, sparse OCR, suspicious single-row
or single-column output, possible column shift, possible header loss, possible
merged cells, long paragraph cells, incomplete provenance, or adapter errors.
A structurally nonempty candidate is not deleted merely because it is
suspicious.

The PDF profile field `possible_table_candidate` is a
`heuristic_hint_not_ground_truth`. The Phase 4C corpus showed that candidate
pages substantially over-approximate real tables, so counts are routing and
review evidence, not accuracy.

## Rendered-page consistency check

The optional benchmark uses up to 12 existing PNG renders from
`workspace/benchmark/pdf-real-v1/review/`, selecting both pages with native
candidate output and pages without it. It runs those PNGs through the image
route and writes the ignored `ocr-consistency.json`. It reports table-count
consistency, in-order shape matches, normalized cell overlap, OCR success, and
first/reuse timings. Native output is explicitly treated as a weak reference,
not human ground truth:

```text
.\chongzu.cmd benchmark pdf-consistency
.\chongzu.cmd benchmark pdf-consistency "D:\review" --max-pages 12
```

This benchmark does not call an LLM, embedding service, public endpoint, or
model download. It does not modify the source PDF or page PNGs.

## Offline and LLM boundary

Core/local extraction uses only project-local Python, packages, OCR models, and
DuckDB/Parquet. Cleaning, Catalog, and the Phase 7A Fake Provider use only
local code and already-published artifacts. There is no `pip`, `uv sync`,
Hugging Face, HTTP OCR, public endpoint, or automatic model fallback during
these routes. With a missing or blank `config/llm.json`, doctor reports `LLM
STATUS: NOT CONFIGURED / OPTIONAL`; this is normal and does not fail doctor or
process. Local routes make no DeepSeek, Qwen, OpenAI, Vision, or embedding
request. Real Vision is an explicit user-selected route only; this phase uses
mocked deterministic providers and does not claim Qwen acceptance.
