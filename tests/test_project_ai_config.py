from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from dongjian.api.app import ApiError, BackendApp
from dongjian.clean import process_source
from dongjian.registry import Registry
from dongjian.semantic.config import SemanticConfig
from dongjian.semantic.settings import ProjectAIConfigStore, load_runtime_ai_settings
from dongjian.services.process import ProcessTask, ProcessTaskManager


def _project(tmp_path: Path) -> Path:
    return tmp_path / "project"


def _write_config(project: Path, value: dict[str, object]) -> None:
    path = project / "config" / "llm.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_missing_and_blank_project_config_are_offline(tmp_path: Path) -> None:
    project = _project(tmp_path)
    missing = load_runtime_ai_settings(project)
    assert missing.source == "offline"
    assert missing.status == "NOT_CONFIGURED"
    assert missing.enabled is False
    assert missing.vision_enabled is False

    _write_config(
        project,
        {
            "base_url": "",
            "api_key": "",
            "model": "",
            "timeout_seconds": 120,
            "vision_enabled": False,
        },
    )
    blank = load_runtime_ai_settings(project)
    assert blank.source == "project"
    assert blank.status == "NOT_CONFIGURED"
    assert blank.enabled is False
    assert blank.vision_enabled is False

    (project / "config" / "llm.json").write_text("\n  \t", encoding="utf-8")
    whitespace = load_runtime_ai_settings(project)
    assert whitespace.source == "project"
    assert whitespace.status == "NOT_CONFIGURED"
    assert whitespace.enabled is False
    assert whitespace.vision_enabled is False


def test_project_config_has_priority_and_vision_is_separate_capability(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.mkdir(parents=True)
    (project / ".env").write_text(
        "LLM_BASE_URL=http://env.invalid/v1\nLLM_API_KEY=env-key\nLLM_MODEL=env-model\n",
        encoding="utf-8",
    )
    _write_config(
        project,
        {
            "base_url": "http://project.invalid/v1",
            "api_key": "project-key",
            "model": "project-model",
            "timeout_seconds": 120,
            "vision_enabled": True,
        },
    )
    runtime = load_runtime_ai_settings(project)
    assert runtime.source == "project"
    assert runtime.config.base_url == "http://project.invalid/v1"
    assert runtime.config.model == "project-model"
    assert runtime.config.timeout_seconds == 120
    assert runtime.enabled is True
    assert runtime.vision_enabled is True


def test_project_config_store_preserves_key_when_ui_field_is_blank_and_relocates(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = ProjectAIConfigStore(project)
    saved = store.save(
        base_url="http://127.0.0.1:9000/v1",
        api_key="synthetic-project-key",
        model="synthetic-model",
        timeout=120,
        vision_enabled=True,
    )
    assert saved.api_key == "synthetic-project-key"
    updated = store.save(
        base_url="http://127.0.0.1:9000/v1",
        api_key="",
        model="synthetic-model-2",
        timeout=30,
        vision_enabled=False,
    )
    assert updated.api_key == "synthetic-project-key"
    assert load_runtime_ai_settings(project).config.model == "synthetic-model-2"

    relocated = tmp_path / "relocated-project"
    (relocated / "config").mkdir(parents=True)
    (relocated / "config" / "llm.json").write_bytes((project / "config" / "llm.json").read_bytes())
    relocated_runtime = load_runtime_ai_settings(relocated)
    assert relocated_runtime.source == "project"
    assert relocated_runtime.config.model == "synthetic-model-2"
    assert relocated_runtime.config.api_key == "synthetic-project-key"


def test_api_health_and_task_surfaces_never_include_project_api_key(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_config(
        project,
        {
            "base_url": "http://127.0.0.1:9000/v1",
            "api_key": "synthetic-secret-not-for-output",
            "model": "synthetic-model",
            "timeout_seconds": 120,
            "vision_enabled": False,
        },
    )
    app = BackendApp(project_root=project, registry_path=project / "workspace/state/registry.duckdb", workspace_root=project / "workspace")
    try:
        settings = app.handle_api("GET", "/api/v1/settings/ai", {}).payload
        health = app.handle_api("GET", "/api/v1/health", {}).payload
        serialized = json.dumps({"settings": settings, "health": health}, ensure_ascii=False)
        assert "synthetic-secret-not-for-output" not in serialized
        assert settings["settings"]["source"] == "project"
        assert settings["settings"]["visionEnabled"] is False
        assert settings["settings"]["configPath"] == "config/llm.json"
    finally:
        app.close(timeout=5)


def test_frontend_and_release_use_one_project_config_contract() -> None:
    root = Path(__file__).parents[1]
    frontend = (root / "frontend/src/App.tsx").read_text(encoding="utf-8")
    api = (root / "frontend/src/api.ts").read_text(encoding="utf-8")
    release = (root / "scripts/build_release.ps1").read_text(encoding="utf-8")
    assert "visionEnabled" in frontend
    assert "未配置：完全离线" in frontend
    assert "visionEnabled: boolean" in api
    assert "config\\llm.json" in release
    assert "llm.example.json" in release


def test_project_key_is_redacted_from_registry_task_and_logs(tmp_path: Path) -> None:
    project = _project(tmp_path)
    secret = "synthetic-key-never-output"
    _write_config(
        project,
        {
            "base_url": "http://127.0.0.1:9000/v1",
            "api_key": secret,
            "model": "synthetic-model",
            "timeout_seconds": 120,
            "vision_enabled": False,
        },
    )
    source = project / "input"
    source.mkdir(parents=True)
    (source / "notes.txt").write_text("analysis note", encoding="utf-8")
    workspace = project / "workspace"
    registry_path = workspace / "state" / "registry.duckdb"
    process_source(source, workers=1, registry_path=registry_path, workspace_root=workspace)
    registry = Registry.open(registry_path)
    try:
        asset_id = registry.connection.execute(
            "SELECT asset_id FROM catalog_assets WHERE asset_type='text' LIMIT 1"
        ).fetchone()[0]
    finally:
        registry.close()

    class FailingProvider:
        name = "synthetic-provider"
        model = "synthetic-model"
        config = SemanticConfig(
            base_url="http://127.0.0.1:9000/v1",
            api_key=secret,
            model="synthetic-model",
        )

        def generate(self, _request):
            raise RuntimeError(f"upstream detail contains {secret}")

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger(f"test-project-key-{id(stream)}")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    provider = FailingProvider()
    app = BackendApp(
        project_root=project,
        registry_path=registry_path,
        workspace_root=workspace,
        semantic_provider=provider,
        logger=logger,
    )
    try:
        with pytest.raises(ApiError):
            app.handle_api("POST", f"/api/v1/assets/{asset_id}/semantic-enrich", {}, b"{}")
        registry = Registry.open(registry_path)
        try:
            error_message = registry.connection.execute(
                "SELECT error_message FROM semantic_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            registry.close()
        assert secret not in str(error_message)
        task = ProcessTask(
            task_id="task-secret-check",
            source=str(source),
            current_file="notes.txt",
            _vision_provider=SimpleNamespace(config=provider.config),
        )
        task_error = ProcessTaskManager._error_for(task, RuntimeError(f"failure {secret}"))
        assert secret not in json.dumps(task.public_dict(), ensure_ascii=False)
        assert secret not in json.dumps(task_error, ensure_ascii=False)
        handler.flush()
        assert secret not in stream.getvalue()
    finally:
        app.close(timeout=5)
        logger.removeHandler(handler)
