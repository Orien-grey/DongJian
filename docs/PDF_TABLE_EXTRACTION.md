# Native PDF table extraction (Phase 4B)

Phase 4B adds `img2table==2.0.0` as a measured candidate for native-text PDF
tables. It is deliberately not declared to be the final PDF parser. The route
is:

```text
registered PDF
    -> Phase 4A PyMuPDF profile (routing facts)
    -> img2table candidate, OCR disabled
    -> zero or more page/table TableAssets
```

The existing PyMuPDF route remains independent. A PDF with a title, paragraphs,
a table, and a footer therefore keeps its page/block `TextAsset` and
`TextChunk` records while also publishing one `TableAsset` per detected table.
There is no exclusive table-versus-text branch.

`possible_table_candidate` from the PDF profile is only a weak heuristic
routing hint (`heuristic_hint_not_ground_truth`), not evidence that a real table
exists. The native candidate must still produce a concrete table structure
before a `TableAsset` is published.

## Operator commands

```text
.\chongzu.cmd extract pdf-table "D:\Research Project" [--workers 1..4] [--force]
.\chongzu.cmd benchmark pdf-table "D:\Research Project" [--workers 1..4] [--force]
.\chongzu.cmd benchmark pdf-table "D:\Synthetic PDFs" --ground-truth reference.json
```

The command performs an incremental scan automatically. `--force` applies only
to the candidate table identity; the Phase 4A text identity can still be
reused. Unsupported files remain registry rows and image-only/suspected-scanned
PDFs are `deferred_to_ocr`, not failures. No OCR package, model, or network
call is involved.

## Profile-driven routing

The stored Phase 4A profile is the source of truth:

| Profile | Candidate pages | Result |
| --- | --- | --- |
| `native_text` | all pages | native candidate attempted |
| `mixed` | `pages_with_text` only | image-only pages deferred |
| `suspected_scanned` | none | `deferred_to_ocr` |
| `unknown` with page count | all known pages, conservative probe | candidate result recorded |
| missing/empty profile | none | deferred with reason |

The table route never reimplements scan heuristics and never turns a weak
`possible_table_candidate` profile hint into a TableAsset by itself.
Candidate metadata carries the same weak-hint semantics.

## Asset and artifact contract

Each detected page/table pair has a provenance-derived stable ID and uses the
same contract as CSV/Excel:

```text
workspace/artifacts/tables/<table_id>/
  raw.parquet
  normalized.parquet
  metadata.json
```

`raw.parquet` preserves the candidate matrix with positional source columns.
`normalized.parquet` applies only NFC/outer-trim, rectangular padding, and safe
mechanical column names. It does not rename fields semantically, alter units,
guess values, merge tables, or invoke AI. Metadata records file ID, content
SHA-256, relative path, page/table index, optional reliable PDF-point bbox,
source row/column ranges, extractor/version, extraction run, candidate config,
raw row widths, column mapping, and any limitations. A missing bbox or confidence
is left null rather than invented.

Evidence-backed `QualityIssue` rows include empty detections, suspicious single
rows/columns, ragged/shifted columns, possible merged cells, multiple tables on
one page, and isolated candidate errors. Normal tables do not receive noise
issues merely because they were detected.

## Ground truth and benchmark output

The optional JSON sidecar is keyed by source-relative path and contains expected
page/table order and cell matrices. The benchmark reports independently:

- expected/detected tables, true positives, false negatives, obvious false
  positives;
- exact shape, row-count and column-count matches;
- expected/detected cells, exact and NFC-normalized cell matches, missing cells,
  and extra cells;
- page/row/cell counts, extraction/normalization/Parquet/Registry timings,
  wall clock, throughput, deferred pages, failures, and candidate runtime size.

Synthetic PDFs cover ruled and borderless tables, Chinese text, dates/numbers,
titles/paragraphs/footers, two tables on a page, multi-page tables, merged-cell
candidates, no-table negatives, image-only defer, and corrupt-file isolation.
They verify correctness machinery, cache behavior, and relocation—not real
scientific accuracy.

## Candidate retention

After synthetic acceptance, the user should supply a sanitized 30--100-file
representative corpus. Freeze source hashes and annotate enough ground truth to
measure detection recall, false positives, row/column correctness, merged-cell
and header preservation, borderless behavior, speed, memory, artifact lineage,
and bundle-size increase. The result must be one of:

- **KEEP** — img2table becomes the default native-text PDF table extractor;
- **FALLBACK** — it handles simple tables while difficult cases escalate to a
  later measured route;
- **REMOVE** — its benefit does not justify its native runtime cost.

GMFT and Docling are not part of the next phase and are not installed here.
RapidOCR image/scanned-page integration is handled by Phase 5B, not this
native-text candidate. Semantic naming,
classification, field interpretation, summaries, and complex quality judgments
remain a later provider-neutral DeepSeek/Qwen layer and cannot overwrite raw or
normalized artifacts.
