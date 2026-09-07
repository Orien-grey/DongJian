"""Small stdlib XLSX geometry reader for the reading view.

Calamine remains the value/table extractor.  This module reads only the OOXML
presentation facts needed to preserve worksheet structure; it is not an Excel
renderer and has no effect on normalized Parquet data.
"""

from __future__ import annotations

from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_REF = re.compile(r"^([A-Z]+)([0-9]+)$")


def _tag(name: str) -> str:
    return f"{{{NS_MAIN}}}{name}"


def _column_number(value: str) -> int:
    result = 0
    for character in value:
        result = result * 26 + ord(character) - 64
    return result


def _cell_position(value: str) -> tuple[int, int] | None:
    match = CELL_REF.match(value.upper())
    if not match:
        return None
    return int(match.group(2)), _column_number(match.group(1))


def _shared_strings(root: ET.Element) -> list[str]:
    result: list[str] = []
    for item in root.findall(_tag("si")):
        result.append("".join(node.text or "" for node in item.iter(_tag("t"))))
    return result


def _styles(root: ET.Element | None) -> dict[int, dict[str, str]]:
    if root is None:
        return {}
    builtin = {
        0: "General", 1: "0", 2: "0.00", 14: "mm-dd-yy", 22: "m/d/yy h:mm",
    }
    num_formats = {int(item.get("numFmtId")): item.get("formatCode", "") for item in root.findall(f"{_tag('numFmts')}/{_tag('numFmt')}")}
    result: dict[int, dict[str, str]] = {}
    xfs = root.findall(f"{_tag('cellXfs')}/{_tag('xf')}")
    for index, xf in enumerate(xfs):
        num_id = int(xf.get("numFmtId", 0) or 0)
        alignment = xf.find(_tag("alignment"))
        result[index] = {
            "number_format": num_formats.get(num_id, builtin.get(num_id, "General")),
            "horizontal": str(alignment.get("horizontal")) if alignment is not None and alignment.get("horizontal") else "",
            "vertical": str(alignment.get("vertical")) if alignment is not None and alignment.get("vertical") else "",
            "wrap_text": str(alignment.get("wrapText")) if alignment is not None and alignment.get("wrapText") else "",
        }
    return result


def read_xlsx_presentation(path: Path, sheet_index: int) -> dict[str, object] | None:
    if path.suffix.casefold() != ".xlsx":
        return None
    try:
        with zipfile.ZipFile(path, "r") as package:
            workbook = ET.fromstring(package.read("xl/workbook.xml"))
            relationships = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
            rel_targets = {
                item.get("Id"): item.get("Target")
                for item in relationships
                if item.get("Id") and item.get("Target")
            }
            sheets = workbook.findall(f"{{{NS_MAIN}}}sheets/{{{NS_MAIN}}}sheet")
            if sheet_index < 0 or sheet_index >= len(sheets):
                return None
            relationship_id = sheets[sheet_index].get(f"{{{NS_REL}}}id")
            target = rel_targets.get(relationship_id)
            if not target:
                return None
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            sheet_root = ET.fromstring(package.read(target))
            shared = _shared_strings(ET.fromstring(package.read("xl/sharedStrings.xml"))) if "xl/sharedStrings.xml" in package.namelist() else []
            styles = _styles(ET.fromstring(package.read("xl/styles.xml"))) if "xl/styles.xml" in package.namelist() else {}
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile, ValueError):
        return None

    merges = [item.get("ref") for item in sheet_root.findall(f"{_tag('mergeCells')}/{_tag('mergeCell')}") if item.get("ref")]
    row_heights: dict[str, object] = {}
    hidden_rows: list[int] = []
    cells: list[dict[str, object]] = []
    for row in sheet_root.findall(f"{_tag('sheetData')}/{_tag('row')}"):
        row_number = int(row.get("r", 0) or 0)
        if row.get("ht") is not None:
            row_heights[str(row_number)] = float(row.get("ht", 0) or 0)
        if row.get("hidden") in {"1", "true"}:
            hidden_rows.append(row_number)
        for cell in row.findall(_tag("c")):
            ref = str(cell.get("r") or "")
            position = _cell_position(ref)
            if position is None:
                continue
            value = cell.find(_tag("v"))
            inline = cell.find(_tag("is"))
            raw_value = "".join(node.text or "" for node in inline.iter(_tag("t"))) if inline is not None else (value.text if value is not None else None)
            if cell.get("t") == "s" and raw_value is not None:
                try:
                    raw_value = shared[int(raw_value)]
                except (IndexError, ValueError):
                    pass
            style_id = int(cell.get("s", 0) or 0)
            style = styles.get(style_id, {})
            item: dict[str, object] = {
                "coordinate": ref,
                "row": position[0] - 1,
                "column": position[1] - 1,
                "value": raw_value,
                "style_id": style_id,
            }
            if cell.find(_tag("f")) is not None:
                item["formula"] = cell.find(_tag("f")).text or ""
            if style:
                item["number_format"] = style.get("number_format")
                item["alignment"] = {key: value for key, value in style.items() if key != "number_format" and value}
            cells.append(item)
    columns: list[dict[str, object]] = []
    for col in sheet_root.findall(f"{_tag('cols')}/{_tag('col')}"):
        item: dict[str, object] = {"min": int(col.get("min", 0) or 0), "max": int(col.get("max", 0) or 0)}
        for key in ("width", "hidden", "bestFit", "customWidth"):
            if col.get(key) is not None:
                item[key] = float(col.get(key)) if key == "width" else col.get(key) in {"1", "true"}
        columns.append(item)
    return {
        "format": "xlsx-ooxml-presentation-v1",
        "coordinate_system": "zero-based cells; Excel coordinates retained in coordinate",
        "merged_ranges": merges,
        "row_heights": row_heights,
        "hidden_rows": hidden_rows,
        "columns": columns,
        "cells": cells[:100_000],
    }
