"""Phase 8 localhost API and task-contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from chongzu.api.app import BackendApp
from chongzu.api.server import ChongZuHTTPServer


def _request(base: str, path: str, *, method: str = "GET", payload: object | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture()
def api_server(tmp_path: Path):
    project = tmp_path / "project"
    source = tmp_path / "中文 资料"
    (project / "workspace" / "state").mkdir(parents=True)
    (project / "workspace" / "logs").mkdir(parents=True)
    (project / "frontend" / "dist").mkdir(parents=True)
    (project / "frontend" / "dist" / "index.html").write_text("<!doctype html><title>ChongZu</title>", encoding="utf-8")
    source.mkdir()
    (source / "clean.csv").write_text("姓名,编号,值\n张三,001,10\n李四,002,20\n", encoding="utf-8")
    (source / "duplicate.csv").write_text("项目,项目\n甲,乙\n", encoding="utf-8")
    (source / "说明.txt").write_text("第一段说明。\n\n第二段说明。\n", encoding="utf-8")
    (source / "empty.txt").write_text("", encoding="utf-8")
    (source / "not-supported.html").write_text("<html>unsupported</html>", encoding="utf-8")
    registry_path = project / "workspace" / "state" / "registry.duckdb"
    workspace = project / "workspace"
    app = BackendApp(project_root=project, registry_path=registry_path, workspace_root=workspace, frontend_dist=project / "frontend" / "dist")
    server = ChongZuHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, source
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        thread.join(timeout=5)


def test_health_empty_and_static_are_local(api_server) -> None:
    base, _source = api_server
    status, payload = _request(base, "/api/v1/health")
    assert status == 200
    assert payload["app"]["name"] == "ChongZu"
    assert payload["llm"]["status"] == "NOT_CONFIGURED"
    assert payload["llm"]["networkCalls"] == "disabled"
    with urlopen(f"{base}/", timeout=10) as response:
        assert response.status == 200
        assert "ChongZu" in response.read().decode("utf-8")


def test_bad_source_and_asset_404_use_stable_errors(api_server) -> None:
    base, _source = api_server
    status, payload = _request(base, "/api/v1/process", method="POST", payload={"source": ""})
    assert status == 400
    assert payload["error"]["code"] == "invalid_source"
    assert payload["error"]["requestId"].startswith("req_")
    status, payload = _request(base, "/api/v1/assets/not-an-asset")
    assert status == 404
    assert payload["error"]["code"] == "asset_not_found"


def test_process_task_catalog_previews_and_quality_update(api_server) -> None:
    base, source = api_server
    status, payload = _request(base, "/api/v1/process", method="POST", payload={"source": str(source)})
    assert status == 202
    task_id = payload["taskId"]
    deadline = time.monotonic() + 40
    task = payload["task"]
    while task["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.2)
        status, task = _request(base, f"/api/v1/tasks/{task_id}")
    assert status == 200
    assert task["status"] == "succeeded", task
    assert task["summary"]["filesUnsupported"] >= 1
    assert task["summary"]["tableAssets"] >= 1
    assert task["summary"]["textAssets"] >= 1

    status, catalog = _request(base, "/api/v1/catalog?view=assets&limit=2&offset=0")
    assert status == 200
    assert catalog["pagination"]["total"] >= 3
    assert len(catalog["items"]) <= 2
    table = next(item for item in catalog["items"] if item["assetType"] == "table")
    status, detail = _request(base, f"/api/v1/assets/{table['assetId']}")
    assert status == 200
    assert detail["source"]["sha256"]
    assert detail["artifacts"]["raw"]
    status, preview = _request(base, f"/api/v1/assets/{table['assetId']}/table-preview?limit=2")
    assert status == 200
    assert preview["layer"] == "normalized"
    assert len(preview["rows"]) <= 2
    status, raw_preview = _request(base, f"/api/v1/assets/{table['assetId']}/table-preview?layer=raw&limit=2")
    assert status == 200
    assert raw_preview["layer"] == "raw"

    status, text_catalog = _request(base, "/api/v1/catalog?type=text&limit=20")
    assert status == 200
    text = text_catalog["items"][0]
    status, text_preview = _request(base, f"/api/v1/assets/{text['assetId']}/text-preview?limit=100")
    assert status == 200
    assert text_preview["layer"] == "normalized"
    assert len(text_preview["text"]) <= 100

    status, issues = _request(base, "/api/v1/quality/issues?status=open&limit=100")
    assert status == 200
    assert issues["items"]
    issue_id = issues["items"][0]["issue_id"]
    status, updated = _request(base, f"/api/v1/quality/issues/{issue_id}", method="PATCH", payload={"status": "accepted"})
    assert status == 200
    assert updated["item"]["status"] == "accepted"


def test_catalog_filter_and_static_path_traversal_are_bounded(api_server) -> None:
    base, _source = api_server
    status, payload = _request(base, "/api/v1/catalog?type=bad")
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"
    status, payload = _request(base, "/%2e%2e/%2e%2e/secret.txt")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_search_and_safe_sql_api_contract(api_server) -> None:
    base, source = api_server
    status, queued = _request(base, "/api/v1/process", method="POST", payload={"source": str(source)})
    assert status == 202
    task_id = queued["taskId"]
    task = queued["task"]
    deadline = time.monotonic() + 40
    while task["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.2)
        status, task = _request(base, f"/api/v1/tasks/{task_id}")
    assert status == 200
    assert task["status"] == "succeeded", task

    status, search = _request(base, "/api/v1/search?q=clean&type=table&limit=10")
    assert status == 200
    assert search["query"] == "clean"
    assert search["results"]
    assert search["results"][0]["assetType"] == "table"
    assert search["results"][0]["provenance"]["contentSha256"]

    status, catalog = _request(base, "/api/v1/catalog?type=table&limit=100")
    assert status == 200
    table_ids = [item["assetId"] for item in catalog["items"]]
    assert table_ids
    status, schema = _request(base, "/api/v1/query/schema", method="POST", payload={"assetIds": [table_ids[0]]})
    assert status == 200
    assert schema["relations"][0]["alias"] == "t1"
    assert schema["relations"][0]["columns"]

    status, result = _request(
        base,
        "/api/v1/query/sql",
        method="POST",
        payload={"assetIds": [table_ids[0]], "sql": "SELECT * FROM t1 LIMIT 2"},
    )
    assert status == 200
    assert result["rowCount"] <= 2
    assert result["sandbox"].startswith("duckdb-memory-")

    status, rejected = _request(
        base,
        "/api/v1/query/sql",
        method="POST",
        payload={"assetIds": [table_ids[0]], "sql": "SELECT * FROM read_csv_auto('C:\\\\outside.csv')"},
    )
    assert status == 400
    assert rejected["error"]["code"] == "external_access_denied"
