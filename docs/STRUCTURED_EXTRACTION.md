# Native structured extraction

Phase 3 implements the registry-backed CSV/TSV/XLS/XLSX path. It produces real
`TableAsset`, `extraction_runs`, `quality_issues`, Parquet, and provenance JSON.
PDF native text is now a separate Phase 4A route; see
[`PDF_EXTRACTION.md`](PDF_EXTRACTION.md). This document does not describe
image/OCR, semantic/LLM, search, or UI work.

## Operator contract

```text
.\dongjian.cmd extract structured "D:\Research Project" [--workers 1..8] [--force]
.\dongjian.cmd benchmark structured "D:\Research Project" [--workers 1..8] [--force]
```

The command automatically performs an incremental Phase 2 scan first. Scan
and extraction remain separate Python modules, but the operator/UI does not
need to coordinate two commands. Output is a quiet aggregate summary; an
isolated corrupt file increments `Failed` without stopping other files.

## Delimited path

CSV and TSV use a strict deterministic fast path:

1. select comma or tab from the registered business format;
2. accept strict UTF-8, UTF-8 BOM, then a strict conservative GB18030 fallback;
3. stream-transcode non-UTF-8 input to a temporary UTF-8 file below the
   workspace staging tree;
4. validate every logical record with standard-library `csv` in strict mode;
5. reject ragged/malformed data rather than truncate or ignore fields;
6. use Polars lazy scanning and streaming Parquet sinks with all CSV cells kept
   as strings (no aggressive inference).

Quoted delimiters and quoted multiline cells are logical CSV records. Empty
files create no table and an `empty_file` issue; header-only files create a
zero-row table. Duplicate headers are mechanically suffixed (`name`,
`name__2`) and retain a mapping plus `duplicate_column_names` issue. Raw
Parquet uses positional columns; the exact original header lives in metadata.

## Excel path and Sheet boundaries

python-calamine opens one XLS/XLSX workbook and releases it after processing.
Sheets are visited in workbook order, including Unicode names; visibility and
empty state are retained when the library reports them. Only one Sheet matrix
is materialized at a time. Corrupt workbooks have an isolated
`corrupt_workbook` run outcome.

A Sheet is evidence, not automatically one table. Its full Calamine view is
stored once below:

```text
workspace/artifacts/sheets/<file_id>/<sha-prefix>/sheet-0001/
  raw.parquet
  metadata.json
```

The conservative detector trims outer all-empty margins and separates regions
only across completely empty row or column bands. An adjacent one-cell title,
footnote, or possible multi-row header stays with the region rather than being
silently cut. Review evidence uses issue types including:

- `ambiguous_table_boundary`
- `possible_title_row`
- `possible_multirow_header`
- `multiple_table_regions`
- `duplicate_column_names`
- `empty_sheet`

This is precision-first. Phase 7 may interpret titles/headers semantically,
but model output will remain separate metadata.

## Raw, normalized, and provenance artifacts

Each table has a stable path independent of display names:

```text
workspace/artifacts/tables/<table_id>/
  raw.parquet
  normalized.parquet
  metadata.json
```

Publication writes a unique temporary sibling and uses atomic replacement.
Raw region Parquet keeps positional columns and original extracted values.
Normalized Parquet only trims text, applies Unicode NFC, and creates unique,
representable column names. It does not merge tables, combine multi-row
headers, rename fields semantically, unify units, or aggressively coerce
types. Semantic output remains null.

Metadata fixes a zero-based, half-open row/column coordinate system and stores
the source relative path, file ID, SHA-256, sheet index/name, extraction run,
extractor/version, original-to-normalized column mapping, full Sheet artifact,
and the source row corresponding to Parquet row zero. DuckDB stores these
catalog/provenance facts, not the complete large table payload.

## Incremental reuse and concurrency

Reuse identity hashes:

```text
file_id + content_sha256 + business_format + extractor name/version
+ structured config version + stable extraction identity schema version
```

All matching artifacts must still exist. Changed content, extractor/config
version changes, or `--force` trigger a new run. A failed rerun does not delete
the prior artifact first. Current catalog state is switched by the single
DuckDB coordinator only after a successful/partial result is published.

Concurrency is bounded to 1..8 workers with at most twice that many submitted
futures. Workers write only their own atomic artifacts; they never share a
DuckDB connection. A workbook is released after its file finishes. CSV uses
Polars streaming sinks, although strict structural validation deliberately
performs an additional sequential read.

## Benchmark fields and known limitations

The benchmark reports files, Sheets, TableAssets, rows, bytes, files/s,
rows/s, MB/s, extraction, normalization, Parquet-write, registry-write, and
wall time. Peak memory is explicitly not reported because Phase 3 has no
reliable zero-cost cross-process sampler.

Known limits:

- only UTF-8/BOM and GB18030 fallback are supported for delimited text;
- CSV type inference is deliberately deferred;
- Calamine returns cell values, not complete formatting/formula/comment
  semantics;
- merged cells, formula intent, and styled blank areas do not trigger
  OpenPyXL fallback;
- region detection will retain ambiguous adjacent titles/footnotes rather than
  guess;
- large Excel Sheets are currently one-Sheet-at-a-time matrices, not streaming
  cell iterators;
- no real research directory is processed until the user supplies a sanitized
  representative path.
