"""Tiny dependency-free XLSX fixture writer used only by tests."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr
import zipfile


def _column_name(index: int) -> str:
    value = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _cell(row_index: int, column_index: int, value: object) -> str:
    reference = f"{_column_name(column_index)}{row_index + 1}"
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, datetime):
        serial = (value - datetime(1899, 12, 30)).total_seconds() / 86400
        return f'<c r="{reference}" s="1"><v>{serial}</v></c>'
    if isinstance(value, date):
        serial = (value - date(1899, 12, 30)).days
        return f'<c r="{reference}" s="1"><v>{serial}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def write_xlsx(
    path: Path,
    sheets: list[tuple[str, list[list[object | None]], str | None]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_entries: list[str] = []
    relationships: list[str] = []
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for sheet_index, (name, rows, visibility) in enumerate(sheets, start=1):
            state = f" state={quoteattr(visibility)}" if visibility else ""
            sheet_entries.append(
                f'<sheet name={quoteattr(name)} sheetId="{sheet_index}"{state} r:id="rId{sheet_index}"/>'
            )
            relationships.append(
                f'<Relationship Id="rId{sheet_index}" '
                f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{sheet_index}.xml"/>'
            )
            row_xml: list[str] = []
            for row_index, row in enumerate(rows):
                cells = "".join(
                    _cell(row_index, column_index, value)
                    for column_index, value in enumerate(row)
                    if value is not None
                )
                row_xml.append(f'<row r="{row_index + 1}">{cells}</row>')
            archive.writestr(
                f"xl/worksheets/sheet{sheet_index}.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetData>{"".join(row_xml)}</sheetData></worksheet>',
            )
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for index in range(1, len(sheets) + 1)
            )
            + "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(sheet_entries)}</sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(relationships)
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            + "</Relationships>",
        )
        archive.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<numFmts count="0"/><fonts count="1"><font/></fonts><fills count="1"><fill/></fills>'
            '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs>'
            "</styleSheet>",
        )
