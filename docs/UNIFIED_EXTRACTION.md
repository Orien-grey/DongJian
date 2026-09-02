# Unified extraction (Phase 5B)

The formal operator entry point is:

```text
.\chongzu.cmd extract "D:\Research Data\Project" [--workers 1..2] [--force]
```

`extract SOURCE` performs one Registry scan and then invokes the applicable
deterministic routes. The named commands (`extract structured`, `extract pdf`,
`extract pdf-table`, and `extract ocr`) remain available for diagnostics and
focused benchmarks.

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
```

Table and text cardinality is independent. A screenshot or scanned page with a
title, body, table, and footer can publish text plus one or more candidate
tables. A no-table image publishes only text. The coordinator never turns a
weak table hint into a table by itself.

## Image and scanned-PDF route

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
`--force` republishes the selected route identities.

## Quality signals

Image-table candidates receive review status only when conservative evidence is
present. Signals include low OCR confidence, sparse OCR, suspicious single-row
or single-column output, possible column shift, possible header loss, possible
merged cells, long paragraph cells, and adapter errors. A structurally nonempty
candidate is not deleted merely because it is suspicious.

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

Core extraction uses only project-local Python, packages, OCR models, and
DuckDB/Parquet. There is no `pip`, `uv sync`, Hugging Face, HTTP OCR, public
endpoint, or automatic model fallback during extraction. With no real user
configuration, doctor reports `LLM STATUS: NOT CONFIGURED`; this is a normal
optional state. No DeepSeek, Qwen, OpenAI, vision, or embedding request is
made in Phase 5B.
