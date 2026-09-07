from pathlib import Path

from dongjian import paths


def test_project_root_is_repository_root() -> None:
    assert paths.PROJECT_ROOT.is_dir()
    assert (paths.PROJECT_ROOT / "AGENTS.md").is_file()
    assert paths.PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_core_paths_are_inside_project_root() -> None:
    for name, value in paths.CORE_DIRECTORIES.items():
        assert paths.is_within_project(value), name

    for name, value in paths.CONTROLLED_ENV_PATHS.items():
        assert paths.is_within_project(value), name


def test_project_path_handles_spaces_without_string_concatenation() -> None:
    candidate = paths.project_path("workspace", "input", "folder with spaces", "sample.txt")
    assert candidate == paths.INPUT_ROOT / "folder with spaces" / "sample.txt"
    assert paths.is_within_project(candidate)


def test_python_runtime_identity_is_pinned() -> None:
    assert paths.PYTHON_VERSION == "3.11.15"
    assert paths.PYTHON_EXE == paths.PYTHON_RUNTIME_DIR / "python.exe"
    assert paths.is_within_project(paths.PYTHON_EXE)
