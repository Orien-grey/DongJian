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

