# Project-local runtime record

Phase 1 runtime preparation and Phase 2.5 portable-runtime validation were
performed on 2026-09-01 (Asia/Shanghai) for the fixed development root
`E:\Desktop\ChongZu`. The later Architecture Refactor changes the product
shape but does not change this accepted runtime contract. Launchers and the production paths below are root-relative
at runtime and are intended to survive copying the project directory.

## CPython selection

The project uses **CPython 3.11.15, Windows x64**. The project-local uv `0.11.21` was queried with:

```text
runtime\uv\uv.exe python list 3.11 --all-versions
```

The catalog offered 3.11.15 down through 3.11.1 for `windows-x86_64`; 3.11.15 was selected because it is the newest stable 3.11.x patch shown by the installed uv catalog at preparation time. Python 3.12 and 3.13 were not selected because the project contract is CPython 3.11.x.

The distribution was installed with uv's managed standalone CPython mechanism:

```text
runtime\uv\uv.exe python install 3.11.15 `
  --install-dir E:\Desktop\ChongZu\runtime\python `
  --no-bin --no-registry
```

The source mechanism is uv's official managed Python download catalog, which supplies a CPython standalone distribution for `windows-x86_64` from the [Astral `python-build-standalone` distribution family](https://github.com/astral-sh/python-build-standalone). No Miniconda or system Python was used. The exact catalog selection is intentionally delegated to the pinned uv binary; future provisioning must record the resolved artifact URL and archive hash before release packaging.

Installed executable:

```text
E:\Desktop\ChongZu\runtime\python\cpython-3.11.15-windows-x86_64-none\python.exe
```

Observed values:

- `Python 3.11.15`
- `win-amd64`
- `python.exe` SHA-256: `749a54c7896d14138d74c6e35f89cb4f8fb1c59d1dd6ddaeaa8107b2a8e0aba2`

The install command used an explicit project-local install directory, `UV_PYTHON_INSTALL_DIR` under `runtime\python`, `--no-registry`, and a project-local `UV_CACHE_DIR`. uv's default managed-Python directory (`%APPDATA%\uv\python`) is not used by the project launchers. `scripts\env.ps1` sets `UV_PYTHON` to the executable above and `UV_MANAGED_PYTHON=1`; project execution also sets `UV_PYTHON_DOWNLOADS=never`.

## Project-private uv

The existing host executable was copied, not moved or modified:

```text
Source:  C:\Users\Orion\.local\bin\uv.exe
Private: E:\Desktop\ChongZu\runtime\uv\uv.exe
Version: uv 0.11.21 (5aa65dd7a 2026-06-11 x86_64-pc-windows-msvc)
SHA-256: 5a7ec85884c2ccb1be560cb8fac3eb890df1adf49bfcc070a270ba70401bdd68
```

Subsequent project commands must invoke `runtime\uv\uv.exe` explicitly. The binary is intentionally ignored by Git as a runtime payload; `runtime\uv\.gitkeep` preserves the directory in the baseline tree.

## Python environment

The development and Phase 1 execution environment is:

```text
E:\Desktop\ChongZu\runtime\venv\Scripts\python.exe
```

It was created from the project CPython with uv, without `--system-site-packages`. A live check reported:

```text
sys.executable = E:\Desktop\ChongZu\runtime\venv\Scripts\python.exe
sys.prefix     = E:\Desktop\ChongZu\runtime\venv
sys.base_prefix= E:\Desktop\ChongZu\runtime\python\cpython-3.11.15-windows-x86_64-none
```

This is project-contained, but an ordinary Windows venv is **not claimed to be fully relocatable**. Its launcher/metadata can retain the base interpreter path. Moving the whole project directory may therefore require rebuilding `runtime\venv` from the project CPython. A genuinely relocatable release bundle is a later release-phase concern.

## Phase 2.5 portable runtime foundation

The development venv above is deliberately not the delivery runtime. The
portable contract is a standalone CPython tree plus a flat, project-local
package directory:

```text
runtime\python\cpython-3.11.15-windows-x86_64-none\python.exe
runtime\packages\
src\
```

