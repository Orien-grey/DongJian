from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from chongzu import paths


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
        "import json,site,sys,duckdb,chongzu; "
        "print(json.dumps({'exe':sys.executable,'prefix':sys.prefix,'base':sys.base_prefix,"
        "'duckdb':duckdb.__file__,'chongzu':chongzu.__file__,'user_site':site.ENABLE_USER_SITE}))"
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

        probe = subprocess.run(
            [str(destination / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME / "python.exe"), "-c", "import duckdb,sys; print(sys.executable); print(duckdb.__file__)"],
            cwd=str(destination),
            env=clean_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode == 0, probe.stdout + probe.stderr
        probe_lines = probe.stdout.strip().splitlines()
        assert str(destination).lower() in probe_lines[0].lower()
        assert str(destination / "runtime" / "packages").lower() in probe_lines[1].lower()
        assert Path(probe_lines[0]).resolve() == destination / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME / "python.exe"
        assert Path(probe_lines[1]).resolve().is_relative_to((destination / "runtime" / "packages").resolve())
    finally:
        if destination.exists():
            shutil.rmtree(destination)
        if staging.exists() and not any(staging.iterdir()):
            staging.rmdir()
