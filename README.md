# ChongZu

ChongZu is a fully relocatable, Windows x64 local research-data organization
workbench. It inventories one scientific project directory, independently
extracts tables and text, preserves source-level provenance, and catalogs the
results in embedded DuckDB plus Parquet. A user-configured OpenAI-compatible
Qwen service will later add semantic names, categories, field explanations,
summaries, and complex quality suggestions without overwriting extracted data.

The fixed development root is `E:\Desktop\ChongZu`; launchers derive the root
from their own location, so a prepared bundle can be moved as a directory.

Phase 3 now implements the first business extraction path: strict CSV/TSV and
native XLS/XLSX extraction into traceable `TableAsset` records plus separate
raw and normalized Parquet. It uses bounded workers, a central DuckDB writer,
content/version-based reuse, and atomic publication below `workspace/`. It does
not extract PDF/images/Office text, call an LLM, create embeddings, or provide
a frontend.

## Product flow

```text
File Registry -> Processing Policy -> Table Extraction -> TableAsset ----+
                                  \-> Text Extraction  -> TextAsset -----+-> Semantic Enrichment
                                                        -> TextChunk ----+        |
                                                                                  v
                                                                           Data Catalog
                                                                        DuckDB + Parquet
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

## Data safety and semantic boundary

- Sources are read-only and never renamed, moved, or edited.
- Derived artifacts live only below `workspace/`.
- Every asset links to `file_id`, content SHA-256, source sheet/page/section,
  extractor/version, and extraction run.
- Data is separated into `raw`, `normalized`, and `semantic` layers.
- Deterministic cleaning may normalize mechanics; semantic cleaning produces
  suggestions or metadata for human review and cannot overwrite raw data.
- The only future runtime network destination is an LLM base URL explicitly
  configured by the user. There is no automatic public fallback.

See [Architecture](docs/ARCHITECTURE.md), [Data model](docs/DATA_MODEL.md),
[Registry](docs/REGISTRY.md), [AI semantics](docs/AI_SEMANTICS.md),
[UI architecture](docs/UI_ARCHITECTURE.md), [Roadmap](docs/ROADMAP.md), and
[Runtime](docs/RUNTIME.md).

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/chongzu/` | Registry, detector, policy, asset/semantic/search contracts, and CLI |
| `tests/` | Automated tests and synthetic fixtures only |
| `config/` | Versioned non-secret examples; real `llm*.json` files are ignored |
| `scripts/` | Windows environment, bootstrap, doctor, and launcher scripts |
| `docs/` | Architecture, contracts, roadmap, UI, and runtime records |
| `runtime/` | Standalone CPython, production packages, dev venv, and uv payloads; ignored |
| `models/`, `cache/` | Future model artifacts and all controlled caches; ignored |
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
.\chongzu.cmd registry summary
```

For development under Windows PowerShell 5.1, load the repository-local
environment. If local script policy blocks it, invoke a child PowerShell with
the same temporary bypass used by the `.cmd` launchers.

```powershell
. .\scripts\env.ps1
& $env:CHONGZU_DEV_PYTHON -m pytest
& $env:CHONGZU_PROJECT_UV lock --check
```

`extract structured` automatically performs an incremental registry scan, so
operators do not need a separate scan step. `--force` republishes an otherwise
reusable extraction. The registry is `workspace\state\registry.duckdb`.

Pinned production packages are DuckDB 1.5.5, Polars 1.44.1 (with its
`polars-runtime-32` 1.44.1 Windows wheel), and python-calamine 0.8.2. They live
in `runtime\packages`; PyArrow, Pandas, NumPy, OpenPyXL, PyMuPDF, OCR, Docling,
Torch, Java, and Tika are not installed. See
[Structured extraction](docs/STRUCTURED_EXTRACTION.md).
