"""Small PyMuPDF-only PDF fixtures used by the Phase 4A tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

import pymupdf


def write_pdf(
    path: Path,
    pages: Iterable[Mapping[str, Any]],
    *,
    title: str = "Synthetic DongJian PDF",
) -> Path:
    """Write deterministic-enough native, blank, image, and rotated pages.

    The helper intentionally uses only the production PyMuPDF dependency.  It
    is a test fixture writer, not a production PDF creation path.
    """

    document = pymupdf.open()
    document.set_metadata({"title": title, "producer": "DongJian synthetic fixture"})
    for specification in pages:
        page = document.new_page(
            width=float(specification.get("width", 595)),
            height=float(specification.get("height", 842)),
        )
        rotation = int(specification.get("rotation", 0) or 0)
        if rotation:
            page.set_rotation(rotation)
        for item in specification.get("texts", ()):
            x, y, text = item
            text_options = {"fontsize": 11}
            if specification.get("fontname"):
                text_options["fontname"] = str(specification["fontname"])
            page.insert_text((float(x), float(y)), str(text), **text_options)
        for item in specification.get("textboxes", ()):
            x0, y0, x1, y1, text = item
            page.insert_textbox(
                pymupdf.Rect(float(x0), float(y0), float(x1), float(y1)),
                str(text),
                fontsize=11,
            )
        for rectangle in specification.get("rectangles", ()):
            page.draw_rect(pymupdf.Rect(*[float(value) for value in rectangle]))
        for line in specification.get("lines", ()):
            x0, y0, x1, y1 = (float(value) for value in line)
            page.draw_line((x0, y0), (x1, y1))
        image_rect = specification.get("image_rect")
        if image_rect is not None:
            pixmap = pymupdf.Pixmap(
                pymupdf.csRGB,
                pymupdf.IRect(0, 0, 100, 100),
                False,
            )
            pixmap.clear_with(int(specification.get("image_color", 180)))
            page.insert_image(
                pymupdf.Rect(*[float(value) for value in image_rect]),
                pixmap=pixmap,
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    document.close()
    return path
