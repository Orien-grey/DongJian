"""Precision-first table-region detection for worksheet cell matrices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


def is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


@dataclass(frozen=True)
class TableRegion:
    """A zero-based, half-open source range."""

    row_start: int
    row_end: int
    column_start: int
    column_end: int
    warnings: tuple[str, ...] = ()


def _bands(non_empty: Sequence[bool]) -> list[tuple[int, int]]:
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for index, occupied in enumerate((*non_empty, False)):
        if occupied and start is None:
            start = index
        elif not occupied and start is not None:
            bands.append((start, index))
            start = None
    return bands


def detect_table_regions(rows: Sequence[Sequence[Any]]) -> list[TableRegion]:
    """Return conservative regions separated only by completely blank lines.

    A title or footnote adjacent to a table remains in the same region.  That
    is deliberate: uncertain boundaries are flagged for review instead of
    being cut by an aggressive heuristic.
    """

    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return []
    padded = [list(row) + [None] * (width - len(row)) for row in rows]
    row_bands = _bands([any(not is_empty(value) for value in row) for row in padded])
    regions: list[TableRegion] = []
    for row_start, row_end in row_bands:
        column_bands = _bands(
            [any(not is_empty(padded[row][column]) for row in range(row_start, row_end)) for column in range(width)]
        )
        for column_start, column_end in column_bands:
            warnings: list[str] = []
            region_rows = [row[column_start:column_end] for row in padded[row_start:row_end]]
            occupied = [sum(not is_empty(value) for value in row) for row in region_rows]
            if len(occupied) > 1 and occupied[0] == 1 and max(occupied[1:], default=0) > 1:
                warnings.extend(("possible_title_row", "ambiguous_table_boundary"))
            if len(region_rows) >= 3:
                first_two_text = all(
                    is_empty(value) or isinstance(value, str)
                    for row in region_rows[:2]
                    for value in row
                )
                later_non_text = any(
                    not is_empty(value) and not isinstance(value, str)
                    for row in region_rows[2:]
                    for value in row
                )
                if first_two_text and later_non_text:
                    warnings.append("possible_multirow_header")
            regions.append(TableRegion(row_start, row_end, column_start, column_end, tuple(dict.fromkeys(warnings))))
    if len(regions) > 1:
        regions = [
            TableRegion(
                region.row_start,
                region.row_end,
                region.column_start,
                region.column_end,
                tuple(dict.fromkeys((*region.warnings, "multiple_table_regions"))),
            )
            for region in regions
        ]
    return regions
