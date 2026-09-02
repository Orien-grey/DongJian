"""Centralized project paths.

No directory is created when this module is imported. Callers can therefore use
these constants for read-only diagnostics as well as for later pipeline stages.
"""

from __future__ import annotations

import os
from pathlib import Path

PYTHON_VERSION = "3.11.15"
PYTHON_RUNTIME_DIRNAME = f"cpython-{PYTHON_VERSION}-windows-x86_64-none"
PROJECT_UV_VERSION = "0.11.21"
PROJECT_UV_SHA256 = "5a7ec85884c2ccb1be560cb8fac3eb890df1adf49bfcc070a270ba70401bdd68"
DUCKDB_VERSION = "1.5.5"
POLARS_VERSION = "1.44.1"
PYTHON_CALAMINE_VERSION = "0.8.2"
PYMUPDF_VERSION = "1.28.2"
IMG2TABLE_VERSION = "2.0.0"
NUMPY_VERSION = "2.4.6"
OPENCV_CONTRIB_VERSION = "5.0.0.93"
PYPDFIUM2_VERSION = "5.13.0"
BEAUTIFULSOUP4_VERSION = "4.15.0"
SOUPSIEVE_VERSION = "2.9.2"
TYPING_EXTENSIONS_VERSION = "4.16.0"
XLSXWRITER_VERSION = "3.2.9"
RAPIDOCR_VERSION = "3.9.2"
ONNXRUNTIME_VERSION = "1.29.0"
PIPELINE_VERSION = "phase4a-pdf-native-text"
PDF_TABLE_PIPELINE_VERSION = "phase4b-pdf-table-candidate"
OCR_PIPELINE_VERSION = "phase5b-rapidocr-image-table"
UNIFIED_PIPELINE_VERSION = "phase5b-unified-extraction"
TEXT_PIPELINE_VERSION = "phase5b-plain-text"
IMAGE_TABLE_PIPELINE_VERSION = "phase5b-image-table-ocrdata"
STRUCTURED_CONFIG_VERSION = "structured-v1"
PDF_CONFIG_VERSION = "pdf-native-text-v1"
TEXT_CHUNK_CONFIG_VERSION = "text-chunk-v1"
PDF_TABLE_CONFIG_VERSION = "pdf-table-v1"
OCR_CONFIG_VERSION = "ocr-v2"
TEXT_CONFIG_VERSION = "text-v1"
IMAGE_TABLE_CONFIG_VERSION = "image-table-v1"
REGISTRY_SCHEMA_NAME = "chongzu_file_registry"
REGISTRY_SCHEMA_VERSION = 3


