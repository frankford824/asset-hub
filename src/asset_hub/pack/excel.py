from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import openpyxl
import xlrd

from asset_hub.catalog.db import Catalog


@dataclass
class ExcelRow:
    row_index: int
    sku_code: str = ""
    sku_name: str = ""


def _cell_str(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def read_excel_rows(path: Path) -> list[ExcelRow]:
    suffix = path.suffix.lower()
    rows: list[ExcelRow] = []
    if suffix == ".xlsx":
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        headers = None
        for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            values = [_cell_str(c) for c in row]
            if i == 1:
                headers = [v.lower() for v in values]
                continue
            code = ""
            name = ""
            if headers:
                for idx, h in enumerate(headers):
                    if idx >= len(values):
                        break
                    if any(k in h for k in ("sku", "编码", "货号", "料号", "code")):
                        code = values[idx] or code
                    if any(k in h for k in ("名称", "品名", "name", "标题")):
                        name = values[idx] or name
            if not code and values:
                code = values[0]
            if not name and len(values) > 1:
                name = values[1]
            if code or name:
                rows.append(ExcelRow(row_index=i, sku_code=code.upper(), sku_name=name))
        wb.close()
        return rows

    if suffix == ".xls":
        book = xlrd.open_workbook(str(path))
        sheet = book.sheet_by_index(0)
        headers = [str(sheet.cell_value(0, c)).strip().lower() for c in range(sheet.ncols)]
        for r in range(1, sheet.nrows):
            values = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
            code = ""
            name = ""
            for idx, h in enumerate(headers):
                if idx >= len(values):
                    break
                if any(k in h for k in ("sku", "编码", "货号", "料号", "code")):
                    code = values[idx] or code
                if any(k in h for k in ("名称", "品名", "name", "标题")):
                    name = values[idx] or name
            if not code and values:
                code = values[0]
            if not name and len(values) > 1:
                name = values[1]
            if code or name:
                rows.append(ExcelRow(row_index=r + 1, sku_code=code.upper(), sku_name=name))
        return rows

    raise ValueError("仅支持 .xlsx / .xls")


def match_assets_for_rows(catalog: Catalog, rows: list[ExcelRow], limit_per_row: int = 20):
    """Align to production idea: prefer SKU token exact hits in finalized catalog."""
    matched = []
    missing = []
    for row in rows:
        q = row.sku_code or row.sku_name
        hits, _total = catalog.search(q, kind="finalized", limit=limit_per_row) if q else ([], 0)
        # filter ready with local file
        ready = [h for h in hits if h.status == "ready" and h.local_path]
        if ready:
            matched.append({"row": row, "assets": ready})
        else:
            missing.append({"row": row, "query": q})
    return matched, missing
