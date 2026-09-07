"""Strict local validation for model-produced Vision extraction JSON."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


MAX_TITLE_CHARS = 2_000
MAX_TEXT_ITEMS = 200
MAX_TEXT_CHARS = 20_000
MAX_TOTAL_TEXT_CHARS = 100_000
MAX_TABLES = 32
MAX_COLUMNS = 128
MAX_ROWS = 10_000
MAX_CELL_CHARS = 8_000
MAX_TOTAL_CELLS = 500_000

PAGE_TYPES = frozenset({"text", "table", "mixed", "other"})
TEXT_ROLES = frozenset({"title", "body", "caption", "note", "other"})
ROOT_KEYS = frozenset({"page_type", "title", "useful_text", "tables", "confidence"})
TEXT_KEYS = frozenset({"text", "role"})
TABLE_KEYS = frozenset({"title", "columns", "rows", "confidence"})


class VisionContractError(ValueError):
    """Raised when a provider response cannot become a local asset."""


@dataclass(frozen=True)
class VisionText:
    text: str
    role: str


@dataclass(frozen=True)
class VisionTable:
    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    confidence: float | None = None


@dataclass(frozen=True)
class VisionDocument:
    page_type: str
    title: str
    useful_text: tuple[VisionText, ...]
    tables: tuple[VisionTable, ...]
    confidence: float | None = None

    @property
    def is_empty(self) -> bool:
        return not self.title.strip() and not any(item.text.strip() for item in self.useful_text) and not self.tables


def _keys(
    value: Mapping[str, Any],
    expected: frozenset[str],
    name: str,
    *,
    required: frozenset[str] | None = None,
) -> None:
    unknown = set(value) - expected
    missing = (required or expected) - set(value)
    if unknown:
        raise VisionContractError(f"{name} contains unsupported fields: {', '.join(sorted(map(str, unknown)))}")
    if missing:
        raise VisionContractError(f"{name} is missing required fields: {', '.join(sorted(missing))}")


def _string(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise VisionContractError(f"{name} must be a string")
    if len(value) > maximum:
        raise VisionContractError(f"{name} exceeds the local size limit")
    return value


def _confidence(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise VisionContractError(f"{name} must be a finite number from 0 to 1 or null")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise VisionContractError(f"{name} must be from 0 to 1")
    return number


def _scalar(value: Any, name: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
            raise VisionContractError(f"{name} exceeds the local cell size limit")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise VisionContractError(f"{name} must be finite")
        return value
    raise VisionContractError(f"{name} must be a scalar")


def validate_vision_payload(value: Any) -> VisionDocument:
    """Validate and normalize one decoded provider JSON value."""

    if not isinstance(value, Mapping):
        raise VisionContractError("Vision response root must be a JSON object")
    _keys(value, ROOT_KEYS, "root", required=frozenset({"page_type", "title", "useful_text", "tables"}))
    page_type = _string(value["page_type"], "page_type", maximum=16)
    if page_type not in PAGE_TYPES:
        raise VisionContractError("page_type is not supported")
    title = _string(value["title"], "title", maximum=MAX_TITLE_CHARS)
    useful_text_value = value["useful_text"]
    if not isinstance(useful_text_value, list):
        raise VisionContractError("useful_text must be an array")
    if len(useful_text_value) > MAX_TEXT_ITEMS:
        raise VisionContractError("useful_text contains too many items")
    useful_text: list[VisionText] = []
    total_text_chars = len(title)
    for index, item in enumerate(useful_text_value):
        if not isinstance(item, Mapping):
            raise VisionContractError(f"useful_text[{index}] must be an object")
        _keys(item, TEXT_KEYS, f"useful_text[{index}]")
        text = _string(item["text"], f"useful_text[{index}].text", maximum=MAX_TEXT_CHARS)
        role = _string(item["role"], f"useful_text[{index}].role", maximum=16)
        if role not in TEXT_ROLES:
            raise VisionContractError(f"useful_text[{index}].role is not supported")
        total_text_chars += len(text)
        if total_text_chars > MAX_TOTAL_TEXT_CHARS:
            raise VisionContractError("useful_text exceeds the local total size limit")
        useful_text.append(VisionText(text=text, role=role))

    tables_value = value["tables"]
    if not isinstance(tables_value, list):
        raise VisionContractError("tables must be an array")
    if len(tables_value) > MAX_TABLES:
        raise VisionContractError("tables contains too many tables")
    tables: list[VisionTable] = []
    total_cells = 0
    for table_index, item in enumerate(tables_value):
        if not isinstance(item, Mapping):
            raise VisionContractError(f"tables[{table_index}] must be an object")
        _keys(item, TABLE_KEYS, f"tables[{table_index}]", required=frozenset({"title", "columns", "rows"}))
        table_title = _string(item["title"], f"tables[{table_index}].title", maximum=MAX_TITLE_CHARS)
        columns_value = item["columns"]
        if not isinstance(columns_value, list) or not columns_value:
            raise VisionContractError(f"tables[{table_index}].columns must be a non-empty array")
        if len(columns_value) > MAX_COLUMNS:
            raise VisionContractError(f"tables[{table_index}] has too many columns")
        columns = tuple(
            _string(column, f"tables[{table_index}].columns[{column_index}]", maximum=MAX_CELL_CHARS)
            for column_index, column in enumerate(columns_value)
        )
        rows_value = item["rows"]
        if not isinstance(rows_value, list):
            raise VisionContractError(f"tables[{table_index}].rows must be an array")
        if len(rows_value) > MAX_ROWS:
            raise VisionContractError(f"tables[{table_index}] has too many rows")
        rows: list[tuple[Any, ...]] = []
        for row_index, row in enumerate(rows_value):
            if not isinstance(row, list):
                raise VisionContractError(f"tables[{table_index}].rows[{row_index}] must be an array")
            if len(row) != len(columns):
                raise VisionContractError(f"tables[{table_index}].rows[{row_index}] has inconsistent width")
            total_cells += len(row)
            if total_cells > MAX_TOTAL_CELLS:
                raise VisionContractError("Vision tables contain too many cells")
            rows.append(
                tuple(
                    _scalar(cell, f"tables[{table_index}].rows[{row_index}][{column_index}]")
                    for column_index, cell in enumerate(row)
                )
            )
        tables.append(
            VisionTable(
                title=table_title,
                columns=columns,
                rows=tuple(rows),
                confidence=_confidence(item.get("confidence"), f"tables[{table_index}].confidence")
                if "confidence" in item
                else None,
            )
        )
    return VisionDocument(
        page_type=page_type,
        title=title,
        useful_text=tuple(useful_text),
        tables=tuple(tables),
        confidence=_confidence(value.get("confidence"), "confidence") if "confidence" in value else None,
    )