Formal launchers (`doctor.cmd` and `chongzu.cmd`) derive the project root from
their own location, set `PYTHONPATH` to the relocated `src;runtime\packages`,
and invoke the standalone executable directly. They never activate or invoke
`runtime\venv`. In the standalone process `sys.prefix == sys.base_prefix` is
expected; that is a normal, non-venv interpreter.

Phase 3, Phase 4A, the Phase 4B candidate, and the Phase 5A/5B OCR foundation
use the existing project-local runtime. Phase 6 cleaning/catalog and Phase 7A
semantic infrastructure add no package, OCR, or model payload. Phase 7A uses
only Python standard library code for configuration, validation, and its
disabled HTTP adapter. The packages are installed into `runtime\packages`
with project-private uv, the locked
versions/hashes, and a wheel-only constraint. `scripts\bootstrap.ps1` performs
the install in a temporary project-local staging venv, removes stale target
payload, overlays the contrib OpenCV wheel last, and publishes the resulting
site-packages payload:

Phase 8 also adds no Python dependency. Its production server uses only
`http.server`, `threading`, `urllib`, and the existing catalog/extraction
services. Node 22.14.0 and npm 10.9.2 were provisioned only as project-local
development tools under `runtime\node-dev` because this machine had no Node
on PATH. They are ignored and are not part of the production runtime contract;
`frontend/dist` is the required release payload. npm's cache is redirected to
`cache\npm`, no system PATH is changed, and the final server never runs npm or
Vite.

```text
runtime\uv\uv.exe pip install --python cache\temp\runtime-provision-staging\Scripts\python.exe --only-binary=:all: --exact duckdb==1.5.5 polars==1.44.1 python-calamine==0.8.2 PyMuPDF==1.28.2 img2table==2.0.0 rapidocr==3.9.2 onnxruntime==1.29.0 omegaconf==2.0.6
```

| Package | Version | Source/constraint |
| --- | --- | --- |
| `duckdb` | `1.5.5` | `uv.lock`, Windows x64 wheel hash `sha256:9f4287f97ccf0c1f3d471e7115be2b067cbf99627e2d34bffd462dd64703cddc` |
| `polars` | `1.44.1` | Pure Python wheel; hash `sha256:1fa62fc1c88fba77a68b28291b5aabdd69e5f38b34e59721a064ae3169b59bb5`. |
| `polars-runtime-32` | `1.44.1` | Required CPython ABI3 Windows x64 wheel; hash `sha256:159334184e6fbb074c9f4692221ea19970a5e2bed2a479f9d7bdb00b7f3eedb9`. |
| `python-calamine` | `0.8.2` | CPython 3.11 Windows x64 wheel; hash `sha256:c94abc66f8b544e5fc126dfaa6b41b77a394adfe09dac95e20679823e41e38be`. |
| `PyMuPDF` | `1.28.2` | CPython 3.10+ ABI3 Windows x64 wheel; hash `sha256:ebd244918798502d7b4504c90410d1711a4d7675a32584ca30f1bab419ecbffe`; no mandatory Python dependencies. |
| `img2table` | `2.0.0` | CPython 3.11 Windows x64 wheel; candidate only; OCR extras not installed. |
| `numpy` | `2.4.6` | Windows x64 wheel; transitive candidate dependency. |
| `opencv-contrib-python` | `5.0.0.93` | CPython 3.7+ ABI3 Windows x64 wheel; transitive native candidate dependency. |
| `pypdfium2` | `5.13.0` | Python 3.11 Windows x64 wheel; PDF rendering dependency. |
| `beautifulsoup4` | `4.15.0` | Pure Python transitive candidate dependency. |
| `soupsieve` | `2.9.2` | Pure Python transitive candidate dependency. |
| `typing-extensions` | `4.16.0` | Pure Python transitive candidate dependency. |
| `XlsxWriter` | `3.2.9` | Pure Python transitive candidate dependency. |
| `rapidocr` | `3.9.2` | Pure Python OCR adapter; explicit local models are required. |
| `onnxruntime` | `1.29.0` | CPython 3.11 Windows x64 CPU wheel. |
| `omegaconf` | `2.0.6` | RapidOCR configuration dependency pinned for Windows wheel compatibility. |
| `opencv-python` | `5.0.0.93` | RapidOCR dependency; the same-version `cv2` payload is shared with the candidate. |
| `Pillow`, `Shapely`, `pyclipper`, `requests`, `PyYAML`, `tqdm`, `colorlog`, `certifi`, `charset-normalizer`, `idna`, `urllib3`, `flatbuffers`, `protobuf`, `packaging`, `colorama`, `six` | pinned in `uv.lock` | RapidOCR/ONNX transitive wheels. |

