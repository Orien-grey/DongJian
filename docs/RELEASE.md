# Offline Windows Release Candidate

The release candidate is assembled only by the repository-local PowerShell
builder:

```powershell
.\scripts\build_release.ps1
```

It reads `VERSION`, copies an explicit allowlist, writes
`release-manifest.json`, `THIRD_PARTY_NOTICES.txt`,
`third-party-components.json`, and `licenses/`, and produces:

```text
release/ChongZu-<version>-rc1-win-x64/
release/ChongZu-<version>-rc1-win-x64.zip
release/ChongZu-<version>-rc1-win-x64.zip.sha256.txt
```

The builder checks `git status --porcelain` before assembly and requires a
clean Git tree. The bundle contains the production standalone CPython, pinned
packages, OCR models, application source/scripts, docs, `frontend/dist`, and
local license evidence. It excludes Git metadata, development venv/Node/uv,
interpreter provisioning site-packages, pip/npm caches, tests, benchmark and
acceptance data, real workspace state, `.env`, and generated registries. `uv`
is a provisioning-only development tool and is not required by normal launch,
process, Catalog, Search, or SQL routes.

The third-party audit reads the final production inputs from local
`*.dist-info/METADATA`, `LICENSE*`/`COPYING*`/`NOTICE*` files, the standalone
CPython license, frontend package metadata/license files, and the OCR model
manifest. It does not infer a model-weight license from the RapidOCR package.
The JSON manifest records `distributed`, `component_type`, evidence paths,
notice state, and review status. `REVIEW_REQUIRED` is an engineering finding,
not a legal conclusion.

The current local audit has six review-required distributed components:
`flatbuffers`, `rapidocr`, PyMuPDF's dual-license selection, and the three
OCR model files. The model manifest provides their current SHA-256 values but
does not provide upstream source/license/redistribution terms. The release
owner must resolve those items before calling an artifact legally cleared for
distribution.

Run the full synthetic product check with:

```powershell
runtime\venv\Scripts\python.exe scripts\run_phase10_acceptance.py
```

The harness tests a fresh directory relocation and the ZIP after extraction to
Chinese/space paths using `start.cmd`, localhost API polling, quality review,
Chinese lexical Search, selected-table SQL, hostile SQL rejection, restart
persistence, and source immutability. It never calls an LLM and does not use
the repository's real research workspace as input.

The current RC is deterministic only. `LLM_STATUS=NOT_CONFIGURED` is expected;
Phase 7B remains blocked until the user supplies configuration and explicitly
authorizes a separate real-provider acceptance. The known product boundaries
are:

- Search is lexical-only; there is no embedding/vector retrieval.
- SQL is limited to explicitly selected `TableAsset` inputs and at most
  50,000 input rows.
- PDF/image table reconstruction may require review.
- Semantic enrichment is implemented, but real-provider acceptance has not
  been run.
- LLM use is optional and is not configured in this RC.

These are release limitations, not deterministic acceptance failures.
