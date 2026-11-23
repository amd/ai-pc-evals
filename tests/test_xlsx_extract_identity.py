# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""The judge's view of a workbook is the same text since xlsx_text streams it (issue #150).

``full_load_xlsx_text`` is xlsx_text as it was before: both copies of the workbook loaded in full with openpyxl's
normal mode. Every workbook here must extract to the same bytes with it and with the streaming xlsx_text."""

import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, List

import pytest

from kwengine import extract

openpyxl = pytest.importorskip("openpyxl")


def full_load_xlsx_text(path: Path) -> str:
    from openpyxl.utils import column_index_from_string, get_column_letter

    formulas_wb = openpyxl.load_workbook(str(path), data_only=False)
    values_wb = openpyxl.load_workbook(str(path), data_only=True)
    charts = extract._zip_count(path, "xl/charts/chart")
    pivots = extract._zip_count(path, "xl/pivotTables/")
    sheets, total_formulas = [], 0
    evaluator: List[Any] = []
    try:
        recalc = bool(extract._xlsx_eval().full_calc_on_load(str(path)))
    except Exception:  # noqa: BLE001
        recalc = False

    def computed(sheet: str, r: int, c: int) -> str:
        if not evaluator:
            try:
                evaluator.append(extract._xlsx_eval().Workbook(str(path)))
            except Exception as exc:  # noqa: BLE001
                evaluator.append(f"{type(exc).__name__}: {exc}"[:200])
        if isinstance(evaluator[0], str):
            return f"[not computed: {evaluator[0]}]"
        try:
            value = evaluator[0].value(sheet, r, c)
        except Exception as exc:  # noqa: BLE001
            return f"[not computed: {str(exc)[:200]}]"
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return "" if value is None else str(value)

    def hidden_columns(ws) -> set:
        out = set()
        for key, dim in ws.column_dimensions.items():
            if dim.hidden:
                lo = dim.min or column_index_from_string(key)
                hi = dim.max or lo
                out.update(range(lo, hi + 1))
        return out

    for ws in formulas_wb.worksheets:
        values = values_wb[ws.title]
        formulas = sum(1 for row in ws.iter_rows() for c in row if c.data_type == "f")
        total_formulas += formulas
        hidden_cols = hidden_columns(ws)
        state = "" if ws.sheet_state == "visible" else f" [hidden sheet: {ws.sheet_state}]"
        lines = [
            f"## Sheet {ws.title!r}{state}: {ws.dimensions}, {ws.max_row} rows x "
            f"{ws.max_column} cols, {formulas} formulas"
        ]
        hidden_rows = sorted(r for r, d in ws.row_dimensions.items() if d.hidden)
        if hidden_cols or hidden_rows:
            lines.append(
                "[hidden: "
                + "; ".join(
                    part
                    for part in (
                        f"columns {', '.join(get_column_letter(c) for c in sorted(hidden_cols))}" if hidden_cols else "",
                        f"rows {', '.join(map(str, hidden_rows[:50]))}" if hidden_rows else "",
                    )
                    if part
                )
                + "; a reader of the sheet does not see them]"
            )
        for r, row in enumerate(ws.iter_rows(max_row=extract.XLSX_ROWS, max_col=extract.XLSX_COLS), start=1):
            cells = []
            for c, cell in enumerate(row, start=1):
                value = cell.value
                if cell.data_type == "f":
                    cached = values.cell(row=r, column=c).value
                    shown = computed(ws.title, r, c) if cached is None or recalc else cached
                    value = f"{getattr(value, 'text', value)} -> {shown}"
                text = "" if value is None else str(value)
                if text and c in hidden_cols:
                    text = f"[hidden] {text}"
                cells.append(text)
            if any(cells):
                prefix = "[hidden row] " if ws.row_dimensions[r].hidden else ""
                lines.append(prefix + " | ".join(cells).rstrip(" |"))
        if ws.max_row > extract.XLSX_ROWS:
            lines.append(f"...[{ws.max_row - extract.XLSX_ROWS} more rows]")
        sheets.append("\n".join(lines))
    head = (
        f"[{len(formulas_wb.worksheets)} sheets, {total_formulas} formulas, "
        f"{charts} charts, {pivots} pivot tables]"
    )
    return "\n\n".join([head, *sheets])


def _rewrite(path: Path, edits) -> None:
    """Rewrite parts of the zip at *path*: ``edits`` maps a part name to a function of its text."""
    with zipfile.ZipFile(path) as z:
        parts = [(info, z.read(info.filename)) for info in z.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in parts:
            if info.filename in edits:
                data = edits[info.filename](data.decode("utf-8")).encode("utf-8")
            z.writestr(info.filename, data)


def _no_recalc(xml: str) -> str:
    return re.sub(r'\s*fullCalcOnLoad="1"', "", xml)


def _spend(tmp_path: Path) -> Path:
    """test_kwengine's workbooks: a formula, a chart, no cached values (openpyxl saves none)."""
    from openpyxl.chart import BarChart, Reference

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Spend"
    ws.append(["vendor", "amount"])
    ws.append(["Acme", 120])
    ws.append(["Northwind", 845.5])
    ws["B4"] = "=SUM(B2:B3)"
    ws["B5"] = "=ROUND(B4/3, 2)"
    ws["B6"] = "=OFFSET(A1, 1, 1)"
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    ws.add_chart(chart, "D2")
    path = tmp_path / "spend.xlsx"
    wb.save(path)
    return path


