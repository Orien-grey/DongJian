from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from chongzu import paths
from tests.xlsx_factory import write_xlsx
from tests.pdf_factory import write_pdf


def _portable_env(root: Path, *, clean_host: bool = False) -> dict[str, str]:
    """Build the same process-local environment as ``scripts/env.ps1``."""

    root = root.resolve()
    runtime = root / "runtime"
    python_dir = runtime / "python" / paths.PYTHON_RUNTIME_DIRNAME
    packages = runtime / "packages"
    cache = root / "cache"
    temp = cache / "temp"
    env = os.environ.copy()
    if clean_host:
        # Keep the host PATH for cmd.exe/PowerShell discovery, but remove all
        # inherited ChongZu and Python path hints to exercise relocation.
        for name in list(env):
            if name.startswith("CHONGZU_") or name in {
                "PYTHONPATH",
                "PYTHONHOME",
                "PYTHONNOUSERSITE",
                "PYTHONPYCACHEPREFIX",
                "VIRTUAL_ENV",
            }:
                env.pop(name, None)
    env.update(
        {
            "CHONGZU_PROJECT_ROOT": str(root),
            "CHONGZU_ROOT": str(root),
            "CHONGZU_RUNTIME_ROOT": str(runtime),
            "CHONGZU_PYTHON": str(python_dir / "python.exe"),
            "CHONGZU_RUNTIME_PYTHON": str(python_dir / "python.exe"),
            "CHONGZU_DEV_PYTHON": str(runtime / "venv" / "Scripts" / "python.exe"),
            "CHONGZU_PROJECT_PYTHON": str(python_dir / "python.exe"),
            "CHONGZU_PACKAGES": str(packages),
            "CHONGZU_SRC": str(root / "src"),
            "CHONGZU_CACHE_TEMP": str(temp),
            "CHONGZU_OCR_MODELS": str(runtime / "models" / "ocr"),
            "CHONGZU_PROJECT_UV": str(runtime / "uv" / "uv.exe"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "PYTHONPYCACHEPREFIX": str(temp / "pycache"),
            "PYTHONPATH": os.pathsep.join((str(root / "src"), str(packages))),
            "TMP": str(temp),
            "TEMP": str(temp),
            "UV_CACHE_DIR": str(cache / "uv"),
            "UV_PYTHON_INSTALL_DIR": str(runtime / "python"),
            "UV_PYTHON": str(python_dir / "python.exe"),
            "UV_MANAGED_PYTHON": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_PROJECT_ENVIRONMENT": str(runtime / "venv"),
            "UV_NO_CONFIG": "1",
            "UV_NO_PROGRESS": "1",
            "UV_LINK_MODE": "copy",
            "PIP_CACHE_DIR": str(cache / "pip"),
            "PIP_CONFIG_FILE": str(cache / "pip" / "pip.ini"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "HF_HOME": str(cache / "huggingface"),
            "HUGGINGFACE_HUB_CACHE": str(cache / "huggingface" / "hub"),
        }
    )
    for directory in (
        cache / "uv",
        cache / "pip",
        cache / "huggingface" / "hub",
        cache / "docling",
        cache / "ocr",
        cache / "tika",
        temp / "pycache",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (cache / "pip" / "pip.ini").touch(exist_ok=True)
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    return env


def _run_cmd(script: Path, args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = "call " + subprocess.list2cmdline([str(script), *args])
    return subprocess.run(
        command,
        shell=True,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=180,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_standalone_runtime_and_target_package_imports() -> None:
    env = _portable_env(paths.PROJECT_ROOT)
    code = (
        "import json,site,sys,duckdb,polars,python_calamine,chongzu; "
        "print(json.dumps({'exe':sys.executable,'prefix':sys.prefix,'base':sys.base_prefix,"
        "'duckdb':duckdb.__file__,'polars':polars.__file__,'calamine':python_calamine.__file__,"
        "'chongzu':chongzu.__file__,'user_site':site.ENABLE_USER_SITE}))"
    )
    completed = subprocess.run(
        [str(paths.PYTHON_EXE), "-c", code],
        cwd=str(paths.PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert Path(result["exe"]).resolve() == paths.PYTHON_EXE.resolve()
    assert result["prefix"] == result["base"]
    assert paths.PACKAGES_ROOT.resolve() in Path(result["duckdb"]).resolve().parents
    assert paths.PACKAGES_ROOT.resolve() in Path(result["polars"]).resolve().parents
    assert paths.PACKAGES_ROOT.resolve() in Path(result["calamine"]).resolve().parents
    assert paths.SRC_ROOT.resolve() in Path(result["chongzu"]).resolve().parents
    assert result["user_site"] is False


def test_formal_launcher_does_not_depend_on_development_venv() -> None:
    launcher = (paths.PROJECT_ROOT / "scripts" / "chongzu.ps1").read_text(encoding="utf-8").lower()
    cmd = (paths.PROJECT_ROOT / "chongzu.cmd").read_text(encoding="utf-8").lower()
    assert "venv" not in launcher
    assert "chongzu_dev_python" not in launcher
    assert "venv" not in cmd


def test_cmd_launcher_preserves_unicode_and_space_arguments(tmp_path: Path) -> None:
    source = tmp_path / "数据 source with spaces"
    source.mkdir()
    sample = source / "资料 文件.txt"
    sample.write_text("portable launcher", encoding="utf-8")
    before = _sha256(sample)
    completed = _run_cmd(
        paths.PROJECT_ROOT / "chongzu.cmd",
        ["scan", str(source)],
        paths.PROJECT_ROOT,
        _portable_env(paths.PROJECT_ROOT),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Discovered: 1" in completed.stdout
    assert _sha256(sample) == before


def _build_relocated_copy(root: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        root / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME,
        destination / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME,
    )
    shutil.copytree(root / "runtime" / "packages", destination / "runtime" / "packages")
    shutil.copytree(root / "runtime" / "uv", destination / "runtime" / "uv")
    shutil.copytree(root / "runtime" / "models" / "ocr", destination / "runtime" / "models" / "ocr")
    shutil.copytree(root / "src", destination / "src")
    shutil.copytree(root / "scripts", destination / "scripts")
    for filename in ("pyproject.toml", "uv.lock", "doctor.cmd", "chongzu.cmd"):
        shutil.copy2(root / filename, destination / filename)
    for relative in (
        "cache/uv",
        "cache/pip",
        "cache/huggingface/hub",
        "cache/docling",
        "cache/ocr",
        "cache/tika",
        "cache/temp",
        "models/ocr",
        "models/docling",
        "workspace/input",
        "workspace/staging",
        "workspace/artifacts",
        "workspace/output",
        "workspace/quarantine",
        "workspace/state",
        "workspace/logs",
    ):
        (destination / relative).mkdir(parents=True, exist_ok=True)


def test_relocated_copy_reanchors_runtime_registry_and_cache() -> None:
    root = paths.PROJECT_ROOT
    staging = root / "workspace" / "portability-test"
    destination = staging / "Moved Project"
    if destination.exists():
        shutil.rmtree(destination)
    original_registry = paths.REGISTRY_PATH
    original_registry_hash = _sha256(original_registry) if original_registry.is_file() else None
    try:
        _build_relocated_copy(root, destination)
        clean_env = _portable_env(destination, clean_host=True)
        doctor = _run_cmd(destination / "doctor.cmd", [], destination, clean_env)
        assert doctor.returncode == 0, doctor.stdout + doctor.stderr
        assert "PASS  PORTABLE RUNTIME:" in doctor.stdout
        assert str(destination).lower() in doctor.stdout.lower()

        source = destination / "workspace" / "fixture data 中文"
        source.mkdir(parents=True)
        (source / "note file.txt").write_text("moved", encoding="utf-8")
        (source / "renamed.download").write_bytes(b"%PDF-1.7\nportable")
        source_file_hashes = {_sha256(path) for path in source.iterdir()}
        scan = _run_cmd(destination / "chongzu.cmd", ["scan", str(source)], destination, clean_env)
        assert scan.returncode == 0, scan.stdout + scan.stderr
        assert "Discovered: 2" in scan.stdout
        assert "Hashed: 2" in scan.stdout
        summary = _run_cmd(destination / "chongzu.cmd", ["registry", "summary"], destination, clean_env)
        assert summary.returncode == 0, summary.stdout + summary.stderr
        summary_data = json.loads(summary.stdout)
        assert str(destination).lower() in summary_data["source_root"].lower()
        assert str(destination).lower() in summary_data["log_path"].lower()

        relocated_registry = destination / "workspace" / "state" / "registry.duckdb"
        assert relocated_registry.is_file()
        assert original_registry.resolve() != relocated_registry.resolve()
        if original_registry_hash is None:
            assert not original_registry.exists()
        else:
            assert _sha256(original_registry) == original_registry_hash
        assert source_file_hashes == {_sha256(path) for path in source.iterdir()}
        assert (destination / "cache" / "temp").is_dir()
        assert any((destination / "cache").rglob("*"))

        structured_source = destination / "workspace" / "structured fixtures 中文"
        structured_source.mkdir(parents=True)
        (structured_source / "sample.csv").write_text("name,value\n北京,12\n上海,8\n", encoding="utf-8")
        write_xlsx(
            structured_source / "book.xlsx",
            [("数据", [["name", "value"], ["alpha", 1], ["beta", 2]], None)],
        )
        structured_hashes = {_sha256(path) for path in structured_source.iterdir()}
        extraction = _run_cmd(
            destination / "chongzu.cmd",
            ["extract", "structured", str(structured_source), "--workers", "2"],
            destination,
            clean_env,
        )
        assert extraction.returncode == 0, extraction.stdout + extraction.stderr
        assert "Structured supported: 2" in extraction.stdout
        assert "Tables produced: 2" in extraction.stdout
        assert structured_hashes == {_sha256(path) for path in structured_source.iterdir()}
        catalog = _run_cmd(
            destination / "chongzu.cmd",
            ["registry", "summary", "--source", str(structured_source)],
            destination,
            clean_env,
        )
        assert catalog.returncode == 0, catalog.stdout + catalog.stderr
        catalog_data = json.loads(catalog.stdout)
        assert catalog_data["catalog"]["table_assets"] == 2
        assert catalog_data["catalog"]["table_rows"] == 4

        pdf_source = destination / "workspace" / "pdf fixtures 中文"
        pdf_source.mkdir(parents=True)
        write_pdf(pdf_source / "native.pdf", [{"texts": [(72, 72, "relocated native text")]}])
        pdf_hashes = {_sha256(path) for path in pdf_source.iterdir()}
        pdf_extraction = _run_cmd(
            destination / "chongzu.cmd",
            ["extract", "pdf", str(pdf_source)],
            destination,
            clean_env,
        )
        assert pdf_extraction.returncode == 0, pdf_extraction.stdout + pdf_extraction.stderr
        assert "PDF files: 1" in pdf_extraction.stdout
        assert "Text assets produced: 1" in pdf_extraction.stdout
        assert pdf_hashes == {_sha256(path) for path in pdf_source.iterdir()}
        pdf_catalog = _run_cmd(
            destination / "chongzu.cmd",
            ["registry", "summary", "--source", str(pdf_source)],
            destination,
            clean_env,
        )
        assert pdf_catalog.returncode == 0, pdf_catalog.stdout + pdf_catalog.stderr
        pdf_catalog_data = json.loads(pdf_catalog.stdout)
        assert pdf_catalog_data["catalog"]["text_assets"] == 1
        assert pdf_catalog_data["catalog"]["text_chunks"] == 1

        table_source = destination / "workspace" / "pdf table fixtures"
        table_source.mkdir(parents=True)
        table_pdf = table_source / "native-table.pdf"
        table_pdf_spec = {
            "texts": [
                (100, 150, "A"), (200, 150, "B"), (300, 150, "C"),
                (100, 180, "1"), (200, 180, "2"), (300, 180, "3"),
                (100, 210, "4"), (200, 210, "5"), (300, 210, "6"),
            ],
            "lines": [
                (90, 120, 330, 120), (90, 160, 330, 160),
                (90, 190, 330, 190), (90, 220, 330, 220),
                (90, 120, 90, 220), (170, 120, 170, 220),
                (270, 120, 270, 220), (330, 120, 330, 220),
            ],
        }
        write_pdf(table_pdf, [table_pdf_spec])
        table_hash = _sha256(table_pdf)
        table_extraction = _run_cmd(
            destination / "chongzu.cmd",
            ["benchmark", "pdf-table", str(table_source), "--workers", "1", "--force"],
            destination,
            clean_env,
        )
        assert table_extraction.returncode == 0, table_extraction.stdout + table_extraction.stderr
        assert "Table assets: 1" in table_extraction.stdout
        assert "OCR: disabled" in table_extraction.stdout
        assert _sha256(table_pdf) == table_hash

        ocr_source = destination / "workspace" / "ocr fixtures"
        ocr_source.mkdir(parents=True, exist_ok=True)
        from PIL import Image, ImageDraw

        ocr_image = ocr_source / "image with spaces.png"
        image = Image.new("RGB", (800, 260), "white")
        ImageDraw.Draw(image).text((35, 90), "Moved OCR 123", fill="black")
        image.save(ocr_image)
        ocr_hash = _sha256(ocr_image)
        ocr_extraction = _run_cmd(
            destination / "chongzu.cmd",
            ["extract", "ocr", str(ocr_source), "--workers", "1", "--force"],
            destination,
            clean_env,
        )
        assert ocr_extraction.returncode == 0, ocr_extraction.stdout + ocr_extraction.stderr
        assert "Text assets produced: 1" in ocr_extraction.stdout
        assert "Failures: 0" in ocr_extraction.stdout
        assert _sha256(ocr_image) == ocr_hash

        probe_code = (
            "import duckdb,json,polars,python_calamine,pymupdf,img2table,numpy,cv2,pypdfium2,rapidocr,onnxruntime,sys,pathlib; "
            "c=duckdb.connect('workspace/state/registry.duckdb'); "
            "p=c.execute(\"select normalized_artifact_path from table_assets where is_current=true limit 1\").fetchone()[0]; "
            "f=polars.read_parquet(pathlib.Path('workspace')/p); "
            "print(json.dumps({'exe':sys.executable,'duckdb':duckdb.__file__,'polars':polars.__file__,"
            "'calamine':python_calamine.__file__,'pymupdf':pymupdf.__file__,"
                         "'img2table':img2table.__file__,'numpy':numpy.__file__,'cv2':cv2.__file__,'pypdfium2':pypdfium2.__file__,"
                         "'rapidocr':rapidocr.__file__,'onnxruntime':onnxruntime.__file__,"
            "'text':c.execute(\"select normalized_artifact_path from text_assets where is_current=true and source_relative_path='native.pdf'\").fetchone()[0],"
            "'rows':f.height}))"
        )
        probe = subprocess.run(
            [str(destination / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME / "python.exe"), "-c", probe_code],
            cwd=str(destination),
            env=clean_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert probe.returncode == 0, probe.stdout + probe.stderr
        probe_result = json.loads(probe.stdout.strip().splitlines()[-1])
        assert probe_result["rows"] == 2
        assert Path(probe_result["exe"]).resolve() == destination / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME / "python.exe"
        for module_name in ("duckdb", "polars", "calamine", "pymupdf", "img2table", "numpy", "cv2", "pypdfium2", "rapidocr", "onnxruntime"):
            module_path = Path(probe_result[module_name]).resolve()
            assert module_path.is_relative_to((destination / "runtime" / "packages").resolve())
            assert "appdata" not in str(module_path).casefold()
        text_artifact = destination / "workspace" / probe_result["text"]
        assert text_artifact.is_file()
        assert "relocated native text" in text_artifact.read_text(encoding="utf-8")
        ocr_code = (
            "import duckdb,json,pathlib; "
            "c=duckdb.connect('workspace/state/registry.duckdb'); "
            "r=c.execute(\"select normalized_artifact_path from text_assets where extractor='rapidocr-onnx' limit 1\").fetchone()[0]; "
            "print(json.dumps({'artifact':r,'text':(pathlib.Path('workspace')/r).read_text(encoding='utf-8')}))"
        )
        ocr_probe = subprocess.run(
            [str(destination / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME / "python.exe"), "-c", ocr_code],
            cwd=str(destination),
            env=clean_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert ocr_probe.returncode == 0, ocr_probe.stdout + ocr_probe.stderr
        ocr_result = json.loads(ocr_probe.stdout.strip().splitlines()[-1])
        assert ocr_result["artifact"].startswith("artifacts/text/")
        assert "Moved" in ocr_result["text"]
    finally:
        if destination.exists():
            shutil.rmtree(destination)
        if staging.exists() and not any(staging.iterdir()):
            staging.rmdir()
