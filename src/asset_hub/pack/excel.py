from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import openpyxl
import xlrd

from asset_hub.catalog.db import Catalog


SKU_REGEX = re.compile(r"^[A-Za-z]+[A-Za-z0-9._-]*\d+$")
EMBEDDED_SKU_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*\d+")
SOURCE_FILE_EXTS = {".psd", ".psb", ".ai", ".cdr", ".eps"}


@dataclass
class ExcelRow:
    row_index: int
    sku_code: str = ""
    sku_name: str = ""
    order_id: str = ""
    quantity: int = 1
    address: str = ""
    keyword: str = ""


def ensure_sku_in_filename(filename: str, sku_code: str) -> str:
    """Prefix an unambiguous SKU without duplicating an existing code."""
    name = Path(filename or "").name
    sku = str(sku_code or "").strip().upper()
    if not name or not SKU_REGEX.fullmatch(sku):
        return name
    tokens = {match.group(0).upper().rstrip("-_.") for match in EMBEDDED_SKU_RE.finditer(name)}
    return name if sku in tokens else f"{sku}——{name}"


def _cell_str(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        try:
            return str(int(float(text)))
        except ValueError:
            pass
    return text


def _quantity(value: str) -> int:
    try:
        return max(1, int(float(value or "1")))
    except (TypeError, ValueError):
        return 1


def _header_index(headers: list[str], needles: tuple[str, ...]) -> int | None:
    for idx, header in enumerate(headers):
        if any(needle in header for needle in needles):
            return idx
    return None


def _parse_values(row_index: int, headers: list[str], values: list[str]) -> ExcelRow | None:
    indexes = {
        "order": _header_index(headers, ("订单", "单号", "order")),
        "sku": _header_index(headers, ("sku", "编码", "货号", "料号", "code")),
        "name": _header_index(headers, ("名称", "品名", "name", "标题")),
        "quantity": _header_index(headers, ("数量", "件数", "qty", "quantity")),
        "address": _header_index(headers, ("地址", "收件", "address")),
        "keyword": _header_index(headers, ("关键词", "关键字", "keyword", "款式")),
    }
    recognized = any(value is not None for value in indexes.values())

    def at(index: int | None, fallback: int | None = None) -> str:
        target = index if index is not None else fallback
        return values[target] if target is not None and target < len(values) else ""

    if recognized:
        sku = at(indexes["sku"])
        name = at(indexes["name"])
        order_id = at(indexes["order"])
        quantity = _quantity(at(indexes["quantity"]))
        address = at(indexes["address"])
        keyword = at(indexes["keyword"])
    else:
        # Exact eve35 positional contract: order, SKU, quantity, address, keyword.
        # Two-column sheets remain compatible with the original asset-hub format.
        if len(values) >= 3:
            order_id, sku = at(None, 0), at(None, 1)
            quantity, address, keyword = _quantity(at(None, 2)), at(None, 3), at(None, 4)
            name = ""
        else:
            order_id, sku, name = "", at(None, 0), at(None, 1)
            quantity, address, keyword = 1, "", ""
    if not any((order_id, sku, name, address, keyword)):
        return None
    return ExcelRow(
        row_index=row_index,
        sku_code=sku.upper(),
        sku_name=name,
        order_id=order_id,
        quantity=quantity,
        address=address,
        keyword=keyword,
    )


def read_excel_rows(path: Path) -> list[ExcelRow]:
    suffix = path.suffix.lower()
    rows: list[ExcelRow] = []
    if suffix == ".xlsx":
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        iterator = ws.iter_rows(values_only=True)
        first = next(iterator, ())
        headers = [_cell_str(value).lower() for value in first]
        for row_index, raw in enumerate(iterator, start=2):
            parsed = _parse_values(row_index, headers, [_cell_str(value) for value in raw])
            if parsed:
                rows.append(parsed)
        wb.close()
        return rows
    if suffix == ".xls":
        book = xlrd.open_workbook(str(path))
        sheet = book.sheet_by_index(0)
        headers = [_cell_str(sheet.cell_value(0, col)).lower() for col in range(sheet.ncols)]
        for index in range(1, sheet.nrows):
            parsed = _parse_values(
                index + 1,
                headers,
                [_cell_str(sheet.cell_value(index, col)) for col in range(sheet.ncols)],
            )
            if parsed:
                rows.append(parsed)
        return rows
    raise ValueError("仅支持 .xlsx / .xls")


def deduplicate_rows(rows: list[ExcelRow]) -> tuple[list[ExcelRow], list[dict]]:
    """Collapse byte-for-byte business duplicates while retaining audit lines."""
    unique: list[ExcelRow] = []
    duplicates: list[dict] = []
    first_by_key: dict[tuple, ExcelRow] = {}
    for row in rows:
        key = (
            row.order_id.casefold(),
            row.sku_code.casefold(),
            row.sku_name.casefold(),
            int(row.quantity),
            row.address.casefold(),
            row.keyword.casefold(),
        )
        first = first_by_key.get(key)
        if first is not None:
            duplicates.append({"row": row, "first_row_index": first.row_index})
            continue
        first_by_key[key] = row
        unique.append(row)
    return unique, duplicates


def match_assets_for_rows(
    catalog: Catalog,
    rows: list[ExcelRow],
    limit_per_row: int = 20,
    rule_handlers: set[str] | None = None,
):
    """Match the unified library while applying the selected rule snapshot."""
    handlers = rule_handlers or {"prefer_current", "library_fallback"}
    matched = []
    missing = []

    def local_deliverables(hits):
        deliverables = []
        source_seen = False
        for hit in hits:
            if not hit.local_path:
                continue
            path = Path(hit.local_path)
            if not path.is_file() or path.stat().st_size != hit.file_size:
                continue
            if path.suffix.lower() in SOURCE_FILE_EXTS:
                source_seen = True
                continue
            deliverables.append(hit)
        return deliverables, source_seen

    for row in rows:
        q = row.sku_code or row.sku_name
        if "validate_sku" in handlers and row.sku_code and not SKU_REGEX.match(row.sku_code):
            missing.append({"row": row, "query": q, "reason": "SKU 格式错误"})
            continue
        hits, _total = catalog.search(q, limit=limit_per_row) if q else ([], 0)
        available, source_seen = local_deliverables(hits)
        if not available and row.sku_name and row.sku_name != q:
            hits, _total = catalog.search(row.sku_name, limit=limit_per_row)
            available, alternate_source_seen = local_deliverables(hits)
            source_seen = source_seen or alternate_source_seen
        unique_available = []
        seen_paths: set[str] = set()
        for hit in available:
            key = str(Path(hit.local_path).resolve())
            if key in seen_paths:
                continue
            seen_paths.add(key)
            unique_available.append(hit)
        available = unique_available
        if "keyword_filter" in handlers and row.keyword:
            keyword = row.keyword.casefold()
            available = [
                hit
                for hit in available
                if keyword
                in " ".join(
                    [hit.file_name, hit.virtual_path, hit.storage_key, hit.sku_name]
                ).casefold()
            ]

        current = [hit for hit in available if hit.kind == "finalized"]
        fallback = [hit for hit in available if hit.kind != "finalized"]
        if current and "prefer_current" in handlers:
            selected = catalog.order_current_assets(current)
            policy, label = "preferred_current", "当前优选"
        elif fallback and "library_fallback" in handlers:
            first = fallback[0]
            first_parent = Path(first.virtual_path).parent if first.virtual_path else Path()
            if q.casefold() in first_parent.name.casefold():
                selected = [
                    asset
                    for asset in fallback
                    if Path(asset.virtual_path).parent == first_parent
                ]
            else:
                selected = [first]
            policy, label = "library_fallback", "库内兜底"
        elif available:
            selected = available
            policy, label = "available", "统一库匹配"
        else:
            reason = "仅存在源文件，未找到成品素材" if source_seen else "库内缺失"
            missing.append({"row": row, "query": q, "reason": reason})
            continue
        matched.append(
            {
                "row": row,
                "assets": selected,
                "selection_policy": policy,
                "selection_label": label,
            }
        )
    return matched, missing
