from __future__ import annotations

import json
from pathlib import Path

import pytest

from dongjian.registry import Registry
import dongjian.services.reset as reset_module
from dongjian.services.reset import WorkspaceResetError, WorkspaceResetService


def test_reset_recreates_registry_and_removes_all_registry_companions(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = project / "workspace"
    source = workspace / "input" / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("immutable source", encoding="utf-8")
    config = project / "config" / "llm.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"base_url": "", "api_key": "", "model": ""}), encoding="utf-8")

    registry_path = workspace / "state" / "registry.duckdb"
    Registry.ensure_initialized(registry_path)
    registry = Registry.open(registry_path, initialize=False)
    try:
        registry.connection.execute(
            """
            INSERT INTO quality_issues(
                issue_id, extraction_run_id, cleaning_run_id, semantic_run_id,
                asset_id, severity, issue_type, description, evidence_json,
                detected_by, suggested_action, status, created_at
            ) VALUES (?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            ["issue-reset", "asset-reset", "warning", "reset-test", "generated", "{}", "test", "review", "open"],
        )
    finally:
        registry.close()

    for suffix in (".wal", ".tmp", ".schema.lock"):
        (registry_path.parent / f"registry.duckdb{suffix}").write_text("generated", encoding="ascii")
    (workspace / "artifacts" / "reports").mkdir(parents=True)
    (workspace / "artifacts" / "reports" / "report.json").write_text("generated", encoding="utf-8")

    result = WorkspaceResetService(project).reset(preserve_runtime_files=False)

    assert result["reset"] is True
    assert result["removed"]["registry_rows"]["database"] == "recreated"
    assert all(not path.exists() for path in registry_path.parent.glob("registry.duckdb.*"))
    assert source.read_text(encoding="utf-8") == "immutable source"
    assert config.is_file()
    assert not (workspace / "artifacts" / "reports" / "report.json").exists()

    fresh = Registry.open(registry_path, initialize=False, read_only=True)
    try:
        assert fresh.schema_version() == 5
        assert fresh.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert fresh.connection.execute("SELECT COUNT(*) FROM quality_issues").fetchone()[0] == 0
    finally:
        fresh.close()


def test_registry_install_failure_reports_target_and_restores_old_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    registry_path = project / "workspace" / "state" / "registry.duckdb"
    Registry.ensure_initialized(registry_path)
    (project / "workspace" / "artifacts" / "keep").mkdir(parents=True)
    marker = project / "workspace" / "artifacts" / "keep" / "marker.txt"
    marker.write_text("still generated until reset succeeds", encoding="utf-8")

    original_replace = reset_module.os.replace
    failed = False

    def fail_install(source: object, target: object) -> None:
        nonlocal failed
        if Path(target).resolve() == registry_path.resolve() and not failed:
            failed = True
            error = PermissionError("synthetic registry lock")
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        original_replace(source, target)

    monkeypatch.setattr(reset_module.os, "replace", fail_install)
    with pytest.raises(WorkspaceResetError) as raised:
        WorkspaceResetService(project).reset(preserve_runtime_files=False)

    error = raised.value
    assert error.stage == "registry_clear"
    assert error.target_path_safe == "workspace/state/registry.duckdb"
    assert "install_fresh_registry:PermissionError" in (error.diagnostic or "")
    assert "winerror=32" in (error.diagnostic or "")
    assert marker.is_file()
    restored = Registry.open(registry_path, initialize=False, read_only=True)
    try:
        assert restored.schema_version() == 5
    finally:
        restored.close()