def _discover_project_root() -> Path:
    """Find the repository root without relying on the current working directory."""

    configured = os.environ.get("CHONGZU_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()

    here = Path(__file__).resolve()
    candidates = (here, *here.parents)
    for candidate in candidates:
        # Runtime copies may intentionally omit repository documentation.  A
        # runnable root is identified by its source and runtime trees rather
        # than by a Git-only file.
        if (candidate / "runtime").is_dir() and (candidate / "src").is_dir():
            return candidate

    # This fallback keeps import errors understandable in a partially prepared
    # checkout; doctor will report the missing/invalid root explicitly.
    return here.parents[2]


PROJECT_ROOT = _discover_project_root()

RUNTIME_ROOT = PROJECT_ROOT / "runtime"
PYTHON_RUNTIME_ROOT = RUNTIME_ROOT / "python"
PYTHON_RUNTIME_DIR = PYTHON_RUNTIME_ROOT / PYTHON_RUNTIME_DIRNAME
PYTHON_EXE = PYTHON_RUNTIME_DIR / "python.exe"
SRC_ROOT = PROJECT_ROOT / "src"
PACKAGES_ROOT = RUNTIME_ROOT / "packages"
UV_ROOT = RUNTIME_ROOT / "uv"
UV_EXE = UV_ROOT / "uv.exe"
VENV_ROOT = RUNTIME_ROOT / "venv"
VENV_PYTHON_EXE = VENV_ROOT / "Scripts" / "python.exe"

CACHE_ROOT = PROJECT_ROOT / "cache"
UV_CACHE_DIR = CACHE_ROOT / "uv"
PIP_CACHE_DIR = CACHE_ROOT / "pip"
HUGGINGFACE_HOME = CACHE_ROOT / "huggingface"
HUGGINGFACE_HUB_CACHE = HUGGINGFACE_HOME / "hub"
DOCLING_CACHE_DIR = CACHE_ROOT / "docling"
OCR_CACHE_DIR = CACHE_ROOT / "ocr"
TIKA_CACHE_DIR = CACHE_ROOT / "tika"
TEMP_ROOT = CACHE_ROOT / "temp"
PYTHON_BYTECODE_CACHE = TEMP_ROOT / "pycache"
PIP_CONFIG_FILE = PIP_CACHE_DIR / "pip.ini"

# OCR models are part of the portable runtime payload.  Keeping them beside
# ``runtime\packages`` makes the bundle self-contained after it is moved to a
# different Windows directory.  Docling remains a future candidate and keeps
# its historical project-level placeholder for now.
MODELS_ROOT = PROJECT_ROOT / "models"
RUNTIME_MODELS_ROOT = RUNTIME_ROOT / "models"
OCR_MODELS_ROOT = RUNTIME_MODELS_ROOT / "ocr"
DOCLING_MODELS_ROOT = MODELS_ROOT / "docling"

WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
ARTIFACTS_ROOT = WORKSPACE_ROOT / "artifacts"
TABLE_ARTIFACTS_ROOT = ARTIFACTS_ROOT / "tables"
SHEET_ARTIFACTS_ROOT = ARTIFACTS_ROOT / "sheets"
INPUT_ROOT = WORKSPACE_ROOT / "input"
STAGING_ROOT = WORKSPACE_ROOT / "staging"
OUTPUT_ROOT = WORKSPACE_ROOT / "output"
QUARANTINE_ROOT = WORKSPACE_ROOT / "quarantine"
STATE_ROOT = WORKSPACE_ROOT / "state"
LOGS_ROOT = WORKSPACE_ROOT / "logs"
REGISTRY_PATH = STATE_ROOT / "registry.duckdb"

CORE_DIRECTORIES = {
    "project_root": PROJECT_ROOT,
    "runtime": RUNTIME_ROOT,
    "runtime_python": PYTHON_RUNTIME_ROOT,
    "runtime_python_standalone": PYTHON_RUNTIME_DIR,
    "runtime_packages": PACKAGES_ROOT,
    "runtime_uv": UV_ROOT,
    "runtime_venv": VENV_ROOT,
    "cache": CACHE_ROOT,
    "cache_uv": UV_CACHE_DIR,
    "cache_pip": PIP_CACHE_DIR,
    "cache_huggingface": HUGGINGFACE_HOME,
    "cache_huggingface_hub": HUGGINGFACE_HUB_CACHE,
    "cache_docling": DOCLING_CACHE_DIR,
    "cache_ocr": OCR_CACHE_DIR,
    "cache_tika": TIKA_CACHE_DIR,
    "cache_temp": TEMP_ROOT,
    "cache_python_bytecode": PYTHON_BYTECODE_CACHE,
    "models": MODELS_ROOT,
    "runtime_models": RUNTIME_MODELS_ROOT,
    "models_ocr": OCR_MODELS_ROOT,
    "models_docling": DOCLING_MODELS_ROOT,
    "workspace": WORKSPACE_ROOT,
    "workspace_artifacts": ARTIFACTS_ROOT,
    "workspace_input": INPUT_ROOT,
    "workspace_staging": STAGING_ROOT,
    "workspace_output": OUTPUT_ROOT,
    "workspace_quarantine": QUARANTINE_ROOT,
    "workspace_state": STATE_ROOT,
    "workspace_logs": LOGS_ROOT,
}

# These are the process-local settings established by scripts/env.ps1. The
# values are intentionally Path objects so doctor and future launchers can
# apply one containment check to every setting.
CONTROLLED_ENV_PATHS = {
    "CHONGZU_PROJECT_ROOT": PROJECT_ROOT,
    "CHONGZU_ROOT": PROJECT_ROOT,
    "CHONGZU_RUNTIME_ROOT": RUNTIME_ROOT,
    "CHONGZU_PYTHON": PYTHON_EXE,
    "CHONGZU_DEV_PYTHON": VENV_PYTHON_EXE,
    "CHONGZU_PACKAGES": PACKAGES_ROOT,
    "CHONGZU_SRC": SRC_ROOT,
    "CHONGZU_CACHE_TEMP": TEMP_ROOT,
    "CHONGZU_OCR_MODELS": OCR_MODELS_ROOT,
    "UV_CACHE_DIR": UV_CACHE_DIR,
    "PIP_CACHE_DIR": PIP_CACHE_DIR,
    "HF_HOME": HUGGINGFACE_HOME,
    "HUGGINGFACE_HUB_CACHE": HUGGINGFACE_HUB_CACHE,
    "TMP": TEMP_ROOT,
    "TEMP": TEMP_ROOT,
    "PYTHONPYCACHEPREFIX": PYTHON_BYTECODE_CACHE,
    "UV_PYTHON_INSTALL_DIR": PYTHON_RUNTIME_ROOT,
    "UV_PYTHON": PYTHON_EXE,
    "UV_PROJECT_ENVIRONMENT": VENV_ROOT,
    "PIP_CONFIG_FILE": PIP_CONFIG_FILE,
}


def is_within_project(path: Path) -> bool:
    """Return whether *path* resolves below ``PROJECT_ROOT``."""

    try:
        Path(path).resolve().relative_to(PROJECT_ROOT)
    except (OSError, ValueError):
        return False
    return True


def ensure_within_project(path: Path) -> Path:
    """Resolve a path and raise if it would escape the project root."""

    resolved = Path(path).resolve()
    if not is_within_project(resolved):
        raise ValueError(f"Path escapes project root: {resolved}")
    return resolved


def project_path(*parts: str | os.PathLike[str]) -> Path:
    """Join path components below the project root with containment checking."""

    return ensure_within_project(PROJECT_ROOT.joinpath(*parts))
