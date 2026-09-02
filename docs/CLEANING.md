# Phase 6 deterministic cleaning and profiling

Phase 6 is a local post-extraction stage. It consumes the published
`TableAsset` and `TextAsset` artifacts and produces new normalized artifacts,
profiles, quality signals, and Catalog rows. It does not call an LLM, modify a
source file, or overwrite an extraction artifact.

## Pipeline contract

```text
raw.parquet / raw text
        |
        +--> deterministic cleaner --> normalized artifact
        |                                  |
        +--------------------------------> profile + quality
                                           |
                                           v
                                    DuckDB catalog_assets
```

Cleaning is independently versioned from extraction. A cleaning identity
contains the asset ID, source content SHA-256, raw artifact SHA-256, cleaner
name/version, configuration version, profile version, and options such as
exact-duplicate removal. An unchanged identity with present artifacts is
reused. `--force` bypasses that reuse; changing the cleaner version reruns
only cleaning/profile and leaves extraction caches valid.

## Table rules

The table cleaner uses Polars and reads the extractor's raw Parquet. It applies
only mechanical operations whose meaning is deterministic:

- Unicode NFC, CRLF/CR to LF, and string edge whitespace normalization;
- explicit null tokens: empty/whitespace-only, `NULL`, `null`, `N/A`, and `NA`;
- removal of completely empty rows and columns;
- mechanical column names with safe duplicate suffixes (`name__2`);
- exact duplicate marking, with optional derived-only removal using
  `--drop-exact-duplicates`;
- conservative physical type inference for integer, float candidate, boolean
  candidate, date/datetime candidate, and string.

`0`, `-`, `/`, `未知`, and other potentially meaningful values are not null.
Numeric-looking values with leading zeroes, more than seven integer digits, or
other identifier-like risk remain strings and receive a profiling hint. No
business field name, unit, synonym, multi-row header, join, or anomalous value
is guessed in this phase.

## Text rules

Text cleaning applies Unicode NFC, newline normalization, removal of control
characters other than line/tab separators, trailing line whitespace removal,
and compression of runs of more than two blank lines. It never summarizes,
rewrites, semantically deduplicates, or deletes content based on importance.

## Manifest and artifacts

Each successful asset has a separate directory under
`workspace/artifacts/cleaning/` containing `normalized.parquet` or
`normalized.txt`, `cleaning.json`, and `profile.json`. The manifest includes
the cleaning identity, source/content/raw identities, original and normalized
column mapping, non-zero actions, inference hints, and:

```json
"layers": {
  "raw": "artifacts/tables/<id>/raw.parquet",
  "normalized": "normalized.parquet",
  "semantic": null
}
```

The path is workspace-relative and is never written beside the source. Atomic
publication means a failed write does not pre-delete an older raw or
successful extraction artifact.

## Profiles and quality

Table profiles record rows/columns, null count/ratio, distinct counts and at
most five samples per column, safe numeric/date min/max/mean/median, exact
duplicate count, empty rows/columns before cleaning, empty-cell and long-text
ratios, irregular widths, identifier/constant/high-cardinality hints, OCR
confidence when available, and provenance completeness. Text profiles record
chars, lines, pages, blocks, chunks, language hint if present, native/OCR
source, confidence, empty/low-content flags, and provenance completeness.

Quality status is deliberately source-aware:

| Status | Meaning |
| --- | --- |
| `ready` | No material deterministic quality signal was found. |
| `needs_review` | OCR/image/PDF candidate provenance or structure needs human/semantic review. The asset is retained; source provenance alone may set this status without creating a generic issue. |
| `unusable` | Empty or clearly failed output. The raw asset is retained when one exists. |

Signals such as `low_ocr_confidence`, `sparse_ocr`,
`possible_table_structure_loss`, `possible_column_shift`,
`possible_header_loss`, `possible_merged_cells`, suspicious one-row/one-column
shape, and `image_table_extractor_error` create `QualityIssue` evidence only.
They are not accuracy claims and do not silently delete a nonempty table.

## Failure isolation and benchmark

The coordinator uses bounded cleaning workers and one DuckDB writer. A failed
cleaner produces `cleaning_status=failed` and a `cleaning_failed` issue in the
Catalog while leaving raw extraction untouched; other assets continue.

```text
.\chongzu.cmd process "D:\Research Data\Project" --workers 2
.\chongzu.cmd benchmark cleaning "D:\Research Data\Project"
```

The benchmark reports asset counts, rows/chars, cleaned/reused counts,
normalize/profile/Parquet/DuckDB timings, wall time, and rows/sec. Its wall
clock covers the `process` call (including uncached extraction), while the
cleaning stage timers remain separate. It is an observation tool, not a
performance guarantee or accuracy KPI.
