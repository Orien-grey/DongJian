"""Product-launcher relocation acceptance for the Phase 8 bundle surface."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from chongzu import paths
from tests.pdf_factory import write_pdf
from tests.test_portable_runtime import _build_relocated_copy, _portable_env
from tests.xlsx_factory import write_xlsx


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_request(base: str, path: str, *, method: str = "GET", payload: object | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def test_moved_project_start_process_catalog_stop() -> None:
    root = paths.PROJECT_ROOT
    staging = root / "workspace" / "portability-test"
    destination = staging / "Phase8 Moved Project"
    if destination.exists():
        shutil.rmtree(destination)
    source = destination / "workspace" / "mixed product corpus 中文 with spaces"
    server_started = False
    try:
        _build_relocated_copy(root, destination)
        clean_env = _portable_env(destination, clean_host=True)
        source.mkdir(parents=True)
        (source / "records.csv").write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")
        write_xlsx(source / "records.xlsx", [("Data", [["name", "value"], ["alpha", 1], ["beta", 2]], None)])
        write_pdf(source / "native.pdf", [{"texts": [(72, 72, "relocated native PDF text")]}])
        (source / "notes.txt").write_text("relocated text evidence", encoding="utf-8")
        (source / "page.html").write_text("<html>retained</html>", encoding="utf-8")
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (800, 300), "white")
        ImageDraw.Draw(image).text((35, 90), "Moved Product OCR 123", fill="black")
        png = source / "picture.png"
        image.save(png)
        image.save(source / "photo.jpg", format="JPEG")
        import pymupdf

        scanned_pdf = source / "scanned.pdf"
        document = pymupdf.open()
        page = document.new_page(width=800, height=300)
        page.insert_image(pymupdf.Rect(0, 0, 800, 300), filename=str(png))
        document.save(scanned_pdf)
        document.close()
        before_hashes = {path.name: _sha256(path) for path in source.iterdir()}

        started = subprocess.run(
            "call " + subprocess.list2cmdline([str(destination / "start.cmd")]),
            shell=True,
            cwd=str(destination),
            env=clean_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
        assert started.returncode == 0, started.stdout + started.stderr
        server_started = True
        state = json.loads((destination / "workspace" / "state" / "server.pid").read_text(encoding="utf-8"))
        base = f"http://127.0.0.1:{state['port']}"
        health = _json_request(base, "/api/v1/health")
        assert health["app"]["name"] == "ChongZu"
        assert health["llm"]["status"] == "NOT_CONFIGURED"

        queued = _json_request(base, "/api/v1/process", method="POST", payload={"source": str(source)})
        task_id = queued["taskId"]
        deadline = time.monotonic() + 240
        task = queued["task"]
        while task["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.5)
            task = _json_request(base, f"/api/v1/tasks/{task_id}")
        assert task["status"] == "succeeded", task
        assert task["summary"]["filesUnsupported"] >= 1
        assert task["summary"]["tableAssets"] >= 2
        assert task["summary"]["textAssets"] >= 4

        overview = _json_request(base, "/api/v1/overview")
        assert overview["supported"] >= 7
        catalog = _json_request(base, "/api/v1/catalog?view=assets&limit=100")
        assert catalog["pagination"]["total"] >= 6
        table = next(item for item in catalog["items"] if item["assetType"] == "table")
        text = next(item for item in catalog["items"] if item["assetType"] == "text")
        table_preview = _json_request(base, f"/api/v1/assets/{table['assetId']}/table-preview?limit=5")
        text_preview = _json_request(base, f"/api/v1/assets/{text['assetId']}/text-preview?limit=200")
        assert table_preview["rows"]
        assert text_preview["text"]

        search = _json_request(base, "/api/v1/search?q=relocated&limit=20")
        assert search["query"] == "relocated"
        assert search["results"]
        assert all(item["provenance"]["contentSha256"] for item in search["results"])
        query_schema = _json_request(
            base,
            "/api/v1/query/schema",
            method="POST",
            payload={"assetIds": [table["assetId"]]},
        )
        assert query_schema["relations"][0]["alias"] == "t1"
        assert query_schema["relations"][0]["columns"]
        query_result = _json_request(
            base,
            "/api/v1/query/sql",
            method="POST",
            payload={"assetIds": [table["assetId"]], "sql": "SELECT * FROM t1 LIMIT 2"},
        )
        assert query_result["rowCount"] <= 2
        assert query_result["sandbox"].startswith("duckdb-memory-")
        hostile_request = Request(
            f"{base}/api/v1/query/sql",
            data=json.dumps(
                {
                    "assetIds": [table["assetId"]],
                    "sql": "SELECT * FROM read_csv_auto('C:\\\\outside.csv')",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urlopen(hostile_request, timeout=15)
        except HTTPError as error:
            hostile_payload = json.loads(error.read().decode("utf-8"))
            assert error.code == 400
            assert hostile_payload["error"]["code"] == "external_access_denied"
        else:
            raise AssertionError("external file SQL unexpectedly succeeded")
        assert before_hashes == {path.name: _sha256(path) for path in source.iterdir()}
    finally:
        if server_started:
            stopped = subprocess.run(
                "call " + subprocess.list2cmdline([str(destination / "stop.cmd")]),
                shell=True,
                cwd=str(destination),
                env=_portable_env(destination, clean_host=True),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=30,
            )
            assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        if destination.exists():
            shutil.rmtree(destination)
        if staging.exists() and not any(staging.iterdir()):
            staging.rmdir()