def _hidden(tmp_path: Path) -> Path:
    """Hidden and very hidden sheets, hidden columns (one and a span), hidden rows in view, past row 25 and more
    than 50 of them, an empty sheet and a sparse one."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    for r in range(1, 120):
        ws.append([f"item {r}", r * 10, f"x{r}", r % 7, "=B{0}*2".format(r)])
    ws.row_dimensions[3].hidden = True
    for r in range(30, 100):
        ws.row_dimensions[r].hidden = True
    ws.row_dimensions[5].height = 30  # a row dimension that is not hidden
    ws.column_dimensions["C"].hidden = True
    ws.column_dimensions.group("D", "E", hidden=True)
    scratch = wb.create_sheet("Scratch")
    scratch["A1"] = "old numbers"
    scratch.sheet_state = "hidden"
    deep = wb.create_sheet("Deep")
    deep["B2"] = "very hidden"
    deep.sheet_state = "veryHidden"
    wb.create_sheet("Empty")
    sparse = wb.create_sheet("Sparse")
    sparse["C5"] = "starts at C5"
    sparse["H40"] = 3.5
    path = tmp_path / "hidden.xlsx"
    wb.save(path)
    return path


def _wide_and_long(tmp_path: Path) -> Path:
    """More than XLSX_ROWS rows and XLSX_COLS columns, formulas in and out of view, dates, booleans, errors."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Flights"
    ws.append([f"col{c}" for c in range(1, 31)])
    for r in range(2, 400):
        ws.append([r * c for c in range(1, 26)] + [datetime(2026, 1, 1 + r % 28), r % 2 == 0, "#N/A", f"=A{r}+B{r}",
                                                    f"=SUM(A{r}:Y{r})"])
        ws.cell(row=r, column=5, value=f"=A{r}*3")
    ws["T3"] = "=AVERAGE(A2:A399)"
    path = tmp_path / "wide.xlsx"
    wb.save(path)
    return path


def _arrays_and_merges(tmp_path: Path) -> Path:
    """An array formula, a merged range over data (a formula under it is dropped by a full load), merged ranges,
    hyperlinks and comments past the data (they grow the sheet), and a merge starting at A1."""
    from openpyxl.comments import Comment
    from openpyxl.worksheet.formula import ArrayFormula

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Arrays"
    for r in range(1, 6):
        ws.append([r, r * 2, f"=A{r}+B{r}"])
    ws["E1"] = ArrayFormula("E1:E3", "=A1:A3*10")
    ws["G2"] = "=C2*2"
    ws.merged_cells.add("C3:D4")  # registered without clearing C4's formula: it is written, a full load drops it
    ws.merge_cells("A30:C32")
    ws["J1"] = "link"
    ws["J1"].hyperlink = "https://example.com/"
    ws["AA70"].comment = Comment("a note past the data", "reviewer")
    other = wb.create_sheet("Merged")
    other["A1"] = "title"
    other.merge_cells("A1:F2")
    other["A3"] = "=1+1"
    path = tmp_path / "arrays.xlsx"
    wb.save(path)
    _rewrite(path, {"xl/worksheets/sheet1.xml": lambda x: x.replace(
        "</hyperlinks>", '<hyperlink ref="Q90:R91" display="past the data"/></hyperlinks>')})
    return path


def _xlsxwriter_recalc(tmp_path: Path) -> Path:
    """xlsxwriter (pandas' engine): placeholder 0 results and fullCalcOnLoad, an XLOOKUP, a dynamic array."""
    xlsxwriter = pytest.importorskip("xlsxwriter")
    path = tmp_path / "written.xlsx"
    wb = xlsxwriter.Workbook(str(path), {"use_future_functions": True})
    ws = wb.add_worksheet("Spend")
    ws.write_row(0, 0, ["vendor", "amount"])
    ws.write_row(1, 0, ["Acme", 120])
    ws.write_row(2, 0, ["Northwind", 845.5])
    ws.write_formula("B4", "=SUM(B2:B3)")
    ws.write_formula("B5", '=XLOOKUP("Acme", A2:A3, B2:B3)')
    ws.write_formula("B6", "=OFFSET(A1, 1, 1)")
    ws.write_array_formula("D2:D3", "{=B2:B3*2}")
    ws.write_dynamic_array_formula("E2", "=SORT(B2:B3)")
    ws.set_row(40, None, None, {"hidden": True})
    wb.close()
    return path


