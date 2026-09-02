# ChongZu

ChongZu is a fully relocatable, Windows x64 local research-data organization
workbench. It inventories one scientific project directory, independently
extracts tables and text, deterministically cleans/profiles the derived assets,
and catalogs the results in embedded DuckDB plus Parquet. A user-configured
OpenAI-compatible service may later add semantic names, categories, field
explanations, summaries, and complex quality suggestions without overwriting
extracted data. No LLM or network endpoint is configured or called in the
current core workflow.

The fixed development root is `E:\Desktop\ChongZu`; launchers derive the root
from their own location, so a prepared bundle can be moved as a directory.

Phase 3 now implements the first business extraction path: strict CSV/TSV and
native XLS/XLSX extraction into traceable `TableAsset` records plus separate
raw and normalized Parquet. It uses bounded workers, a central DuckDB writer,
content/version-based reuse, and atomic publication below `workspace/`.
Phase 4A adds the native PDF facts path: PyMuPDF page inventories, native text
blocks, page/block provenance, deterministic chunks, and PDF profiles. Phase 4B
keeps `img2table` as a measured native-text table candidate; Phase 4C records a
read-only real-corpus baseline. Phase 5A adds a local, offline RapidOCR + ONNX
Runtime foundation, and Phase 5B connects it to the image table adapter and
formal `extract SOURCE` pipeline. Images and scanned PDF pages may now emit
independent TextAssets and TableAssets from one OCR pass. Phase 6 adds the
formal `process SOURCE` route: deterministic cleaning, bounded profiling,
quality status/issues, and a queryable `catalog_assets` view. No LLM,
embedding, or frontend is part of the core route.

## Product flow

```text
File Registry -> Processing Policy -> Table Extraction -> TableAsset ----+
                                  \-> Text Extraction  -> TextAsset -----+-> Deterministic Cleaning
                                                        -> TextChunk ----+       |
                                                                                v
                                                                           Profiling / Quality
                                                                                |
                                                                                v
                                                                            Data Catalog
                                                                         DuckDB + Parquet
                                                                                |
                                                                                v
                                                                        Future Semantic Layer
```

Table and text extraction are independent. A single PDF, image, Word file, or
presentation may produce both 0..N `TableAsset` records and 0..N `TextAsset`
records. AI-generated display names never become stable asset IDs.

## First-stage support matrix

| Format | Table | Text | OCR/visual possibility | Business status |
| --- | --- | --- | --- | --- |
| CSV, TSV | yes | no default text pass | no | supported |
| XLS, XLSX | yes | optional later metadata text | no | supported |
| PDF | yes | yes | scanned/complex pages may need it | supported |
| JPG, JPEG, PNG | yes | yes | yes; screenshots remain images | supported |
| DOC, DOCX | yes | yes | future evidence-based escalation only | supported |
| PPT, PPTX | yes | yes | future evidence-based escalation only | supported |
| TXT | no | yes | no | supported |
| HTML, CSS, XML, JS | no | no | no | unsupported, retained in Registry |
| ZIP contents | no | no | no | unsupported, archive is not expanded |
| executable/binary unknown or any unlisted format | no | no | no | unsupported, retained in Registry |

The Phase 2 lightweight detector remains in place. Detection records what a
file appears to be; the deterministic processing policy separately records
whether the product supports it and which extraction branches are candidates.
The Phase 4A PyMuPDF branch and Phase 4B `img2table` native-text candidate are
independent: `extract pdf-table` preserves the Phase 4A
`TextAsset`/`TextChunk` rows while adding zero or more PDF `TableAsset` rows.
The candidate explicitly defers image-only/suspected-scanned pages to the
Phase 5B OCR/image route and is not yet a permanent default extractor.
`possible_table_candidate`
is a weak heuristic routing hint, not table ground truth.

The current runtime reports `LLM STATUS = NOT CONFIGURED` when no real config
exists. This is normal: extraction never calls DeepSeek, Qwen, OpenAI, a vision
API, an embedding API, or a public endpoint/fallback.

## Data safety and semantic boundary

- Sources are read-only and never renamed, moved, or edited.
- Derived artifacts live only below `workspace/`.
- Every asset links to `file_id`, content SHA-256, source sheet/page/section,
  extractor/version, and extraction run.
- Data is separated into `raw`, `normalized`, and `semantic` layers.
- Deterministic cleaning may normalize mechanics; semantic cleaning produces
  suggestions or metadata for human review and cannot overwrite raw data.
- `process SOURCE` writes separate cleaning manifests, normalized artifacts,
  profiles, and catalog metadata. Raw artifacts and source SHA-256 remain
  unchanged; `semantic_status` is `pending` until a future authorized pass.
- The only future runtime network destination is an LLM base URL explicitly
  configured by the user. There is no automatic public fallback.

