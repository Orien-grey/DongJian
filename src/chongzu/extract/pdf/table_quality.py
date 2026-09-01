"""Deterministic ground-truth scoring for the PDF table candidate."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
import unicodedata


CellValue = str | int | float | bool | None


def _rows(value: Iterable[Iterable[Any]]) -> tuple[tuple[CellValue, ...], ...]:
    return tuple(tuple(cell for cell in row) for row in value)


@dataclass(frozen=True)
class ExpectedTable:
    """A known table in a synthetic or manually annotated PDF."""

    page_number: int
    rows: tuple[tuple[CellValue, ...], ...]
    table_index: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("expected table page_number must be one-based")
        if self.table_index is not None and self.table_index < 0:
            raise ValueError("expected table_index must be non-negative")
        object.__setattr__(self, "rows", _rows(self.rows))

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)


@dataclass(frozen=True)
class DetectedTable:
    """A candidate matrix retained for scoring before catalog publication."""

    page_number: int
    table_index: int
    rows: tuple[tuple[CellValue, ...], ...]
    table_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("detected table page_number must be one-based")
        if self.table_index < 0:
            raise ValueError("detected table_index must be non-negative")
        object.__setattr__(self, "rows", _rows(self.rows))

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)


def normalize_cell(value: Any) -> str | None:
    """Normalize only Unicode and outer whitespace for comparison."""

    if value is None:
        return None
    return unicodedata.normalize("NFC", str(value)).strip()


@dataclass(frozen=True)
class GroundTruthScore:
    expected_tables: int
    detected_tables: int
    true_positives: int
    false_negatives: int
    obvious_false_positives: int
    exact_shape_matches: int
    row_count_matches: int
    column_count_matches: int
    expected_cells: int
    detected_cells: int
    exact_cell_matches: int
    normalized_cell_matches: int
    missing_cells: int
    extra_cells: int

    def as_dict(self) -> dict[str, int]:
        return {
            "expected_tables": self.expected_tables,
            "detected_tables": self.detected_tables,
            "true_positives": self.true_positives,
            "false_negatives": self.false_negatives,
            "obvious_false_positives": self.obvious_false_positives,
            "exact_shape_matches": self.exact_shape_matches,
            "row_count_matches": self.row_count_matches,
            "column_count_matches": self.column_count_matches,
            "expected_cells": self.expected_cells,
            "detected_cells": self.detected_cells,
            "exact_cell_matches": self.exact_cell_matches,
            "normalized_cell_matches": self.normalized_cell_matches,
            "missing_cells": self.missing_cells,
            "extra_cells": self.extra_cells,
        }


def _cell(rows: tuple[tuple[CellValue, ...], ...], row: int, column: int) -> tuple[bool, CellValue]:
    if row >= len(rows) or column >= len(rows[row]):
        return False, None
    return True, rows[row][column]


def score_tables(
    expected: Iterable[ExpectedTable],
    detected: Iterable[DetectedTable],
) -> GroundTruthScore:
    """Match tables by page and order, then report granular correctness counts."""

    expected_items = sorted(tuple(expected), key=lambda item: (item.page_number, item.table_index or 0))
    detected_items = sorted(tuple(detected), key=lambda item: (item.page_number, item.table_index))
    unmatched = list(detected_items)
    true_positives = 0
    false_negatives = 0
    exact_shape_matches = 0
    row_count_matches = 0
    column_count_matches = 0
    expected_cells = 0
    detected_cells = 0
    exact_cell_matches = 0
    normalized_cell_matches = 0
    missing_cells = 0
    extra_cells = 0

    for expected_table in expected_items:
        candidate_index = next(
            (
                index
                for index, candidate in enumerate(unmatched)
                if candidate.page_number == expected_table.page_number
                and (
                    expected_table.table_index is None
                    or candidate.table_index == expected_table.table_index
                )
            ),
            None,
        )
        if candidate_index is None:
            false_negatives += 1
            expected_cells += expected_table.row_count * expected_table.column_count
            missing_cells += expected_table.row_count * expected_table.column_count
            continue

        candidate = unmatched.pop(candidate_index)
        true_positives += 1
        if expected_table.row_count == candidate.row_count:
            row_count_matches += 1
        if expected_table.column_count == candidate.column_count:
            column_count_matches += 1
        if (
            expected_table.row_count == candidate.row_count
            and expected_table.column_count == candidate.column_count
        ):
            exact_shape_matches += 1

        expected_cells += expected_table.row_count * expected_table.column_count
        detected_cells += candidate.row_count * candidate.column_count
        for row_index in range(max(expected_table.row_count, candidate.row_count)):
            for column_index in range(max(expected_table.column_count, candidate.column_count)):
                expected_present, expected_value = _cell(expected_table.rows, row_index, column_index)
                detected_present, detected_value = _cell(candidate.rows, row_index, column_index)
                if expected_present and not detected_present:
                    missing_cells += 1
                elif detected_present and not expected_present:
                    extra_cells += 1
                elif expected_present and detected_present:
                    if expected_value == detected_value:
                        exact_cell_matches += 1
                    if normalize_cell(expected_value) == normalize_cell(detected_value):
                        normalized_cell_matches += 1

    for candidate in unmatched:
        detected_cells += candidate.row_count * candidate.column_count
        # An unmatched candidate is an obvious false positive.  Count its
        # cells separately so a benchmark can distinguish extra tables from
        # value mismatches inside matched tables.
        extra_cells += candidate.row_count * candidate.column_count

    return GroundTruthScore(
        expected_tables=len(expected_items),
        detected_tables=len(detected_items),
        true_positives=true_positives,
        false_negatives=false_negatives,
        obvious_false_positives=len(unmatched),
        exact_shape_matches=exact_shape_matches,
        row_count_matches=row_count_matches,
        column_count_matches=column_count_matches,
        expected_cells=expected_cells,
        detected_cells=detected_cells,
        exact_cell_matches=exact_cell_matches,
        normalized_cell_matches=normalized_cell_matches,
        missing_cells=missing_cells,
        extra_cells=extra_cells,
    )


def load_ground_truth(path: Path) -> dict[str, tuple[ExpectedTable, ...]]:
    """Load the small JSON sidecar format used by synthetic/annotated corpora."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("ground truth must be a JSON object")
    files = payload.get("files", payload)
    if not isinstance(files, dict):
        raise ValueError("ground truth files must be a JSON object")
    result: dict[str, tuple[ExpectedTable, ...]] = {}
    for relative_path, value in files.items():
        table_values = value.get("tables", []) if isinstance(value, dict) else value
        if not isinstance(table_values, list):
            raise ValueError(f"ground truth tables for {relative_path!r} must be a list")
        tables: list[ExpectedTable] = []
        for table in table_values:
            if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
                raise ValueError(f"ground truth table for {relative_path!r} is invalid")
            bbox = table.get("bbox")
            parsed_bbox = tuple(float(item) for item in bbox) if isinstance(bbox, list) and len(bbox) == 4 else None
            tables.append(
                ExpectedTable(
                    page_number=int(table.get("page_number", 1)),
                    table_index=(int(table["table_index"]) if table.get("table_index") is not None else None),
                    rows=_rows(table["rows"]),
                    bbox=parsed_bbox,
                )
            )
        # Registry relative paths use POSIX separators even on Windows. Accept
        # either spelling in a hand-authored sidecar so benchmark annotations
        # remain portable across editors and machines.
        normalized_path = str(relative_path).replace("\\", "/")
        result[normalized_path] = tuple(tables)
    return result


def score_to_json(score: GroundTruthScore | None) -> Mapping[str, int] | None:
    return score.as_dict() if score is not None else None
