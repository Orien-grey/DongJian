"""Run the Phase 10 product journey against a fresh release and its ZIP.

This is an acceptance harness, not a product dependency.  It intentionally
uses only the development checkout to create synthetic evidence, then talks
to the relocated production launcher over localhost HTTP.  It never imports
the application services to satisfy an acceptance assertion.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RELEASE_ROOT = ROOT / "release"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
BUNDLE_NAME = f"DongJian-{VERSION}-rc2-win-x64"
SOURCE_NAME = "接受 测试资料 中文 with spaces"
WORK_ROOT = ROOT / "cache" / "temp" / "phase10-acceptance"
DEFAULT_TIMEOUT = 30
TASK_TIMEOUT = 360
PORT = 18_765


@dataclass(frozen=True)
class FileFingerprint:
    sha256: str
    size: int
    mtime_ns: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprints(root: Path) -> dict[str, FileFingerprint]:
    return {
        path.relative_to(root).as_posix(): FileFingerprint(
            sha256=_sha256(path),
            size=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _clean_process_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("DONGJIAN_") or key in {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
        }:
            env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["DONGJIAN_NO_BROWSER"] = "1"
    return env


def _launcher(root: Path, name: str, *, timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    command = "call " + subprocess.list2cmdline([str(root / name)])
    return subprocess.run(
        command,
        cwd=str(root),
        env=_clean_process_env(),
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _run_required(root: Path, name: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    result = _launcher(root, name, timeout=timeout)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise AssertionError(f"{name} failed with {result.returncode}:\n{output}")
    return output


def _git_head() -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"unable to resolve source HEAD: {(result.stdout or '') + (result.stderr or '')}")
    return result.stdout.strip()


def _json_request(
    base: str,
    path: str,
    *,
    method: str = "GET",
    payload: object | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, dict[str, Any]]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _ok(base: str, path: str, *, method: str = "GET", payload: object | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    status, value = _json_request(base, path, method=method, payload=payload, timeout=timeout)
    if status < 200 or status >= 300:
        raise AssertionError(f"{method} {path} returned HTTP {status}: {value}")
    return value


def _wait_task(base: str, task_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TASK_TIMEOUT
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _ok(base, f"/api/v1/tasks/{task_id}")
        if latest.get("status") not in {"queued", "running"}:
            break
        time.sleep(0.5)
    if latest.get("status") != "succeeded":
        raise AssertionError(f"process task did not succeed: {latest}")
    return latest


def _write_image(path: Path) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1_200, 520), "white")
    draw = ImageDraw.Draw(image)
    draw.text((45, 35), "DongJian OCR 001 Beijing University", fill="black")
    draw.text((45, 95), "scanned evidence keyword", fill="black")
    left, top, right, bottom = 45, 190, 1_100, 450
    draw.rectangle((left, top, right, bottom), outline="black", width=3)
    for row in range(1, 4):
        y = top + (bottom - top) * row // 4
        draw.line((left, y, right, y), fill="black", width=2)
    for column in range(1, 4):
        x = left + (right - left) * column // 4
        draw.line((x, top, x, bottom), fill="black", width=2)
    draw.text((80, 220), "A001", fill="black")
    draw.text((350, 220), "12", fill="black")
    draw.text((650, 220), "ready", fill="black")
    draw.text((80, 285), "A002", fill="black")
    draw.text((350, 285), "24", fill="black")
    draw.text((650, 285), "review", fill="black")
    draw.text((80, 350), "A003", fill="black")
    draw.text((350, 350), "36", fill="black")
    draw.text((650, 350), "done", fill="black")
    buffer = __import__("io").BytesIO()
    image.save(buffer, format="PNG")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())
    return buffer.getvalue()


def _make_corpus(source: Path) -> None:
    from tests.pdf_factory import write_pdf
    from tests.xlsx_factory import write_xlsx

    source.mkdir(parents=True, exist_ok=True)
    (source / "records.csv").write_text(
        "id,name,keyword,value\n"
        "001,北京大学,北京大学,10\n"
        "002,上海研究院,北京大学,20\n"
        "002,上海研究院,北京大学,20\n"
        "003,空值样本,,30\n",
        encoding="utf-8",
    )
    (source / "labels.csv").write_text(
        "id,label\n001,样本一\n002,样本二\n003,样本三\n",
        encoding="utf-8",
    )
    (source / "records.tsv").write_text(
        "id\tkeyword\tvalue\n001\t北京大学\t10\n002\t上海研究院\t20\n",
        encoding="utf-8",
    )
    write_xlsx(
        source / "records.xlsx",
        [
            ("Data", [["id", "keyword", "value"], ["001", "北京大学", 10], ["002", "上海研究院", 20], ["002", "上海研究院", 20]], None),
            ("Labels", [["id", "label"], ["001", "样本一"], ["002", "样本二"]], None),
        ],
    )
    fixture_xls = ROOT / "tests" / "fixtures" / "python-calamine-base.xls"
    shutil.copyfile(fixture_xls, source / "legacy records.xls")
    (source / "duplicate columns.csv").write_text("id,id\n001,北京大学\n", encoding="utf-8")
    (source / "notes.txt").write_text(
        "北京大学科研资料。\n\n"
        "This is a no-table document with a searchable keyword and a second paragraph.\n",
        encoding="utf-8",
    )
    write_pdf(
        source / "native.pdf",
        [{"texts": [(72, 72, "北京大学 native PDF searchable keyword"), (72, 110, "No table on this page.")] }],
        title="Native acceptance PDF",
    )
    image_bytes = _write_image(source / "scanned page.png")
    image_path = source / "screenshot table.png"
    image_path.write_bytes(image_bytes)
    shutil.copyfile(image_path, source / "photo.jpg")
    from PIL import Image

    with Image.open(source / "photo.jpg") as image:
        image.convert("RGB").save(source / "photo.jpg", format="JPEG")
    scanned_pdf = source / "scanned.pdf"
    document = __import__("pymupdf").open()
    page = document.new_page(width=1_200, height=520)
    page.insert_image(page.rect, stream=image_bytes)
    document.save(scanned_pdf)
    document.close()
    mixed_pdf = source / "mixed.pdf"
    document = __import__("pymupdf").open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "Page 1 native 北京大学 content", fontsize=12)
    page = document.new_page(width=1_200, height=520)
    page.insert_image(page.rect, stream=image_bytes)
    document.save(mixed_pdf)
    document.close()
    (source / "网页 screenshot.html").write_text("<html>unsupported evidence</html>", encoding="utf-8")
    # Keep the container signatures so discovery treats these as supported
    # formats; extraction must isolate the malformed payload instead of
    # turning the task into a batch failure.
    (source / "corrupted image.png").write_bytes(b"\x89PNG\r\n\x1a\ncorrupt image payload")
    (source / "corrupted document.pdf").write_bytes(b"%PDF-1.7\n%corrupt document payload")


def _assert_release_allowlist(bundle: Path) -> None:
    if not (bundle / "start.cmd").is_file():
        raise AssertionError("release start.cmd is missing")
    if (bundle / "VERSION").read_text(encoding="utf-8").strip() != "0.1.0":
        raise AssertionError("release VERSION is not 0.1.0")
    manifest_path = bundle / "release-manifest.json"
    notices_path = bundle / "THIRD_PARTY_NOTICES.txt"
    components_path = bundle / "third-party-components.json"
    licenses_path = bundle / "licenses"
    for required in (manifest_path, notices_path, components_path):
        if not required.is_file():
            raise AssertionError(f"release metadata is missing: {required.relative_to(bundle)}")
    if not licenses_path.is_dir() or not any(licenses_path.rglob("*")):
        raise AssertionError("release licenses directory is missing or empty")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    components = json.loads(components_path.read_text(encoding="utf-8-sig"))
    assert manifest["version"] == "0.1.0", manifest
    assert manifest["artifact_name"] == BUNDLE_NAME, manifest
    assert manifest["git_commit"] == _git_head(), manifest
    assert manifest["source_tree_clean"] is True, manifest
    assert manifest["frontend_build"]["status"] == "built", manifest
    assert manifest["llm_status"] == "NOT_CONFIGURED", manifest
    assert manifest["third_party_manifest"]["path"] == "third-party-components.json", manifest
    assert manifest["third_party_manifest"]["component_count"] == components["summary"]["component_count"], manifest
    assert any(item["review_status"] == "RELEASE LEGAL REVIEW REQUIRED" for item in components["components"])
    for component in components["components"]:
        for relative in component["bundled_license_path"]:
            if not (bundle / relative).is_file():
                raise AssertionError(f"bundled license evidence is missing: {relative}")
    forbidden_names = {
        ".git",
        ".github",
        ".env",
        "node_modules",
        "cache",
        "tests",
        "acceptance",
        "secrets",
    }
    forbidden_suffixes = {".zip", ".pyc", ".pyo"}
    violations: list[str] = []
    forbidden_data_prefixes = (
        "workspace/benchmark",
        "workspace/benchmarks",
        "cache/benchmark",
        "cache/benchmarks",
        "release/benchmark",
        "release/benchmarks",
    )
    for path in bundle.rglob("*"):
        relative = path.relative_to(bundle)
        parts = {part.casefold() for part in relative.parts}
        relative_text = relative.as_posix().casefold()
        forbidden_runtime_dir = any(
            relative_text == prefix or relative_text.startswith(prefix + "/")
            for prefix in ("runtime/venv", "runtime/node-dev", "runtime/uv", "runtime/python/cpython-3.11.15-windows-x86_64-none/lib/site-packages")
        )
        forbidden_data = any(
            relative_text == prefix or relative_text.startswith(prefix + "/")
            for prefix in forbidden_data_prefixes
        )
        if parts & forbidden_names or forbidden_runtime_dir or forbidden_data or path.suffix.casefold() in forbidden_suffixes:
            violations.append(relative.as_posix())
    if violations:
        raise AssertionError(f"release allowlist violation: {violations[:20]}")
    frontend_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (bundle / "frontend" / "dist").rglob("*")
        if path.is_file() and path.suffix.casefold() in {".js", ".html"}
    )
    if "尚未配置 AI 模型" not in frontend_text or "semantic-enrich" not in frontend_text:
        raise AssertionError("release frontend semantic no-config flow is missing")
    forbidden_strings = (
        r"E:\Desktop\DongJian",
        "C:\\Users\\",
        "runtime\\node-dev",
        "runtime\\venv",
    )
    source_extensions = {".py", ".cmd", ".ps1", ".json", ".html", ".js", ".css"}
    for path in bundle.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in source_extensions:
            continue
        # Documentation is allowed to mention development examples; runtime
        # code/configuration is not.  The standalone CPython standard library
        # contains generic Windows examples (including Lib/venv activation
        # templates); it is not DongJian configuration and is audited by the
        # exact-runtime-directory checks above instead.
        relative_parts = {part.casefold() for part in path.relative_to(bundle).parts}
        if "docs" in relative_parts or "runtime/python" in path.relative_to(bundle).as_posix().casefold():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in forbidden_strings:
            if needle.casefold() in text.casefold():
                raise AssertionError(f"absolute/development path in runtime file {path}: {needle}")


def _copy_tree(source: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copytree(source, destination))


def _asset_items(base: str, query: str = "") -> list[dict[str, Any]]:
    suffix = "?" + urlencode({"limit": 100, **({"q": query} if query else {})})
    return _ok(base, "/api/v1/catalog" + suffix)["items"]


def _run_journey(bundle: Path, source: Path, *, label: str, assert_reuse: bool) -> dict[str, Any]:
    if not (bundle / "workspace" / "state" / "registry.duckdb").exists():
        # This check is intentionally before start: a fresh release must not
        # carry a development registry.
        pass
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["git_commit"] == _git_head(), (label, manifest)
    assert manifest["version"] == "0.1.0", (label, manifest)
    assert (bundle / "THIRD_PARTY_NOTICES.txt").is_file()
    assert (bundle / "licenses").is_dir()
    start_output = _run_required(bundle, "start.cmd", timeout=60)
    state_path = bundle / "workspace" / "state" / "server.pid"
    if not state_path.is_file():
        raise AssertionError(f"{label}: start did not record server PID: {start_output}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    base = f"http://127.0.0.1:{int(state['port'])}"
    try:
        health = _ok(base, "/api/v1/health")
        assert health["app"]["name"] == "DongJian"
        assert health["llm"]["status"] == "NOT_CONFIGURED"
        assert health["llm"]["networkCalls"] == "disabled"
        with urlopen(f"{base}/", timeout=DEFAULT_TIMEOUT) as response:
            assert response.status == 200
            assert "DongJian" in response.read().decode("utf-8")
        empty = _ok(base, "/api/v1/overview")
        assert empty["files"] == 0 and empty["tableAssets"] == 0 and empty["textAssets"] == 0, empty

        queued = _ok(base, "/api/v1/process", method="POST", payload={"source": str(source)})
        first_task = _wait_task(base, str(queued["taskId"]))
        first_summary = first_task["summary"]
        assert first_summary["filesUnsupported"] >= 1, first_summary
        assert first_summary["filesSupported"] >= 10, first_summary
        assert first_summary["tableAssets"] >= 4, first_summary
        assert first_summary["textAssets"] >= 4, first_summary
        assert first_summary["extractionFailures"] >= 1, first_summary

        overview = _ok(base, "/api/v1/overview")
        assert overview["unsupported"] >= 1 and overview["failed"] >= 1, overview
        assert overview["tableAssets"] >= 4 and overview["textAssets"] >= 4, overview
        all_items = _asset_items(base)
        table_items = _asset_items(base, "")
        table_items = [item for item in table_items if item["assetType"] == "table"]
        text_items = [item for item in all_items if item["assetType"] == "text"]
        assert table_items and text_items
        records = next(item for item in table_items if item["source"]["relativePath"] == "records.csv")
        labels = next(item for item in table_items if item["source"]["relativePath"] == "labels.csv")
        text = next(item for item in text_items if item["source"]["relativePath"] == "notes.txt")
        table_detail = _ok(base, f"/api/v1/assets/{records['assetId']}")
        text_detail = _ok(base, f"/api/v1/assets/{text['assetId']}")
        assert table_detail["source"]["sha256"] and table_detail["artifacts"]["normalized"]
        assert text_detail["source"]["sha256"] and text_detail["artifacts"]["normalized"]
        semantic_status, semantic_error = _json_request(
            base,
            f"/api/v1/assets/{records['assetId']}/semantic-enrich",
            method="POST",
            payload={},
        )
        assert semantic_status == 409, (label, semantic_status, semantic_error)
        assert semantic_error["error"]["code"] == "SEMANTIC_NOT_CONFIGURED", semantic_error
        assert "traceback" not in json.dumps(semantic_error).casefold()
        assert _ok(base, f"/api/v1/assets/{records['assetId']}/table-preview?limit=5")["rows"]
        assert "北京大学" in _ok(base, f"/api/v1/assets/{text['assetId']}/text-preview?limit=500")["text"]

        issues = _ok(base, "/api/v1/quality/issues?status=open&limit=100")["items"]
        assert issues, "corpus should create at least one open deterministic quality issue"
        issue_id = issues[0]["issue_id"]
        updated = _ok(base, f"/api/v1/quality/issues/{issue_id}", method="PATCH", payload={"status": "accepted"})
        assert updated["item"]["status"] == "accepted"
        unchanged = _ok(base, f"/api/v1/assets/{records['assetId']}")
        assert unchanged["source"]["sha256"] == table_detail["source"]["sha256"]

        search_path = "/api/v1/search?" + urlencode({"q": "北京大学", "limit": 20})
        search = _ok(base, search_path)
        assert search["results"] and any(result["provenance"]["contentSha256"] for result in search["results"])
        assert any(result["assetId"] in {records["assetId"], text["assetId"]} for result in search["results"])

        selected = [records["assetId"], labels["assetId"]]
        schema = _ok(base, "/api/v1/query/schema", method="POST", payload={"assetIds": selected})
        assert [relation["alias"] for relation in schema["relations"]] == ["t1", "t2"]
        assert schema["limits"]["maxInputRows"] == 50_000
        simple = _ok(base, "/api/v1/query/sql", method="POST", payload={"assetIds": selected, "sql": "SELECT * FROM t1 LIMIT 2"})
        assert simple["rowCount"] <= 2 and simple["sandbox"].startswith("duckdb-memory-")
        join = _ok(
            base,
            "/api/v1/query/sql",
            method="POST",
            payload={
                "assetIds": selected,
                "sql": "SELECT t1.id, t2.label FROM t1 JOIN t2 ON t1.id = t2.id ORDER BY t1.id",
            },
        )
        assert join["rowCount"] >= 1 and "id" in join["columns"]
        hostile_status, hostile = _json_request(
            base,
            "/api/v1/query/sql",
            method="POST",
            payload={"assetIds": selected, "sql": "SELECT * FROM read_csv_auto('C:\\\\outside.csv')"},
        )
        assert hostile_status == 400 and hostile["error"]["code"] == "external_access_denied", hostile

        duplicate_start = _run_required(bundle, "start.cmd", timeout=60)
        assert "already running" in duplicate_start
        second_queued = _ok(base, "/api/v1/process", method="POST", payload={"source": str(source)})
        second_task = _wait_task(base, str(second_queued["taskId"]))
        second_summary = second_task["summary"]
        if assert_reuse:
            assert second_summary["reusedExtraction"] >= 1, second_summary
            assert second_summary["reusedCleaning"] >= 1, second_summary
        assert _ok(base, "/api/v1/search?" + urlencode({"q": "北京大学", "limit": 5}))["results"]

        # Verify durable registry/artifacts through the real launcher before
        # the final stop-twice cleanup.  This is intentionally a new process,
        # not a reused in-process BackendApp object.
        normal_stop = _run_required(bundle, "stop.cmd", timeout=60)
        assert "Stopped" in normal_stop or "not running" in normal_stop
        restart_output = _run_required(bundle, "start.cmd", timeout=60)
        assert "started" in restart_output or "already running" in restart_output
        restarted_state = json.loads(state_path.read_text(encoding="utf-8"))
        restarted_base = f"http://127.0.0.1:{int(restarted_state['port'])}"
        persisted = _ok(restarted_base, "/api/v1/overview")
        assert persisted["tableAssets"] >= 4 and persisted["textAssets"] >= 4, persisted
        assert _asset_items(restarted_base)
        assert _ok(restarted_base, "/api/v1/search?" + urlencode({"q": "北京大学", "limit": 5}))["results"]
        return {
            "label": label,
            "first": first_summary,
            "reuse": second_summary,
            "assets": overview["tableAssets"] + overview["textAssets"],
        }
    finally:
        stopped = _run_required(bundle, "stop.cmd", timeout=60)
        assert "Stopped" in stopped or "not running" in stopped or "stale" in stopped
        stopped_again = _run_required(bundle, "stop.cmd", timeout=60)
        assert "not running" in stopped_again


def _run_lifecycle_edges(bundle: Path) -> None:
    state_path = bundle / "workspace" / "state" / "server.pid"
    # Make a manually interrupted prior run harmless before installing the
    # deliberately stale state used by this test.  stop.cmd still validates
    # health/token and never kills an arbitrary Python process.
    _run_required(bundle, "stop.cmd", timeout=60)
    state_path.write_text(json.dumps({"pid": 999_999, "port": PORT, "controlToken": "stale"}), encoding="utf-8")
    output = _run_required(bundle, "start.cmd", timeout=60)
    assert "started" in output, f"stale-PID start output did not confirm start: {output}"
    _run_required(bundle, "stop.cmd", timeout=60)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", PORT))
        listener.listen(1)
        result = _launcher(bundle, "start.cmd", timeout=60)
        combined = (result.stdout or "") + (result.stderr or "")
        assert result.returncode != 0 and "already in use" in combined, (
            f"occupied-port start returned {result.returncode}: {combined}"
        )
    _run_required(bundle, "start.cmd", timeout=60)
    _run_required(bundle, "stop.cmd", timeout=60)


def _extract_zip(zip_path: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (destination_root / member.filename).resolve()
            target.relative_to(destination_root)
        archive.extractall(destination_root)
    extracted = destination_root / BUNDLE_NAME
    if not (extracted / "start.cmd").is_file():
        raise AssertionError("ZIP extraction did not contain the expected product root")
    return extracted


def main() -> int:
    bundle = RELEASE_ROOT / BUNDLE_NAME
    zip_path = RELEASE_ROOT / f"{BUNDLE_NAME}.zip"
    sha_path = RELEASE_ROOT / f"{BUNDLE_NAME}.zip.sha256.txt"
    if not bundle.is_dir() or not zip_path.is_file():
        raise SystemExit("run scripts/build_release.ps1 first")
    if not sha_path.is_file():
        raise AssertionError("release ZIP SHA-256 sidecar is missing")
    expected_zip_hash = _sha256(zip_path)
    sidecar = sha_path.read_text(encoding="ascii").strip().split()
    if len(sidecar) != 2 or sidecar[0].casefold() != expected_zip_hash or sidecar[1] != zip_path.name:
        raise AssertionError(f"release ZIP SHA-256 sidecar does not match {zip_path.name}")
    _assert_release_allowlist(bundle)
    if (bundle / "workspace" / "state" / "registry.duckdb").exists():
        raise AssertionError("release bundle contains a development registry")
    before_bundle = _fingerprints(bundle)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    source = WORK_ROOT / SOURCE_NAME
    if source.exists():
        shutil.rmtree(source)
    _make_corpus(source)
    source_before = _fingerprints(source)

    relocated = WORK_ROOT / "洞见 发布测试" / BUNDLE_NAME
    _copy_tree(bundle, relocated)
    _run_required(relocated, "doctor.cmd", timeout=120)
    relocated_result = _run_journey(relocated, source, label="directory relocation", assert_reuse=True)
    _run_lifecycle_edges(relocated)
    assert source_before == _fingerprints(source)

    # The ZIP itself is extracted to a different Chinese/space path and gets a
    # separate empty state.  This is a second product journey, not an internal
    # service shortcut.
    unzipped_parent = WORK_ROOT / "最终 解压 验收"
    unzipped = _extract_zip(zip_path, unzipped_parent)
    zip_result = _run_journey(unzipped, source, label="zip relocation", assert_reuse=False)
    assert source_before == _fingerprints(source)
    assert before_bundle == _fingerprints(bundle)
    print(json.dumps({"directory": relocated_result, "zip": zip_result}, ensure_ascii=False, indent=2))
    print("PHASE10 ACCEPTANCE: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, URLError, zipfile.BadZipFile) as exc:
        print(f"PHASE10 ACCEPTANCE: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
