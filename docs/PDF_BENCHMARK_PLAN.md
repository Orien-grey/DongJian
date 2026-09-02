# PDF table benchmark plan (Phase 4B)

Phase 4C runs this plan against a user-supplied real corpus without changing
the candidate extractor. The full source is profiled first; a deterministic
stratified sample is then written to `workspace/benchmark/pdf-real-v1/` with a
blank `review.csv`. Candidate output is evidence for human review, not its own
ground truth. The current substitute corpus is small and table-light, so its
zero/low table count is not a KEEP/REMOVE decision.

Phase 4A is the control path: PyMuPDF supplies page inventory, native text
blocks, coordinates, image/drawing signals, and a reproducible profile. Phase
4B now provides a wheel-provisioned `img2table==2.0.0` candidate for native-text
pages only. It is not a permanent default and is never invoked with an OCR
engine.

## Corpus and protocol

1. The user supplies a sanitized PDF directory; do not scan the complete project
   by default. Select 30--100 representative files, including native text,
   borderless tables, ruled tables, multi-row headers, merged cells, rotated
   pages, and image-only/mixed pages.
2. Freeze the source SHA-256 list and record the Phase 4A profile for each page.
3. Run the candidate on native-text pages selected by the stored Phase 4A
   profile, including explicitly sampled negative controls. Mixed PDFs use only
   native-text pages; suspected-scanned pages are `deferred_to_ocr`. Keep the
   original PDFs read-only.
4. Store candidate output as versioned, provenance-linked artifacts and compare
   against a small human-reviewed reference. Never convert a heuristic hint
   directly into a `TableAsset`.

## Candidate sequence

```text
PyMuPDF native facts/profile
          |
          v
img2table candidate (Phase 4B, only after approval)
          |
          v
measured table quality and bundle-cost decision
```

GMFT and Docling are not the next phase and remain disabled unless later
representative evidence proves the current routes insufficient. Phase 5B uses
RapidOCR blocks with a local img2table image adapter for scanned/image pages;
this is a separate dual-extraction route, not a claim that native candidate
output is ground truth. `img2table` remains a candidate dependency until its
Windows x64 wheel/runtime and real-corpus result are reviewed.

## Evaluation dimensions

- table detection recall and page-level false positives;
- row/column correctness and cell-level value preservation;
- merged-cell and multi-row-header handling;
- header preservation and borderless-table behavior;
- native text versus image-only/mixed-page coverage;
- extraction speed, peak memory when measured, and failure isolation;
- wheel/runtime size increase, native DLL inventory, and relocation behavior;
- artifact/provenance completeness and repeat-run determinism.

The report must include the exact candidate version, Python/Windows wheel,
dependency tree, hashes, route reason, timings, quality issues, and examples of
both successes and false positives. A candidate may be retained only when its
measured benefit justifies its portability and maintenance cost.

## Current ground-truth machinery

`chongzu benchmark pdf-table <SOURCE> --ground-truth reference.json` reports
separate fields for expected/detected tables, true positives, false negatives,
obvious false positives, exact shape, row-count/column-count matches, exact and
Unicode-normalized cell matches, missing cells, and extra cells. The sidecar is
keyed by source-relative path and one-based page/table order. Reused candidate
runs are scored from their raw Parquet artifacts, so a benchmark can be rerun
without reparsing unchanged PDFs.

Synthetic fixtures validate this scoring and relocation machinery. They are not
a proxy for scientific-corpus accuracy. After the user supplies a sanitized
30--100 PDF sample, freeze source SHA-256 values, annotate a representative
reference set, and compare candidate output against the annotations. The final
retention decision is explicitly one of:

- **KEEP** — retain img2table as the default native PDF table path;
- **FALLBACK** — use it for simple/native tables and route difficult cases to a
  later measured extractor;
- **REMOVE** — the quality gain does not justify runtime footprint or
  maintenance cost.
