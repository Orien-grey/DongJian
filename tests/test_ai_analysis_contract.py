from __future__ import annotations

import json
from pathlib import Path

import pytest

from chongzu.api.app import ApiError, BackendApp
from chongzu.clean import process_source
from chongzu.services.analysis import AnalysisService
from chongzu.services.sql import SqlServiceError


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "measurements.csv").write_text("sample,value\nalpha,42\nbeta,7\n", encoding="utf-8")
    (source / "notes.txt").write_text("alpha experiment narrative for analysis", encoding="utf-8")
    workspace = tmp_path / "workspace"
    process_source(
        source,
        workers=1,
        registry_path=workspace / "state/registry.duckdb",
        workspace_root=workspace,
    )
    return source, workspace


def _asset_ids(workspace: Path) -> tuple[str, str]:
    from chongzu.registry import Registry

    registry = Registry.open(workspace / "state/registry.duckdb")
    try:
        rows = registry.connection.execute(
            "SELECT asset_id, asset_type FROM catalog_assets ORDER BY asset_type"
        ).fetchall()
    finally:
        registry.close()
    return next(row[0] for row in rows if row[1] == "table"), next(row[0] for row in rows if row[1] == "text")


def test_analysis_context_exposes_table_text_provenance_and_bounds(tmp_path: Path) -> None:
    _source, workspace = _prepare(tmp_path)
    table_id, text_id = _asset_ids(workspace)
    service = AnalysisService(
        registry_path=workspace / "state/registry.duckdb",
        workspace_root=workspace,
    )
    context = service.asset_context([table_id, text_id])
    assert context["contract"] == "analysis-context-v1"
    assert {asset["assetType"] for asset in context["assets"]} == {"table", "text"}
    table = next(asset for asset in context["assets"] if asset["assetType"] == "table")
    text = next(asset for asset in context["assets"] if asset["assetType"] == "text")
    assert table["schema"]["columns"]
    assert table["rowCount"] == 2
    assert table["sampleRows"]
    assert table["source"]["sha256"]
    assert table["provenance"]["extractionRunId"]
    assert text["title"]
    assert text["text"] == "alpha experiment narrative for analysis"
    assert text["chunks"]
    assert text["source"]["relativePath"].endswith("notes.txt")


def test_analysis_delegates_search_and_read_only_sql(tmp_path: Path) -> None:
    _source, workspace = _prepare(tmp_path)
    table_id, text_id = _asset_ids(workspace)
    service = AnalysisService(
        registry_path=workspace / "state/registry.duckdb",
        workspace_root=workspace,
    )
    search = service.search("alpha", asset_type="all", limit=10, offset=0, match="all")
    assert search["results"]
    assert any(result["assetId"] == text_id for result in search["results"])
    query = service.safe_sql([table_id], "SELECT sample, value FROM t1 WHERE value > 10")
    assert query["rows"] == [{"sample": "alpha", "value": 42}]
    with pytest.raises(SqlServiceError):
        service.sql_service.execute([table_id], "DELETE FROM t1")


def test_analysis_http_context_search_and_sql_are_thin_adapters(tmp_path: Path) -> None:
    _source, workspace = _prepare(tmp_path)
    table_id, text_id = _asset_ids(workspace)
    app = BackendApp(
        project_root=tmp_path,
        registry_path=workspace / "state/registry.duckdb",
        workspace_root=workspace,
    )
    try:
        context = app.handle_api(
            "POST",
            "/api/v1/analysis/context",
            {},
            json.dumps({"assetIds": [table_id, text_id]}).encode(),
        )
        assert context.status == 200
        search = app.handle_api(
            "POST",
            "/api/v1/analysis/search",
            {},
            json.dumps({"query": "alpha", "limit": 10}).encode(),
        )
        assert search.status == 200
        sql = app.handle_api(
            "POST",
            "/api/v1/analysis/sql",
            {},
            json.dumps({"assetIds": [table_id], "sql": "SELECT * FROM t1 LIMIT 1"}).encode(),
        )
        assert sql.status == 200
        with pytest.raises(ApiError) as failure:
            app.handle_api(
                "POST",
                "/api/v1/analysis/sql",
                {},
                json.dumps({"assetIds": [table_id], "sql": "UPDATE t1 SET value=0"}).encode(),
            )
        assert failure.value.code in {"read_only_required", "forbidden_sql"}
    finally:
        app.close(timeout=5)