def _cached_values(tmp_path: Path) -> Path:
    """Formulas saved with Excel's results and no recalculation flag (as Excel or LibreOffice save them), one
    saved without a result, and a cached date."""
    xlsxwriter = pytest.importorskip("xlsxwriter")
    path = tmp_path / "cached.xlsx"
    wb = xlsxwriter.Workbook(str(path))
    ws = wb.add_worksheet("Totals")
    date = wb.add_format({"num_format": "yyyy-mm-dd"})
    ws.write_row(0, 0, ["a", "b", "sum"])
    for r in range(1, 40):
        ws.write_row(r, 0, [r, r * 2])
        ws.write_formula(r, 2, f"=A{r + 1}+B{r + 1}", None, 3 * r)
    ws.write_formula("E1", '=IF(A2>0,"yes","no")', None, "yes")
    ws.write_formula("E2", "=1/0", None, "#DIV/0!")
    ws.write_formula("E3", "=DATE(2026,1,2)", date, 46024)
    ws.write_formula("E4", "=TRUE()", None, True)
    ws.write_formula("E5", "=A2*100")  # no result saved (xlsxwriter writes 0): kept as cached
    wb.close()
    _rewrite(path, {"xl/workbook.xml": _no_recalc})
    return path


def _no_row_refs(tmp_path: Path) -> Path:
    """Rows and cells without r= attributes (some writers omit them) and no <dimension> tag."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bare"
    for r in range(1, 40):
        ws.append([r, f"row {r}", "=A1*2" if r == 2 else None])
    path = tmp_path / "bare.xlsx"
    wb.save(path)
    strip = (lambda x: re.sub(r'<dimension ref="[^"]*"\s*/>', "",
                              re.sub(r'(<(?:row|c)\b[^>]*?)\s+r="[^"]*"', r"\1", x)))
    _rewrite(path, {"xl/worksheets/sheet1.xml": strip})
    return path


WORKBOOKS = [_spend, _hidden, _wide_and_long, _arrays_and_merges, _xlsxwriter_recalc, _cached_values, _no_row_refs]


@pytest.mark.parametrize("build", WORKBOOKS, ids=[b.__name__.lstrip("_") for b in WORKBOOKS])
def test_streamed_xlsx_text_is_the_full_loads_byte_for_byte(tmp_path, build):
    path = build(tmp_path)
    expected = full_load_xlsx_text(path)
    assert extract.xlsx_text(path) == expected


def test_the_identity_workbooks_cover_what_they_say(tmp_path):
    text = extract.xlsx_text(_hidden(tmp_path))
    assert "[hidden sheet: veryHidden]" in text and "## Sheet 'Empty': A1:A1, 1 rows x 1 cols, 0 formulas" in text
    assert "## Sheet 'Sparse': A1:H40, 40 rows x 8 cols" in text and "...[94 more rows]" in text
    assert "[hidden: columns C, D, E; rows 3, 30, 31," in text and "[hidden row] item 3" in text
    text = extract.xlsx_text(_arrays_and_merges(tmp_path))
    assert "## Sheet 'Arrays': A1:AA91, 91 rows x 27 cols, 6 formulas" in text  # C4's formula is merged away
    assert "=A1:A3*10 ->" in text
    text = extract.xlsx_text(_cached_values(tmp_path))
    assert "=A2+B2 -> 3" in text and "=1/0 -> #DIV/0!" in text and "2026-01-02" in text
    text = extract.xlsx_text(_no_row_refs(tmp_path))
    assert "39 rows x 3 cols, 1 formulas" in text and "...[14 more rows]" in text


def test_a_workbook_with_broken_sheet_xml_reports_it_as_a_full_load_did(tmp_path):
    path = _spend(tmp_path)
    _rewrite(path, {"xl/worksheets/sheet1.xml": lambda x: x.replace("</sheetData>", "<row r=\"x\"/></sheetData>")})
    with pytest.raises(ValueError) as full:
        full_load_xlsx_text(path)
    assert str(full.value).startswith("Unable to read workbook: could not read worksheets from")
    assert extract.extract(path) == "[could not be opened as .xlsx: " + f"ValueError: {full.value}"[:400] + "]"


def test_a_sheet_past_excels_last_row_fails_as_a_full_load_did(tmp_path):
    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["A2"] = "first", "past the last row"
    path = tmp_path / "long.xlsx"
    wb.save(path)
    _rewrite(path, {"xl/worksheets/sheet1.xml": lambda x: x.replace('<row r="2"', '<row r="1048580"').replace(
        'r="A2"', 'r="A1048580"')})
    with pytest.raises(ValueError) as full:
        full_load_xlsx_text(path)
    with pytest.raises(ValueError) as streamed:
        extract.xlsx_text(path)
    assert str(streamed.value) == str(full.value) and "1048576" in str(full.value)
