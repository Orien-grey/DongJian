import os
import subprocess
from pathlib import Path

from chongzu import doctor, paths


def test_doctor_passes_in_project_environment() -> None:
    report = doctor.run_checks()
    assert report.ok, [check for check in report.checks if check.status == "FAIL"]


def test_current_python_is_project_local_and_not_miniconda() -> None:
    executable = Path(__import__("sys").executable).resolve()
    assert paths.is_within_project(executable)
    assert "miniconda" not in str(executable).lower()
    assert "anaconda" not in str(executable).lower()


def test_phase_one_heavy_tools_are_nonfatal_information() -> None:
    report = doctor.run_checks()
    for name in ("Java/Tika", "Docling", "OCR/RapidOCR"):
        checks = [check for check in report.checks if check.name == name]
        assert checks and checks[0].status == "INFO"
        assert checks[0].fatal is False


def test_portable_doctor_passes_with_standalone_python() -> None:
    root = paths.PROJECT_ROOT
    env = os.environ.copy()
    env.update(
        {
            "CHONGZU_PROJECT_ROOT": str(root),
            "CHONGZU_ROOT": str(root),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join((str(paths.SRC_ROOT), str(paths.PACKAGES_ROOT))),
            "PYTHONPYCACHEPREFIX": str(paths.PYTHON_BYTECODE_CACHE),
            "TMP": str(paths.TEMP_ROOT),
            "TEMP": str(paths.TEMP_ROOT),
            "UV_CACHE_DIR": str(paths.UV_CACHE_DIR),
            "UV_PYTHON": str(paths.PYTHON_EXE),
            "UV_PYTHON_INSTALL_DIR": str(paths.PYTHON_RUNTIME_ROOT),
            "UV_MANAGED_PYTHON": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    completed = subprocess.run(
        [str(paths.PYTHON_EXE), "-m", "chongzu", "doctor"],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PASS  PORTABLE RUNTIME:" in completed.stdout
    assert "PASS  conda/miniconda exclusion:" in completed.stdout
