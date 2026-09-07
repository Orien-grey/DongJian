"""Local-only browser acceptance fixture server.

This is a development harness. It creates synthetic inputs below
``cache/temp``, runs the real local pipeline, starts the real HTTP app, and
uses a fake semantic provider. It is not part of the portable runtime or
release payload.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import time
import zipfile
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dongjian.services.process as process_module
from dongjian.api.app import BackendApp
from dongjian.api.server import DongJianHTTPServer
from dongjian.clean import process_source
from dongjian.semantic.fake_provider import FakeSemanticProvider
from dongjian.services.file_insight import FileInsightPolicyStore
from tests.pdf_factory import write_pdf
from tests.xlsx_factory import write_xlsx


TARGET = ROOT / "cache" / "temp" / "browser-acceptance"


def _write_docx(path: Path) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>'
        '<w:p><w:r><w:t xml:space="preserve">DOCX introduction for locator</w:t></w:r></w:p>'
        '<w:tbl><w:tblGrid><w:gridCol w:w="1800"/><w:gridCol w:w="1800"/></w:tblGrid>'
        '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
        '<w:p><w:r><w:t>Form title</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>North</w:t></w:r></w:p></w:tc></w:tr>'
        '</w:tbl><w:p><w:r><w:t xml:space="preserve">DOCX conclusion</w:t></w:r></w:p>'
        '<w:sectPr/></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)


def _write_table_image(path: Path) -> None:
    image = Image.new("RGB", (640, 420), "white")
    draw = ImageDraw.Draw(image)
    for x in (70, 280, 500):
        draw.line((x, 80, x, 330), fill="black", width=3)
    for y in (80, 145, 210, 275, 330):
        draw.line((70, y, 500, y), fill="black", width=3)
    draw.text((90, 98), "Region", fill="black")
    draw.text((300, 98), "Value", fill="black")
    draw.text((90, 163), "North", fill="black")
    draw.text((300, 163), "12", fill="black")
    draw.text((90, 228), "South", fill="black")
    draw.text((300, 228), "9", fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def make_fixture() -> tuple[Path, Path, Path]:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    static_source = TARGET / "static-input"
    dynamic_source = TARGET / "dynamic-input"
    workspace = TARGET / "workspace"
    static_source.mkdir(parents=True)
    dynamic_source.mkdir(parents=True)

    write_pdf(
        static_source / "two-pages.pdf",
        [
            {"texts": [(72, 72, "PDF page one")], "rectangles": [(40, 40, 260, 180)]},
            {"texts": [(72, 72, "PDF page two")], "rectangles": [(40, 40, 260, 180)]},
        ],
    )
    write_xlsx(
        static_source / "book.xlsx",
        [
            ("Sheet A", [["name", "value"], ["North", "12"]], None),
            ("Sheet B", [["name", "value"], ["South", "9"]], None),
        ],
    )
    _write_docx(static_source / "form.docx")
    (static_source / "notes.txt").write_text(
        "TXT continuous reading. Search needle is here for locator testing; another needle is below.\n",
        encoding="utf-8",
    )
    _write_table_image(static_source / "table.png")
    for index in range(6):
        (dynamic_source / f"dynamic-{index}.txt").write_text(
            f"Dynamic file {index}; local processing remains usable while the next file is running.\n",
            encoding="utf-8",
        )
    return static_source, dynamic_source, workspace


class SlowFakeProvider(FakeSemanticProvider):
    def generate(self, request):  # type: ignore[no-untyped-def]
        time.sleep(0.35)
        return super().generate(request)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    static_source, dynamic_source, workspace = make_fixture()
    registry_path = workspace / "state" / "registry.duckdb"
    process_source(
        static_source,
        workers=1,
        force=True,
        registry_path=registry_path,
        workspace_root=workspace,
    )
    print("STATIC_DONE", flush=True)
    FileInsightPolicyStore(workspace).save(True)

    # Keep the real task coordinator and local pipeline, adding only a small
    # delay after each durable local-ready event so the browser can observe
    # overlapping UI states without making the product itself slower.
    real_process_source = process_module.process_source

    def delayed_process(*process_args, **process_kwargs):  # type: ignore[no-untyped-def]
        callback = process_kwargs.get("progress_callback")

        def observed(stage, progress, **metadata):  # type: ignore[no-untyped-def]
            if callback is not None:
                callback(stage, progress, **metadata)
            if stage == "local_ready":
                time.sleep(0.22)

        process_kwargs["progress_callback"] = observed
        return real_process_source(*process_args, **process_kwargs)

    process_module.process_source = delayed_process
    print("APP_INIT", flush=True)
    app = BackendApp(
        project_root=ROOT,
        registry_path=registry_path,
        workspace_root=workspace,
        frontend_dist=ROOT / "frontend" / "dist",
        semantic_provider=SlowFakeProvider(),
    )
    print("APP_READY", flush=True)
    server = DongJianHTTPServer(("127.0.0.1", args.port), app)
    print(f"PORT={server.server_address[1]}", flush=True)
    print(f"STATIC_SOURCE={static_source}", flush=True)
    print(f"DYNAMIC_SOURCE={dynamic_source}", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
