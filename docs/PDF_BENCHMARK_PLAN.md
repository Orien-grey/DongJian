# PDF table benchmark plan (Phase 4B)

Phase 4A is the control path: PyMuPDF supplies page inventory, native text
blocks, coordinates, image/drawing signals, and a reproducible profile. It does
not claim to recognize tables. Phase 4B will use those facts to select a small,
user-approved corpus and measure a candidate table extractor rather than
installing a heavy stack speculatively.

## Corpus and protocol

1. The user supplies a sanitized PDF directory; do not scan the complete project
   by default. Select 30--100 representative files, including native text,
   borderless tables, ruled tables, multi-row headers, merged cells, rotated
   pages, and image-only/mixed pages.
2. Freeze the source SHA-256 list and record the Phase 4A profile for each page.
3. Run the candidate on only pages with a measured table hint or an explicitly
   sampled negative control. Keep the original PDFs read-only.
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

GMFT and Docling remain later complex-table benchmark candidates. RapidOCR is a
separate Phase 5 image/scanned-document decision. No candidate is a default
dependency until its Windows x64 wheel/runtime and corpus result are recorded.

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
