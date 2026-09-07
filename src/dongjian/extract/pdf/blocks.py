"""Text-block extraction, safe text normalization, and deterministic chunks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import unicodedata

from dongjian.assets import BoundingBox


@dataclass(frozen=True)
class ExtractedTextBlock:
    """One native text block as reported by PyMuPDF."""

    block_index: int
    bbox: BoundingBox
    raw_text: str

    @property
    def normalized_text(self) -> str:
        return normalize_text(self.raw_text)

    @property
    def char_count(self) -> int:
        return len(self.raw_text)

    @property
    def effective_char_count(self) -> int:
        return len("".join(self.raw_text.split()))


@dataclass(frozen=True)
class ChunkSlice:
    chunk_index: int
    char_start: int
    char_end: int
    text: str


def normalize_text(value: str) -> str:
    """Apply only reversible, deterministic text hygiene."""

    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    # Keep tabs and line boundaries, but remove NUL and other control garbage
    # that can make JSON/terminal/catalog consumers unsafe or misleading.
    normalized = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or ord(character) >= 32
    )
    return normalized


def _bbox(value: Any) -> BoundingBox | None:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return None
    try:
        return BoundingBox(*(float(item) for item in value))
    except (TypeError, ValueError):
        return None


def _line_text(line: dict[str, Any]) -> str:
    spans = line.get("spans", [])
    if not isinstance(spans, list):
        return ""
    return "".join(str(span.get("text", "")) for span in spans if isinstance(span, dict))


def extract_text_blocks(page_dict: dict[str, Any]) -> list[ExtractedTextBlock]:
    """Convert the PyMuPDF ``dict`` representation into text-only blocks."""

    result: list[ExtractedTextBlock] = []
    blocks = page_dict.get("blocks", [])
    if not isinstance(blocks, list):
        return result
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != 0:
            continue
        box = _bbox(block.get("bbox"))
        if box is None:
            continue
        lines = block.get("lines", [])
        if isinstance(lines, list):
            text = "\n".join(
                _line_text(line) for line in lines if isinstance(line, dict)
            )
        else:
            text = str(block.get("text", ""))
        if text.strip():
            result.append(ExtractedTextBlock(block_index=block_index, bbox=box, raw_text=text))
    return result


def image_block_bboxes(page_dict: dict[str, Any]) -> list[BoundingBox]:
    blocks = page_dict.get("blocks", [])
    if not isinstance(blocks, list):
        return []
    boxes: list[BoundingBox] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != 1:
            continue
        box = _bbox(block.get("bbox"))
        if box is not None:
            boxes.append(box)
    return boxes


def image_info_bboxes(page: Any) -> list[BoundingBox]:
    """Return image placements without loading embedded image bytes."""

    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:  # pragma: no cover - depends on malformed page content
        return []
    if not isinstance(infos, list):
        return []
    boxes: list[BoundingBox] = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        box = _bbox(info.get("bbox"))
        if box is not None:
            boxes.append(box)
    return boxes


def chunk_text(
    text: str,
    *,
    max_chars: int = 2000,
    overlap: int = 0,
) -> list[ChunkSlice]:
    """Split normalized text at paragraph/newline boundaries when possible."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be between 0 and max_chars - 1")
    if not text:
        return []
    slices: list[ChunkSlice] = []
    start = 0
    chunk_index = 0
    while start < len(text):
        proposed_end = min(len(text), start + max_chars)
        end = proposed_end
        if proposed_end < len(text):
            # Prefer a paragraph/newline boundary after the first half of the
            # proposed chunk; otherwise use the hard character boundary.
            boundary_floor = start + max(1, max_chars // 2)
            boundary = text.rfind("\n", boundary_floor, proposed_end)
            if boundary > start:
                end = boundary + 1
        slices.append(ChunkSlice(chunk_index, start, end, text[start:end]))
        if end >= len(text):
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start
        chunk_index += 1
    return slices


def effective_chars(text: str) -> int:
    return len("".join(text.split()))
