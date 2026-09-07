import os
import site
from pathlib import Path

from dongjian import paths


def test_controlled_environment_is_project_local() -> None:
    for name in (
        "DONGJIAN_ROOT",
        "DONGJIAN_PYTHON",
        "DONGJIAN_PACKAGES",
        "DONGJIAN_SRC",
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "TMP",
        "TEMP",
        "UV_PYTHON",
    ):
        value = os.environ.get(name)
        assert value, name
        assert paths.is_within_project(Path(value)), (name, value)


def test_python_user_site_is_disabled() -> None:
    assert os.environ.get("PYTHONNOUSERSITE") == "1"
    assert site.ENABLE_USER_SITE is False
    user_site = site.getusersitepackages()
    assert not user_site or user_site not in os.sys.path


def test_uv_cannot_fall_back_to_downloaded_or_global_python() -> None:
    assert os.environ.get("UV_MANAGED_PYTHON") == "1"
    assert os.environ.get("UV_PYTHON_DOWNLOADS") == "never"
    assert Path(os.environ["UV_PYTHON"]).resolve() == paths.PYTHON_EXE.resolve()


def test_pythonpath_and_temp_are_not_external() -> None:
    pythonpath = os.environ.get("PYTHONPATH", "")
    assert pythonpath
    assert all(paths.is_within_project(Path(part)) for part in pythonpath.split(os.pathsep) if part)
    assert paths.is_within_project(Path(os.environ["PYTHONPYCACHEPREFIX"]))


def test_env_script_does_not_persist_or_change_path() -> None:
    script = paths.PROJECT_ROOT / "scripts" / "env.ps1"
    content = script.read_text(encoding="utf-8")
    assert "setx" not in content.lower()
    assert "$env:PATH" not in content
