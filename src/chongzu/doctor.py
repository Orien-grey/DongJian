"""Environment and containment diagnostics for the project-local runtime."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import site
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import paths


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fatal: bool = False


class DoctorReport:
    """Collected checks and the process exit decision."""

    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, name: str, status: str, detail: str, *, fatal: bool = False) -> None:
        self.checks.append(Check(name=name, status=status, detail=detail, fatal=fatal))

    @property
    def ok(self) -> bool:
        return not any(check.fatal and check.status == "FAIL" for check in self.checks)


def _normalized(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _path_values(value: str) -> Iterable[Path]:
    for part in value.split(os.pathsep):
        if part:
            yield Path(part).expanduser()


def _controlled_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(paths.UV_CACHE_DIR)
    env["UV_NO_CONFIG"] = "1"
    env["UV_NO_PROGRESS"] = "1"
    env["UV_PYTHON_INSTALL_DIR"] = str(paths.PYTHON_RUNTIME_ROOT)
    env["UV_PYTHON"] = str(paths.PYTHON_EXE)
    env["UV_MANAGED_PYTHON"] = "1"
    env["UV_PYTHON_DOWNLOADS"] = "never"
    env["PYTHONNOUSERSITE"] = "1"
    env["TMP"] = str(paths.TEMP_ROOT)
    env["TEMP"] = str(paths.TEMP_ROOT)
    return env


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_runtime(report: DoctorReport) -> None:
    executable = Path(sys.executable).resolve()
    portable_executable = paths.PYTHON_EXE.resolve()
    development_executable = paths.VENV_PYTHON_EXE.resolve()
    is_portable = executable == portable_executable
    is_development = executable == development_executable
    if executable.is_file() and paths.is_within_project(executable):
        report.add("python executable", "PASS", str(executable))
    else:
        report.add(
            "python executable",
            "FAIL",
            f"{executable} is outside the project root",
            fatal=True,
        )

    if is_portable:
        report.add("portable Python executable", "PASS", str(executable))
    elif is_development:
        report.add(
            "portable Python executable",
            "FAIL",
            f"development venv active; production requires {portable_executable}",
        )
    else:
        report.add(
            "portable Python executable",
            "FAIL",
            f"active interpreter is {executable}; production requires {portable_executable}",
            fatal=True,
        )

    version = ".".join(str(part) for part in sys.version_info[:3])
    if version == paths.PYTHON_VERSION:
        report.add("python version", "PASS", version)
    else:
        report.add("python version", "FAIL", f"{version}; required {paths.PYTHON_VERSION}", fatal=True)

    lowered = _normalized(executable)
    if "miniconda" in lowered or "anaconda" in lowered:
        report.add("conda/miniconda exclusion", "FAIL", str(executable), fatal=True)
    else:
        report.add("conda/miniconda exclusion", "PASS", "executable path is not Conda/Miniconda")

    prefixes = {"sys.prefix": Path(sys.prefix), "sys.base_prefix": Path(sys.base_prefix)}
    for name, prefix in prefixes.items():
        if paths.is_within_project(prefix):
            report.add(name, "PASS", str(prefix.resolve()))
        else:
            report.add(name, "FAIL", f"outside project root: {prefix}", fatal=True)

    if is_portable and sys.prefix == sys.base_prefix:
        report.add("portable sys.prefix", "PASS", "sys.prefix == sys.base_prefix (standalone runtime)")
    elif is_development:
        report.add(
            "portable sys.prefix",
            "FAIL",
            "development venv has a distinct sys.prefix; this is expected only for development",
        )
    else:
        report.add(
            "portable sys.prefix",
            "FAIL",
            f"sys.prefix={sys.prefix}; sys.base_prefix={sys.base_prefix}",
            fatal=True,
        )

    search_paths = [Path(entry) for entry in sys.path if entry]
    outside = [str(entry) for entry in search_paths if not paths.is_within_project(entry)]
    external_conda = [entry for entry in outside if "conda" in _normalized(entry) or "anaconda" in _normalized(entry)]
    if external_conda:
        report.add("python search paths", "FAIL", "; ".join(external_conda), fatal=True)
    elif outside:
        report.add("python search paths", "FAIL", "; ".join(outside), fatal=True)
    else:
        report.add("python search paths", "PASS", "all non-empty sys.path entries are project-local")

    pythonhome = os.environ.get("PYTHONHOME")
    if not pythonhome:
        report.add("PYTHONHOME", "PASS", "unset")
    elif all(paths.is_within_project(value) for value in _path_values(pythonhome)):
        report.add("PYTHONHOME", "PASS", pythonhome)
    else:
        report.add("PYTHONHOME", "FAIL", f"outside project root: {pythonhome}", fatal=True)

    pythonpath = os.environ.get("PYTHONPATH")
    if pythonpath and all(paths.is_within_project(value) for value in _path_values(pythonpath)):
        report.add("PYTHONPATH", "PASS", pythonpath)
    elif pythonpath:
        report.add("PYTHONPATH", "FAIL", f"outside project root: {pythonpath}", fatal=True)
    else:
        report.add("PYTHONPATH", "FAIL", "not set; project package path is not explicit", fatal=True)

    if os.environ.get("PYTHONNOUSERSITE", "") == "1":
        report.add("PYTHONNOUSERSITE", "PASS", "1")
    else:
        report.add("PYTHONNOUSERSITE", "FAIL", "must be 1", fatal=True)

    if os.environ.get("UV_MANAGED_PYTHON", "") == "1":
        report.add("UV_MANAGED_PYTHON", "PASS", "1")
    else:
        report.add("UV_MANAGED_PYTHON", "FAIL", "must be 1", fatal=True)
    if os.environ.get("UV_PYTHON_DOWNLOADS", "") == "never":
        report.add("UV_PYTHON_DOWNLOADS", "PASS", "never")
    else:
        report.add("UV_PYTHON_DOWNLOADS", "FAIL", "must be never during project execution", fatal=True)

    user_site = site.getusersitepackages()
    if site.ENABLE_USER_SITE is False and (not user_site or user_site not in sys.path):
        report.add("user site-packages", "PASS", "disabled and absent from sys.path")
    else:
        report.add("user site-packages", "FAIL", f"enabled or visible: {user_site}", fatal=True)


def _check_directories(report: DoctorReport) -> None:
    for name, directory in paths.CORE_DIRECTORIES.items():
        if not paths.is_within_project(directory):
            report.add(name, "FAIL", f"outside project root: {directory}", fatal=True)
        elif not directory.is_dir():
            if name == "runtime_venv":
                report.add(
                    name,
                    "INFO",
                    "development-only venv is absent; portable runtime does not require it",
                )
                continue
            report.add(name, "FAIL", f"missing directory: {directory}", fatal=True)
        elif os.access(str(directory), os.W_OK):
            report.add(name, "PASS", f"exists and is writable: {directory}")
        else:
            report.add(name, "FAIL", f"not writable: {directory}", fatal=True)


def _check_environment_paths(report: DoctorReport) -> None:
    for name, expected in paths.CONTROLLED_ENV_PATHS.items():
        value = os.environ.get(name)
        if not value:
            report.add(name, "FAIL", "not set", fatal=True)
            continue
        values = list(_path_values(value))
        if all(paths.is_within_project(item) for item in values):
            report.add(name, "PASS", value)
        else:
            report.add(name, "FAIL", f"outside project root: {value}", fatal=True)

        # For single-path variables, also ensure the launcher points at the
        # designated project location rather than merely another local path.
        if name not in {"TMP", "TEMP"} and len(values) == 1:
            try:
                if values[0].resolve() != expected.resolve():
                    report.add(name + " target", "FAIL", f"expected {expected}, got {values[0]}", fatal=True)
            except OSError as exc:
                report.add(name + " target", "FAIL", str(exc), fatal=True)


def _check_portable_imports(report: DoctorReport) -> None:
    """Verify imports and executable selection for the production runtime.

    The development venv is intentionally allowed to report non-fatal FAIL
    entries so existing developer checks remain useful.  Only the standalone
    interpreter can produce a portable PASS, and any failure there is fatal.
    """

    executable = Path(sys.executable).resolve()
    portable_executable = paths.PYTHON_EXE.resolve()
    development_executable = paths.VENV_PYTHON_EXE.resolve()
    is_portable = executable == portable_executable
    is_development = executable == development_executable
    strict = is_portable or not is_development
    failures: list[str] = []

    if is_portable:
        report.add("PATH Python", "PASS", "launcher invoked the explicit project standalone executable")
    elif is_development:
        report.add("PATH Python", "FAIL", "development venv active; launchers must use standalone Python")
        failures.append("PATH Python")
    else:
        report.add("PATH Python", "FAIL", f"unexpected interpreter: {executable}", fatal=True)
        failures.append("PATH Python")

    if paths.PACKAGES_ROOT.is_dir() and paths.is_within_project(paths.PACKAGES_ROOT):
        report.add("portable packages", "PASS", str(paths.PACKAGES_ROOT))
    else:
        report.add(
            "portable packages",
            "FAIL",
            f"missing or outside project: {paths.PACKAGES_ROOT}",
            fatal=strict,
        )
        failures.append("portable packages")

    try:
        import duckdb  # type: ignore[import-not-found]

        duckdb_file = Path(duckdb.__file__).resolve()
    except Exception as exc:  # pragma: no cover - depends on broken runtime payload
        duckdb_file = None
        report.add("portable DuckDB import", "FAIL", str(exc), fatal=strict)
        failures.append("portable DuckDB import")
    else:
        if is_portable and paths.is_within_project(duckdb_file) and duckdb_file.is_relative_to(paths.PACKAGES_ROOT.resolve()):
            report.add("portable DuckDB import", "PASS", str(duckdb_file))
        elif is_development:
            report.add(
                "portable DuckDB import",
                "FAIL",
                f"development import is {duckdb_file}; production must import from {paths.PACKAGES_ROOT}",
            )
            failures.append("portable DuckDB import")
        else:
            report.add(
                "portable DuckDB import",
                "FAIL",
                f"imported from {duckdb_file}; expected below {paths.PACKAGES_ROOT}",
                fatal=True,
            )
            failures.append("portable DuckDB import")

        duckdb_version = str(getattr(duckdb, "__version__", ""))
        if is_portable and duckdb_version == paths.DUCKDB_VERSION:
            report.add("portable DuckDB version", "PASS", duckdb_version)
        elif is_development:
            report.add(
                "portable DuckDB version",
                "FAIL",
                f"development import reports {duckdb_version or 'unknown'}; production requires {paths.DUCKDB_VERSION}",
            )
            failures.append("portable DuckDB version")
        else:
            report.add(
                "portable DuckDB version",
                "FAIL",
                f"{duckdb_version or 'unknown'}; required {paths.DUCKDB_VERSION}",
                fatal=True,
            )
            failures.append("portable DuckDB version")

    source_file = Path(__file__).resolve()
    if paths.is_within_project(source_file) and source_file.is_relative_to(paths.SRC_ROOT.resolve()):
        report.add("portable chongzu source import", "PASS", str(source_file))
    elif is_development:
        report.add("portable chongzu source import", "FAIL", f"imported from {source_file}")
        failures.append("portable chongzu source import")
    else:
        report.add("portable chongzu source import", "FAIL", f"imported from {source_file}", fatal=True)
        failures.append("portable chongzu source import")

    if is_portable and sys.prefix == sys.base_prefix:
        prefix_ok = True
    elif is_development:
        prefix_ok = False
    else:
        prefix_ok = False
    if not prefix_ok:
        failures.append("portable sys.prefix")

    if not failures:
        report.add("PORTABLE RUNTIME", "PASS", "standalone CPython + project-local packages are active")
    elif is_development:
        report.add(
            "PORTABLE RUNTIME",
            "FAIL",
            "development venv is not part of the portable runtime contract",
        )
    else:
        report.add("PORTABLE RUNTIME", "FAIL", "; ".join(failures), fatal=True)


def _check_uv(report: DoctorReport) -> None:
    if not paths.UV_EXE.is_file() or not paths.is_within_project(paths.UV_EXE):
        report.add("project-local uv", "FAIL", f"missing or outside project: {paths.UV_EXE}", fatal=True)
        return

    try:
        digest = _sha256(paths.UV_EXE)
    except OSError as exc:
        report.add("project-local uv", "FAIL", f"cannot hash executable: {exc}", fatal=True)
        return
    if digest == paths.PROJECT_UV_SHA256:
        report.add("project-local uv SHA-256", "PASS", digest)
    else:
        report.add("project-local uv SHA-256", "FAIL", f"{digest}; expected {paths.PROJECT_UV_SHA256}", fatal=True)

    try:
        completed = subprocess.run(
            [str(paths.UV_EXE), "--no-cache", "--version"],
            cwd=str(paths.PROJECT_ROOT),
            env=_controlled_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        output = (completed.stdout or completed.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        report.add("project-local uv version", "FAIL", str(exc), fatal=True)
        return
    if completed.returncode == 0 and output.startswith(f"uv {paths.PROJECT_UV_VERSION}"):
        report.add("project-local uv version", "PASS", output)
    else:
        report.add("project-local uv version", "FAIL", output or f"exit {completed.returncode}", fatal=True)


def _check_optional_tools(report: DoctorReport) -> None:
    git = shutil.which("git")
    if git:
        report.add("Git (optional)", "INFO", f"available at {git}")
    else:
        report.add("Git (optional)", "INFO", "not installed; not required for processing")

    java = shutil.which("java")
    if java:
        report.add("Java/Tika", "INFO", f"Java present at {java}; NOT REQUIRED IN PHASE 2.5")
    else:
        report.add("Java/Tika", "INFO", "NOT INSTALLED / NOT REQUIRED IN PHASE 2.5")

    report.add("Docling", "INFO", "NOT INSTALLED / NOT REQUIRED IN PHASE 2.5")
    report.add("OCR/RapidOCR", "INFO", "NOT INSTALLED / NOT REQUIRED IN PHASE 2.5")


def run_checks() -> DoctorReport:
    """Run all project-local runtime checks in the current process."""

    report = DoctorReport()
    if paths.PROJECT_ROOT.is_dir() and paths.is_within_project(paths.PROJECT_ROOT):
        report.add("project root", "PASS", str(paths.PROJECT_ROOT))
    else:
        report.add("project root", "FAIL", str(paths.PROJECT_ROOT), fatal=True)
    _check_runtime(report)
    _check_portable_imports(report)
    _check_directories(report)
    _check_environment_paths(report)
    _check_uv(report)
    _check_optional_tools(report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chongzu doctor", description="Check project-local runtime containment")
    parser.parse_args(list(argv) if argv is not None else None)
    report = run_checks()
    print(f"Project root: {paths.PROJECT_ROOT}")
    for check in report.checks:
        print(f"{check.status:<5} {check.name}: {check.detail}")
    result = "PASS" if report.ok else "FAIL"
    print(f"RESULT: {result}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised by the launcher
    raise SystemExit(main())
