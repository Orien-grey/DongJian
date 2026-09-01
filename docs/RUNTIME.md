# Project-local runtime record

Phase 1 runtime preparation was performed on 2026-09-01 (Asia/Shanghai) for the fixed project root `E:\Desktop\ChongZu`.

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
sys.base_prefix= E:\Desktop\ChongZu\runtime\python\cpython-3.11-windows-x86_64-none
```

This is project-contained, but an ordinary Windows venv is **not claimed to be fully relocatable**. Its launcher/metadata can retain the base interpreter path. Moving the whole project directory may therefore require rebuilding `runtime\venv` from the project CPython. A genuinely relocatable release bundle is a later release-phase concern.

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
PYTHONPATH               E:\Desktop\ChongZu\src
UV_PYTHON_INSTALL_DIR    E:\Desktop\ChongZu\runtime\python
UV_PROJECT_ENVIRONMENT   E:\Desktop\ChongZu\runtime\venv
PIP_CONFIG_FILE          E:\Desktop\ChongZu\cache\pip\pip.ini
```

`UV_NO_CONFIG=1`, `UV_LINK_MODE=copy`, `PIP_NO_INPUT=1`, and `PIP_DISABLE_PIP_VERSION_CHECK=1` are also set to reduce hidden host configuration and cache coupling. Heavy component cache/model variables will be added and verified only in their later phases.

Always load `scripts\env.ps1` before invoking uv. During this preparation, two initial bare uv probes demonstrated why: without the project variables uv attempted to initialize/open its default user paths under `%LOCALAPPDATA%\uv\cache` and `%APPDATA%\uv\python`; both probes failed before creating anything. Every successful download, lock, sync, and verification command then used the project-local variables.

## Phase 1 Python packages

The lock file is [`uv.lock`](../uv.lock). `uv sync --locked` installed exactly these packages into the project venv:

| Package | Version | Role |
| --- | --- | --- |
| `chongzu` | `0.1.0.dev0` | Local editable project package |
| `pytest` | `8.4.2` | Test runner |
| `colorama` | `0.4.6` | pytest Windows dependency |
| `iniconfig` | `2.3.0` | pytest dependency |
| `packaging` | `26.3` | pytest dependency |
| `pluggy` | `1.6.0` | pytest dependency |
| `pygments` | `2.21.0` | pytest dependency |

No Polars, DuckDB, PyArrow, PyMuPDF, Docling, Torch, RapidOCR, python-calamine, openpyxl, BeautifulSoup, or Tika client was installed.

The standalone CPython image also contains its own project-local bootstrap tools `pip==26.1.2` and `setuptools==82.0.1` under `runtime\python`; they are not system packages and are not exposed through the venv because the venv does not use system site-packages. The package build isolation uses the exact `setuptools==80.10.2` requirement declared in `pyproject.toml`.