On 2026-09-02 the installed `runtime\packages` payload measured **546,149,953
bytes (520.85 MiB)** after the Phase 5A refresh. The Phase 4B baseline was
**479,187,499 bytes (456.99 MiB)**, so the OCR package increment is
**66,962,454 bytes (63.86 MiB)**. The separate audited model bundle under
`runtime\models\ocr` is **31,750,473 bytes (30.28 MiB, including its manifest)**;
the combined Phase 5A increment is therefore **98,712,927 bytes (94.14 MiB)**.
The package measurement excludes generated `__pycache__` files and the
target-install lock marker. The wheel-only audit resolved 33 distributions for
the complete production payload; every selected artifact had a compatible
Windows x64 wheel and provisioning performed no source build or local
compilation. This is an observed portability cost, not a release-size
guarantee; final packaging must repeat it after cleanup/compression.

The wheel-only audit selected these Windows x64 artifacts for the candidate
tree (the remaining candidate dependencies are pure-Python wheels):

| Distribution | Selected wheel | Wheel bytes | SHA-256 |
| --- | --- | ---: | --- |
| `img2table` | `img2table-2.0.0-cp311-cp311-win_amd64.whl` | 437,699 | `40b571764571d883e062d90e8af041abb284d361a8a15dfe3fcc0bbc001c371d` |
| `numpy` | `numpy-2.4.6-cp311-cp311-win_amd64.whl` | 12,608,406 | `1e254a00cdf42b1e4d5b3d68d33af63268d41340d8885df2ab6470f2e1500147` |
| `opencv-contrib-python` | `opencv_contrib_python-5.0.0.93-cp37-abi3-win_amd64.whl` | 53,822,579 | `461622db95c964652d4d8fda171034961c3de270f78a6095aaad31050771774a` |
| `pypdfium2` | `pypdfium2-5.13.0-py3-none-win_amd64.whl` | 3,885,553 | `47dcca2a8d507b5fd24f94c3c9d48fb379430f097bc20f01beff6c963ffbcedb` |

The Phase 4A Windows x64 wheel is `pymupdf-1.28.2-cp310-abi3-win_amd64.whl`
(19,826,532 bytes). The observed installed payload is approximately 50.4 MB
under `runtime\packages\pymupdf`, including the native `mupdfcpp64.dll` and
`_mupdf.pyd`; the compatibility `fitz` package and dist-info are beside it.
Provisioning used `--only-binary=:all:` and completed without a local compiler.
PyMuPDF is dual-licensed (GNU AGPL v3 or Artifex commercial terms); the final
offline bundle must record the applicable license choice before release.

The package payload is ignored by Git; only `runtime\packages\.gitkeep` is
intended to be tracked. Future production dependencies must follow the same rule and be
verified by portable doctor. The package directory is intentionally a target
installation rather than an editable install, so it contains no reference to
the original checkout path.

Phase 5A adds three RapidOCR model files under `runtime\models\ocr`:
`PP-OCRv6_det_small.onnx`, `ch_ppocr_mobile_v2.0_cls_mobile.onnx`, and
`PP-OCRv6_rec_small.onnx`, plus `manifest.json` with file sizes and SHA-256
digests. The current measured model payload is 31,749,509 bytes (30.28 MiB).
The adapter always passes these paths explicitly; a missing or modified model
fails doctor and OCR rather than triggering a network download. The bootstrap
uses a project-local staging venv and publishes its wheel payload into
`runtime\packages` to avoid the Windows target-installer trampoline-lock issue.

