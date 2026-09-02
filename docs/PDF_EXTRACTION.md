# Native PDF text extraction (Phase 4A)

Phase 4A establishes the PDF facts layer without OCR or table recognition. The
registered source remains immutable and is opened by PyMuPDF one file at a time.
The coordinator automatically performs an incremental scan, selects supported
`pdf` rows, and writes results through the central DuckDB registry writer.

## Operator commands

```text
.\chongzu.cmd extract pdf "D:\Research Project" [--workers 1..4] [--force]
.\chongzu.cmd benchmark pdf "D:\Research Project" [--workers 1..4] [--force]
.\chongzu.cmd extract pdf-table "D:\Research Project" [--workers 1..4] [--force]
.\chongzu.cmd benchmark pdf-table "D:\Research Project" [--workers 1..4] [--force] [--ground-truth reference.json]
```

The explicit `pdf` and `pdf-table` subcommands remain available as expert
routes. The formal user entry point is `extract SOURCE`, which coordinates all
applicable routes. Unsupported files and
supported-but-not-yet-implemented image or Office routes remain in the Registry
and are not treated as PDF failures. `pdf-table` is the Phase 4B img2table
candidate and never enables OCR.
`--force` bypasses reuse. The default is one worker; higher values use a bounded
process pool and retain a single DuckDB writer.

## Extraction layers

`src/chongzu/extract/pdf/` separates the route into:

- `pymupdf_extractor.py`: opens the PDF, inventories pages, extracts text blocks,
  and creates `TextAsset`/`TextChunk` contracts;
- `blocks.py`: converts PyMuPDF dictionaries, applies NFC/line-ending/control
  cleanup, and performs deterministic paragraph-aware chunking;
- `profiling.py`: calculates page and document facts and weak table hints;
- `artifacts.py`: publishes text and profile JSON atomically below `workspace`;
- `runner.py`: scan, identity/reuse, bounded execution, timings, and registry
  persistence.

Phase 4B adds `img2table_extractor.py`, `table_quality.py`, and
`table_runner.py`. The table runner reuses the stored Phase 4A profile and
publishes page/table `TableAsset` records without replacing any text records.
See [PDF table extraction](PDF_TABLE_EXTRACTION.md) for the candidate contract.

One PDF may produce zero or many text assets, one for each non-empty native text
block. Each block carries page number, source bounding box, source relative path,
file ID, content SHA-256, extractor/version, and extraction run ID. No PDF
`TableAsset` is produced in Phase 4A; a weak table hint is only a profile fact
and `QualityIssue` for future Phase 4B routing.

## Profile contract

Each PDF receives a profile artifact with `profile_version`, `page_count`,
`total_chars`, `chars_per_page`, `text_block_count`, `image_count`,
`pages_with_text`, `pages_without_text`, `text_coverage_ratio`,
`native_text_available`, `suspected_scanned_pages`, `classification`,
`reason_codes`, PDF metadata, and a `pages` array. Each page records:

`page_number`, width/height, rotation, total/effective characters, text-block and
image counts, text/image area coverage estimates, drawing/grid-line counts,
native-text/scanned/table-candidate booleans, and reason codes. The profile also
labels table candidates as `heuristic_hint_not_ground_truth`.

The classification is deliberately conservative:

- `native_text`: every page has effective native text;
- `mixed`: native text coexists with blank or suspected-scanned evidence;
- `suspected_scanned`: image evidence and low effective text occur on at least
  half of pages;
- `unknown`: no native text without enough image evidence to assert a scan.

Text scarcity alone never declares a scan. `possible_table_candidate` uses only
aligned short text blocks and/or several drawing lines; it is a weak routing
hint, not a table extractor or ground truth, and cannot create a `TableAsset`.

## Artifacts and provenance

```text
workspace/artifacts/text/<text_asset_id>/
  raw.txt
  normalized.txt
  metadata.json
workspace/artifacts/pdf_profiles/<file_id>/<content_sha256>/profile.json
```

Raw text is the PyMuPDF block text. Normalized text applies only Unicode NFC,
line-ending normalization, and removal of NUL/other control garbage while
retaining paragraph boundaries. Metadata records the source path, SHA, page,
block, run, extractor versions, chunk configuration, and offset basis. The PDF
itself is never copied or modified.

Chunks are page/block-local and deterministic. They prefer newline boundaries,
then use a conservative character limit; the current default has no overlap.
`chunk_id` includes the parent asset, chunk index, offsets, and
`text-chunk-v1`, so future re-chunking does not require re-parsing the PDF.

## Reuse and failure behavior

Reuse identity includes file ID, content SHA-256, business format, extractor and
version, `pdf-native-text-v1`, `text-chunk-v1`, and the stable extraction
identity schema version. Additive Catalog schema migrations do not invalidate
this identity.
Successful and partial runs are reused only when every referenced text and
profile artifact still exists. Changed content, extractor/config changes, or
`--force` create a new run and make the new assets current only after atomic
publication. A corrupt PDF is isolated as `corrupt_pdf`; prior successful
artifacts are not pre-deleted and unrelated files continue processing.

## Phase 4B boundary

Native PDF text and candidate table extraction are independent cache identities.
An unchanged PyMuPDF result can be reused while a changed img2table/config
identity is rerun, and vice versa. `--force` applies to the selected route.
Image-only/suspected-scanned pages are recorded as `deferred_to_ocr` by the
native table candidate; Phase 5B routes the same profile-selected pages to the
local RapidOCR plus image-table adapter. The candidate's synthetic or rendered
page consistency scores do not stand in for a real-corpus quality decision.

## Known limitations

- OCR is a Phase 5A/5B route. Native PDF table detection remains a separate
  Phase 4B candidate and is intentionally not final/default yet.
- Built-in block text is not a semantic section/header interpretation.
- Page coverage estimates are bounding-box signals, not pixel-accurate unions.
- CJK/font encoding quality depends on the source PDF's embedded ToUnicode maps.
- The default serial route favors predictable native-library behavior; bounded
  process workers are available for benchmark runs.
- No user research directory is processed until a sanitized representative path
  is explicitly supplied.
