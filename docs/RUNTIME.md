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

Phase 3 production dependencies are installed into `runtime\packages` with
project-private uv, the locked versions/hashes, and a wheel-only constraint:

```text
runtime\uv\uv.exe pip install --target runtime\packages --python runtime\python\cpython-3.11.15-windows-x86_64-none\python.exe --only-binary=:all: --exact duckdb==1.5.5 polars==1.44.1 python-calamine==0.8.2
```

| Package | Version | Source/constraint |
| --- | --- | --- |
| `duckdb` | `1.5.5` | `uv.lock`, Windows x64 wheel hash `sha256:9f4287f97ccf0c1f3d471e7115be2b067cbf99627e2d34bffd462dd64703cddc` |
| `polars` | `1.44.1` | Pure Python wheel; hash `sha256:1fa62fc1c88fba77a68b28291b5aabdd69e5f38b34e59721a064ae3169b59bb5`. |
| `polars-runtime-32` | `1.44.1` | Required CPython ABI3 Windows x64 wheel; hash `sha256:159334184e6fbb074c9f4692221ea19970a5e2bed2a479f9d7bdb00b7f3eedb9`. |
| `python-calamine` | `0.8.2` | CPython 3.11 Windows x64 wheel; hash `sha256:c94abc66f8b544e5fc126dfaa6b41b77a394adfe09dac95e20679823e41e38be`. |

The package payload is ignored by Git; only `runtime\packages\.gitkeep` is
intended to be tracked. Future production dependencies must follow the same rule and be
verified by portable doctor. The package directory is intentionally a target
installation rather than an editable install, so it contains no reference to
the original checkout path.

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
UV_PYTHON_INSTALL_DIR    E:\Desktop\ChongZu\runtime\python
UV_PROJECT_ENVIRONMENT   E:\Desktop\ChongZu\runtime\venv
PIP_CONFIG_FILE          E:\Desktop\ChongZu\cache\pip\pip.ini
```

`UV_NO_CONFIG=1`, `UV_LINK_MODE=copy`, `PIP_NO_INPUT=1`, and `PIP_DISABLE_PIP_VERSION_CHECK=1` are also set to reduce hidden host configuration and cache coupling. Heavy component cache/model variables will be added and verified only if a later benchmark selects the corresponding component.

Always load `scripts\env.ps1` before invoking uv. During this preparation, two initial bare uv probes demonstrated why: without the project variables uv attempted to initialize/open its default user paths under `%LOCALAPPDATA%\uv\cache` and `%APPDATA%\uv\python`; both probes failed before creating anything. Every successful download, lock, sync, and verification command then used the project-local variables.

## Phase 1-3 Python packages

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
| `pytest` | `8.4.2` | Test runner |
| `colorama` | `0.4.6` | pytest Windows dependency |
| `iniconfig` | `2.3.0` | pytest dependency |
| `packaging` | `26.3` | pytest dependency |
| `pluggy` | `1.6.0` | pytest dependency |
| `pygments` | `2.21.0` | pytest dependency |

The resolved Phase 3 runtime tree adds only Polars, its matching runtime wheel,
and python-calamine. The wheel-only dry run and provisioning required no local
compiler or source build. PyArrow, Pandas, NumPy, OpenPyXL, PyMuPDF, RapidOCR,
ONNX Runtime, img2table, GMFT, Docling, Torch, Java, and Tika remain absent.
Polars writes and reads Parquet using its bundled native runtime.

Phase 3 processing makes no network request and does not call the configured
LLM. Provisioning is the only network-enabled step. Java/Tika is not a default
route; GMFT and Docling remain future benchmark candidates rather than
portable-bundle requirements.

The standalone CPython image also contains its own project-local bootstrap tools `pip==26.1.2` and `setuptools==82.0.1` under `runtime\python`; they are not system packages and are not exposed through the venv because the venv does not use system site-packages. The package build isolation uses the exact `setuptools==80.10.2` requirement declared in `pyproject.toml`.