See [Architecture](docs/ARCHITECTURE.md), [Data model](docs/DATA_MODEL.md),
[Cleaning](docs/CLEANING.md), [Data Catalog](docs/DATA_CATALOG.md),
[Unified extraction](docs/UNIFIED_EXTRACTION.md), [Registry](docs/REGISTRY.md),
[AI semantics](docs/AI_SEMANTICS.md), [UI architecture](docs/UI_ARCHITECTURE.md),
[Roadmap](docs/ROADMAP.md), and [Runtime](docs/RUNTIME.md).

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/chongzu/` | Registry, detector, policy, asset/semantic/search contracts, and CLI |
| `tests/` | Automated tests and synthetic fixtures only |
| `config/` | Versioned non-secret examples; real `llm*.json` files are ignored |
| `scripts/` | Windows environment, bootstrap, doctor, and launcher scripts |
| `docs/` | Architecture, contracts, roadmap, UI, and runtime records |
| `runtime/` | Standalone CPython, production packages, dev venv, and uv payloads; ignored |
| `runtime/models/ocr/` | Pinned RapidOCR ONNX models and integrity manifest; ignored |
| `models/`, `cache/` | Future model placeholders and all controlled caches; ignored |
| `workspace/input/` | Immutable real source evidence; ignored |
| `workspace/staging/` | Recoverable intermediate work; ignored |
| `workspace/artifacts/` | Stable raw/normalized Parquet and provenance metadata; ignored |
| `workspace/output/` | Future reports and exports; ignored |
| `workspace/state/` | `registry.duckdb`, checkpoints, and query state; ignored |
| `workspace/quarantine/`, `workspace/logs/` | Isolated failure metadata and structured logs; ignored |

No real research input, secret, downloaded runtime/package/model, generated
database, Parquet file, cache, or output belongs in Git.

## Current commands

Formal portable launchers require no activation:

```text
.\doctor.cmd
.\chongzu.cmd scan "D:\Research Data\Project"
.\chongzu.cmd extract structured "D:\Research Data\Project" --workers 4
.\chongzu.cmd benchmark structured "D:\Research Data\Project"
.\chongzu.cmd extract pdf "D:\Research Data\Project"
.\chongzu.cmd benchmark pdf "D:\Research Data\Project"
.\chongzu.cmd extract pdf-table "D:\Research Data\Project" --workers 2
.\chongzu.cmd benchmark pdf-table "D:\Research Data\Project" --ground-truth reference.json
.\chongzu.cmd extract ocr "D:\Research Data\Project" --workers 2
.\chongzu.cmd extract "D:\Research Data\Project" --workers 2
.\chongzu.cmd process "D:\Research Data\Project" --workers 2
.\chongzu.cmd benchmark cleaning "D:\Research Data\Project" --workers 2
.\chongzu.cmd catalog summary
.\chongzu.cmd catalog list --type table --quality needs_review --limit 20
.\chongzu.cmd catalog show <asset-id> --rows 20 --chars 2000
.\chongzu.cmd benchmark pdf-consistency
.\chongzu.cmd benchmark ocr "D:\Research Data\Project" --workers 2
.\chongzu.cmd registry summary
```

For development under Windows PowerShell 5.1, load the repository-local
environment. If local script policy blocks it, invoke a child PowerShell with
the same temporary bypass used by the `.cmd` launchers.

```powershell
. .\scripts\env.ps1
& $env:CHONGZU_DEV_PYTHON -m pytest
& $env:CHONGZU_PROJECT_UV lock --check --offline
```

`extract structured` automatically performs an incremental registry scan, so
operators do not need a separate scan step. `--force` republishes an otherwise
reusable extraction. The registry is `workspace\state\registry.duckdb`.

Pinned production packages are DuckDB 1.5.5, Polars 1.44.1 (with its
`polars-runtime-32` 1.44.1 Windows wheel), python-calamine 0.8.2, PyMuPDF
1.28.2, the Phase 4B candidate `img2table` 2.0.0, RapidOCR 3.9.2, and ONNX
Runtime 1.29.0 with locked native dependencies. They live in
`runtime\packages`; the three RapidOCR ONNX models and their SHA-256 manifest
live in `runtime\models\ocr`. PyArrow, Pandas, OpenPyXL, Docling, Torch, Java,
and Tika are not installed. NumPy, OpenCV, pypdfium2, Pillow, Shapely,
pyclipper, requests, and the other small packages are transitive runtime
dependencies of the table/OCR candidates. See
[Structured extraction](docs/STRUCTURED_EXTRACTION.md),
[PDF extraction](docs/PDF_EXTRACTION.md), and
[PDF table extraction](docs/PDF_TABLE_EXTRACTION.md), and
[OCR foundation](docs/OCR_FOUNDATION.md), [unified extraction](docs/UNIFIED_EXTRACTION.md),
[cleaning](docs/CLEANING.md), and [data catalog](docs/DATA_CATALOG.md).
