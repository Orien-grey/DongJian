"""Conservative, evidence-only PDF page and document profiling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from chongzu.assets import BoundingBox

from .blocks import ExtractedTextBlock


def _area(box: BoundingBox) -> float:
    return max(0.0, box.x1 - box.x0) * max(0.0, box.y1 - box.y0)


def _coverage(boxes: Iterable[BoundingBox], width: float, height: float) -> float:
    page_area = max(width * height, 0.0)
    if page_area <= 0:
        return 0.0
    # Overlap is deliberately not unioned. Capping keeps this a conservative
    # signal rather than pretending to be a pixel-accurate segmentation.
    return min(1.0, sum(_area(box) for box in boxes) / page_area)


def _aligned_text_columns(blocks: Sequence[ExtractedTextBlock]) -> bool:
    if len(blocks) < 4:
        return False
    rows: list[list[ExtractedTextBlock]] = []
    for block in sorted(blocks, key=lambda item: (item.bbox.y0, item.bbox.x0)):
        for row in rows:
            if abs(row[0].bbox.y0 - block.bbox.y0) <= 4.0:
                row.append(block)
                break
        else:
            rows.append([block])
    aligned_rows = [row for row in rows if len(row) >= 3]
    return any(len({round(block.bbox.x0, 1) for block in row}) >= 3 for row in aligned_rows)


def page_table_hint(
    blocks: Sequence[ExtractedTextBlock],
    *,
    grid_line_count: int,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if _aligned_text_columns(blocks):
        reasons.append("aligned_text_columns")
    short_blocks = [block for block in blocks if block.effective_char_count <= 40]
    if len(short_blocks) >= 8 and len(short_blocks) / max(len(blocks), 1) >= 0.7:
        reasons.append("many_short_text_blocks")
    if grid_line_count >= 4:
        reasons.append("page_drawings_grid_like")
    return bool(reasons), tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class PageProfile:
    page_number: int
    width: float
    height: float
    rotation: int
    total_chars: int
    effective_chars: int
    text_block_count: int
    image_count: int
    image_area_coverage_ratio: float
    text_area_coverage_ratio: float
    drawing_count: int
    grid_line_count: int
    native_text_available: bool
    suspected_scanned: bool
    possible_table_candidate: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "total_chars": self.total_chars,
            "effective_chars": self.effective_chars,
            "text_block_count": self.text_block_count,
            "image_count": self.image_count,
            "image_area_coverage_ratio": self.image_area_coverage_ratio,
            "text_area_coverage_ratio": self.text_area_coverage_ratio,
            "drawing_count": self.drawing_count,
            "grid_line_count": self.grid_line_count,
            "native_text_available": self.native_text_available,
            "suspected_scanned": self.suspected_scanned,
            "possible_table_candidate": self.possible_table_candidate,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class PDFProfile:
    profile_version: str
    page_count: int
    total_chars: int
    chars_per_page: tuple[int, ...]
    text_block_count: int
    image_count: int
    pages_with_text: tuple[int, ...]
    pages_without_text: tuple[int, ...]
    text_coverage_ratio: float
    native_text_available: bool
    suspected_scanned_pages: tuple[int, ...]
    classification: str
    reason_codes: tuple[str, ...]
    pages: tuple[PageProfile, ...]
    metadata: Mapping[str, Any]
    elapsed_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_version": self.profile_version,
            "page_count": self.page_count,
            "total_chars": self.total_chars,
            "chars_per_page": list(self.chars_per_page),
            "text_block_count": self.text_block_count,
            "image_count": self.image_count,
            "pages_with_text": list(self.pages_with_text),
            "pages_without_text": list(self.pages_without_text),
            "text_coverage_ratio": self.text_coverage_ratio,
            "native_text_available": self.native_text_available,
            "suspected_scanned_pages": list(self.suspected_scanned_pages),
            "classification": self.classification,
            "reason_codes": list(self.reason_codes),
            "pages": [page.as_dict() for page in self.pages],
            "metadata": dict(self.metadata),
            "elapsed_ms": self.elapsed_ms,
        }


def profile_page(
    *,
    page_number: int,
    width: float,
    height: float,
    rotation: int,
    blocks: Sequence[ExtractedTextBlock],
    image_boxes: Sequence[BoundingBox],
    image_count: int,
    drawing_count: int,
    grid_line_count: int,
) -> PageProfile:
    total_chars = sum(block.char_count for block in blocks)
    nonempty_chars = sum(block.effective_char_count for block in blocks)
    native_text = nonempty_chars > 0
    image_coverage = _coverage(image_boxes, width, height)
    text_coverage = _coverage([block.bbox for block in blocks], width, height)
    suspected = (
        image_count > 0
        and nonempty_chars < 20
        and image_coverage >= 0.35
    )
    table_candidate, table_reasons = page_table_hint(blocks, grid_line_count=grid_line_count)
    reasons: list[str] = list(table_reasons)
    if native_text:
        reasons.append("native_text_blocks")
    else:
        reasons.append("no_effective_text")
    if image_count:
        reasons.append("embedded_images")
    if suspected:
        reasons.append("low_text_with_large_image")
    elif image_count and nonempty_chars < 20:
        reasons.append("low_text_but_scanned_signal_inconclusive")
    return PageProfile(
        page_number=page_number,
        width=float(width),
        height=float(height),
        rotation=int(rotation or 0),
        total_chars=total_chars,
        effective_chars=nonempty_chars,
        text_block_count=len(blocks),
        image_count=int(image_count),
        image_area_coverage_ratio=image_coverage,
        text_area_coverage_ratio=text_coverage,
        drawing_count=int(drawing_count),
        grid_line_count=int(grid_line_count),
        native_text_available=native_text,
        suspected_scanned=suspected,
        possible_table_candidate=table_candidate,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def build_pdf_profile(
    *,
    pages: Sequence[PageProfile],
    metadata: Mapping[str, Any],
    elapsed_ms: float,
    profile_version: str = "pdf-profile-v1",
) -> PDFProfile:
    page_count = len(pages)
    with_text = tuple(page.page_number for page in pages if page.native_text_available)
    without_text = tuple(page.page_number for page in pages if not page.native_text_available)
    suspected = tuple(page.page_number for page in pages if page.suspected_scanned)
    total_chars = sum(page.total_chars for page in pages)
    image_count = sum(page.image_count for page in pages)
    reasons: list[str] = []
    if page_count == 0:
        classification = "unknown"
        reasons.append("no_pages")
    elif suspected and with_text:
        classification = "mixed"
        reasons.append("native_and_suspected_scanned_pages")
    elif suspected and len(suspected) * 2 >= page_count:
        classification = "suspected_scanned"
        reasons.append("most_pages_have_scanned_signal")
    elif len(with_text) == page_count and page_count > 0:
        classification = "native_text"
        reasons.append("all_pages_have_native_text")
    elif with_text:
        classification = "mixed"
        reasons.append("text_and_blank_pages")
    else:
        classification = "unknown"
        reasons.append("no_native_text_and_no_scanned_consensus")
    if any(page.possible_table_candidate for page in pages):
        reasons.append("possible_table_candidate_present")
    return PDFProfile(
        profile_version=profile_version,
        page_count=page_count,
        total_chars=total_chars,
        chars_per_page=tuple(page.total_chars for page in pages),
        text_block_count=sum(page.text_block_count for page in pages),
        image_count=image_count,
        pages_with_text=with_text,
        pages_without_text=without_text,
        text_coverage_ratio=(len(with_text) / page_count) if page_count else 0.0,
        native_text_available=bool(with_text),
        suspected_scanned_pages=suspected,
        classification=classification,
        reason_codes=tuple(dict.fromkeys(reasons)),
        pages=tuple(pages),
        metadata=dict(metadata),
        elapsed_ms=float(elapsed_ms),
    )