All cache/temp variables remain process-local and are rooted below the current
project directory. Relocating the bundle therefore re-anchors the runtime,
package imports, registry, logs, and caches to the new root. The standalone
CPython payload itself is the uv-managed `python-build-standalone` family
recorded above; a release build must still archive and verify the complete
payload before distribution.

## Process-local containment

[`scripts/env.ps1`](../scripts/env.ps1) is compatible with Windows PowerShell 5.1. It derives the root from `$PSScriptRoot`, creates only project-local cache/temp subdirectories, and changes only the current process environment. It never calls `setx`, edits `PATH`, edits the registry, or writes user/system environment settings.

The script controls at least:

```text
UV_CACHE_DIR             E:\Desktop\ChongZu\cache\uv
PIP_CACHE_DIR            E:\Desktop\ChongZu\cache\pip
HF_HOME                  E:\Desktop\ChongZu\cache\huggingface
HUGGINGFACE_HUB_CACHE    E:\Desktop\ChongZu\cache\huggingface\hub
TMP / TEMP               E:\Desktop\ChongZu\cache\temp
PYTHONPYCACHEPREFIX      E:\Desktop\ChongZu\cache\temp\pycache
PYTHONNOUSERSITE         1
PYTHONPATH               E:\Desktop\ChongZu\src;E:\Desktop\ChongZu\runtime\packages
CHONGZU_OCR_MODELS       E:\Desktop\ChongZu\runtime\models\ocr
UV_PYTHON_INSTALL_DIR    E:\Desktop\ChongZu\runtime\python
UV_PROJECT_ENVIRONMENT   E:\Desktop\ChongZu\runtime\venv
PIP_CONFIG_FILE          E:\Desktop\ChongZu\cache\pip\pip.ini
```

`UV_NO_CONFIG=1`, `UV_LINK_MODE=copy`, `PIP_NO_INPUT=1`, and `PIP_DISABLE_PIP_VERSION_CHECK=1` are also set to reduce hidden host configuration and cache coupling. Heavy component cache/model variables will be added and verified only if a later benchmark selects the corresponding component. The Phase 4B candidate downloads no models and does not use OCR caches.

Always load `scripts\env.ps1` before invoking uv. During this preparation, two initial bare uv probes demonstrated why: without the project variables uv attempted to initialize/open its default user paths under `%LOCALAPPDATA%\uv\cache` and `%APPDATA%\uv\python`; both probes failed before creating anything. Every successful download, lock, sync, and verification command then used the project-local variables.

## Phase 1-5B Python packages

The lock file is [`uv.lock`](../uv.lock). `uv sync --locked` installs the
development/test set into `runtime\venv`; the production subset is installed
as a target into `runtime\packages` with the same pinned versions:

| Package | Version | Role |
| --- | --- | --- |
| `chongzu` | `0.1.0.dev0` | Local editable project package |
| `duckdb` | `1.5.5` | Phase 2 local registry database |
| `polars` | `1.44.1` | Phase 3 CSV/TSV and Parquet engine |
| `polars-runtime-32` | `1.44.1` | Polars native Windows x64 runtime |
| `python-calamine` | `0.8.2` | Native XLS/XLSX reader |
| `PyMuPDF` | `1.28.2` | Phase 4A native PDF text/page profiling |
| `img2table` | `2.0.0` | Phase 4B native-text PDF table candidate; OCR disabled |
| `rapidocr` | `3.9.2` | Phase 5A/5B offline CPU OCR |
| `onnxruntime` | `1.29.0` | RapidOCR CPU inference |
| `numpy` | `2.4.6` | img2table native candidate dependency |
| `opencv-contrib-python` | `5.0.0.93` | img2table native candidate dependency |
| `pypdfium2` | `5.13.0` | img2table PDF rendering dependency |
| `beautifulsoup4`, `soupsieve`, `typing-extensions`, `XlsxWriter` | pinned above | img2table transitive dependencies |
| `pytest` | `8.4.2` | Test runner |
| `colorama` | `0.4.6` | pytest Windows dependency |
| `iniconfig` | `2.3.0` | pytest dependency |
| `packaging` | `26.3` | pytest dependency |
| `pluggy` | `1.6.0` | pytest dependency |
| `pygments` | `2.21.0` | pytest dependency |

