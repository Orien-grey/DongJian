# ChongZu

ChongZu is a Windows-native, local research-data processing project for a single directory containing approximately 1,500 heterogeneous files. It will identify actual file types, extract text and tables, normalize results, profile and clean data, and persist queryable outputs without modifying the originals.

This working project's fixed root is `E:\Desktop\ChongZu`.

The repository is currently at **Phase 1**: project-local Python foundation and environment diagnostics. There is not yet an executable data-processing pipeline.

## Non-negotiable constraints

- Production target: Windows x64 only.
- No administrator rights, WSL, Docker, Conda, or system services.
- The finished bundle keeps CPython 3.11.x, Java, Tika, Python packages, native tools, and models inside this project.
- Core processing must work offline after an explicit preparation step.
- Runtime behavior must not depend on system Python/Java, global `PATH`, `%USERPROFILE%`, or an uncontrolled user cache.
- Source files are immutable. Failures are isolated per file and batch work is resumable.

The project intentionally excludes Kubernetes, Spark, Ray, NiFi, NeMo Curator, the full Unstructured stack, Data Prep Kit runtime, and OpenRefine Server.

## Planned workflow

1. Discover files and record stable SHA-256 fingerprints.
2. Detect true MIME/type, using Tika where authoritative identification or fallback is needed.
3. Route to the cheapest suitable extractor:
   - fast: native text, HTML/XML, Office Open XML, and spreadsheet readers;
   - medium: PyMuPDF for normal PDFs, RapidOCR for images/required scanned pages, and Tika fallback;
   - slow: Docling only for justified complex/scanned documents.
4. Convert results to a canonical text/table representation with provenance and quality signals.
5. Profile and clean deterministically, then persist to Parquet and DuckDB.
6. Resume safely, skip unchanged files, and report per-file/per-stage timing and failures.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for routing and component boundaries, [docs/ROADMAP.md](docs/ROADMAP.md) for staged delivery, [docs/ENVIRONMENT_AUDIT.md](docs/ENVIRONMENT_AUDIT.md) for the Phase 0 host audit, and [docs/RUNTIME.md](docs/RUNTIME.md) for the project-local runtime record.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/chongzu/` | Future Python package |
| `tests/` | Automated tests and small synthetic fixtures |
| `config/` | Versioned, non-secret configuration templates |
| `scripts/` | Windows environment, bootstrap, and diagnostic launch scripts |
| `docs/` | Architecture, roadmap, and audit records |
| `runtime/` | Project-local CPython, venv, uv, Java, and Tika payloads; ignored by Git |
| `models/` | OCR and Docling model artifacts; ignored by Git |
| `cache/` | All controlled tool/package/model caches; ignored by Git |
| `workspace/input/` | Real immutable source data; ignored by Git |
| `workspace/staging/` | Recoverable intermediate work; ignored by Git |
| `workspace/output/` | Canonical, Parquet, and report outputs; ignored by Git |
| `workspace/state/` | DuckDB registry, checkpoints, and run state; ignored by Git |
| `workspace/quarantine/` | Failure metadata or explicitly copied problem artifacts; ignored by Git |
| `workspace/logs/` | Structured run logs and metrics; ignored by Git |

Only `.gitkeep` placeholders are versionable inside runtime, model, cache, and workspace directories. No real research data or downloaded artifact belongs in Git.

## Phase 1 commands

On Windows PowerShell 5.1, use a temporary execution-policy bypass if the host policy blocks local scripts; this does not change the policy permanently:

```powershell
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor.ps1
```

The scripts invoke only `runtime\uv\uv.exe` and `runtime\venv\Scripts\python.exe`. `env.ps1` changes only the current process and keeps cache/temp locations below this project.