The resolved Phase 3/4A/4B/5A/5B runtime tree adds Polars, its matching runtime
wheel, python-calamine, PyMuPDF, and the candidate dependency tree above. The
wheel-only dry run and provisioning required no local compiler or source build.
PyArrow, Pandas, OpenPyXL, GMFT, Docling, Torch, Java, and Tika remain absent.
RapidOCR/ONNX Runtime are present for offline OCR only; Phase 5B injects their
OCR blocks into the existing img2table image adapter without a second OCR
backend call. NumPy/OpenCV/pypdfium2 are shared native dependencies of the
candidate and OCR trees. Phase 5B, Phase 6, and Phase 7A add no package or
model dependency.
Polars writes and reads Parquet using its bundled native runtime. PyMuPDF's
native payload is loaded from `runtime\packages\pymupdf`; the development venv
is not a production input.

Phase 3/4A/4B/5A/5B/6 processing and Phase 7A Fake semantic enrichment make no
network request and do not call a configured LLM. Provisioning is the only
network-enabled step in the current workflow. Phase 6/7A use only the already
installed Polars, DuckDB, and standard-library payload. Doctor reports
`PyMuPDF PASS`, `img2table PASS (candidate)`, `RapidOCR PASS`, `ONNX Runtime
PASS`, `OCR models PASS`, `Polars PASS`, `Calamine PASS`, `DuckDB PASS`, and
`LLM STATUS: NOT CONFIGURED / OPTIONAL` when no `.env` exists. Java/Tika is not a
default route; GMFT and Docling are not next-phase requirements and can return
only through later evidence. img2table's optional OCR extras are not installed;
Phase 5B uses its own explicit RapidOCR model bundle and never downloads models
at runtime. `process SOURCE`, `catalog`, `semantic status`, and the Phase 7A
Fake Provider only read/write project-contained workspace state; they do not
run `pip`, `uv sync`, Hugging Face, HTTP OCR, or an LLM endpoint. Phase 6/7A
introduce no runtime-size increase; final bundle-size measurement must still
be repeated after package cleanup. The future `.env` is project-local and
ignored by Git; the current Phase 7A guard rejects real provider execution
even when it is filled.

The Phase 8 server is localhost-only (`127.0.0.1`) and its process task calls
the Python coordinator directly. Frontend assets are static local files; the
API has no arbitrary path/file route. `start.cmd` records only its own server
PID below `workspace\state`, and `stop.cmd` uses that exact PID rather than
terminating all Python processes. See [WINDOWS_RUN.md](WINDOWS_RUN.md).

Phase 9 keeps the same runtime footprint and adds no package or model. Local
Search uses DuckDB catalog/chunk queries and standard-library matching; it does
not install or load FTS or any other extension. The SQL workbench uses Polars
to load selected normalized Parquet data and a fresh in-memory DuckDB connection
with `enable_external_access=false`, one thread, a 512 MiB memory setting, and
a 10 second interruptible bound. User SQL never receives the Registry
connection or an artifact path. PyArrow remains intentionally absent; temporary
relations are populated through parameterized batches.

The runtime network guard covers process, Search, SQL, and the static frontend.
No model download, pip/uv operation, Hugging Face access, HTTP OCR, external
SQL file function, or public endpoint is part of the product route. `LLM_STATUS
= NOT_CONFIGURED` remains a normal optional state.

The standalone CPython image also contains its own project-local bootstrap tools `pip==26.1.2` and `setuptools==82.0.1` under `runtime\python`; they are not system packages and are not exposed through the venv because the venv does not use system site-packages. The package build isolation uses the exact `setuptools==80.10.2` requirement declared in `pyproject.toml`.
