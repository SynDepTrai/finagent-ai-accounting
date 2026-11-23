from __future__ import annotations

import io
import json
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

MISA_HEADERS = [
    "Ngày hạch toán",
    "Ngày chứng từ",
    "Số chứng từ",
    "Ký hiệu HĐ",
    "Số hoá đơn",
    "Ngày hoá đơn",
    "MST Nhà cung cấp",
    "Tên Nhà cung cấp",
    "Diễn giải",
    "Mã hàng",
    "Tên hàng hoá / Dịch vụ",
    "ĐVT",
    "Số lượng",
    "Đơn giá",
    "Thành tiền chưa thuế",
    "TK Nợ",
    "TK Có",
    "% Thuế GTGT",
    "Tiền thuế GTGT",
    "TK Thuế GTGT",
    "Tổng thanh toán dòng",
]


def build_misa_excel(invoice_rows: list[dict[str, Any]]) -> bytes:
    """Export invoices into a standard MISA AMIS / MISA SME purchase voucher import workbook (.xlsx)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "MISA_ChungTuMuaHang"

    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    ws.append(MISA_HEADERS)
    for col_idx in range(1, len(MISA_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for idx, inv in enumerate(invoice_rows, start=1):
        payload = json.loads(inv["extracted_payload"]) if isinstance(inv["extracted_payload"], str) else inv["extracted_payload"]
        meta = payload.get("invoice_meta", {})
        seller = payload.get("seller", {})
        voucher_no = f"MH{idx:05d}"
        issue_date = meta.get("issue_date", "")

        for item in payload.get("line_items", []):
            if item.get("nature") == "NOTE":
                continue
            ws.append(
                [
                    issue_date,
                    issue_date,
                    voucher_no,
                    meta.get("series", ""),
                    meta.get("invoice_number", ""),
                    issue_date,
                    seller.get("tax_code", ""),
                    seller.get("name", ""),
                    f"Mua hàng theo HĐ {meta.get('invoice_number', '')} - {seller.get('name', '')}",
                    item.get("mapped_item_code") or item.get("item_code") or "",
                    item.get("description", ""),
                    item.get("unit") or "",
                    item.get("quantity") if item.get("quantity") is not None else 1,
                    item.get("unit_price") if item.get("unit_price") is not None else item.get("amount_before_tax", 0),
                    item.get("amount_before_tax", 0),
                    item.get("debit_account") or "6427",
                    item.get("credit_account") or "331",
                    item.get("vat_rate", "10%"),
                    item.get("vat_amount", 0),
                    "1331" if (item.get("vat_amount") or 0) > 0 else "",
                    item.get("total_amount", 0),
                ]
            )

    # Auto-fit column widths reasonably
    for col in ws.columns:
        max_len = max(len(str(c.value or "")) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 3, 12), 42)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


FAST_HEADERS = [
    "Mã NCC",
    "Tên NCC",
    "Ngày CT",
    "Số CT",
    "Ký hiệu HĐ",
    "Số HĐ",
    "Ngày HĐ",
    "Diễn giải",
    "TK Có",
    "Mã vật tư",
    "Tên vật tư",
    "ĐVT",
    "Số lượng",
    "Đơn giá",
    "Tiền hàng",
    "TK Nợ",
    "Mã thuế",
    "Tiền thuế",
    "TK Thuế",
]


def build_fast_excel(invoice_rows: list[dict[str, Any]]) -> bytes:
    """Export invoices into Fast Accounting purchase voucher import workbook (.xlsx)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "FAST_PN_MuaHang"
    header_fill = PatternFill(start_color="0F766E", end_color="0F766E", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    ws.append(FAST_HEADERS)
    for col_idx in range(1, len(FAST_HEADERS) + 1):
        c = ws.cell(row=1, column=col_idx)
        c.fill = header_fill
        c.font = header_font

    for idx, inv in enumerate(invoice_rows, start=1):
        payload = json.loads(inv["extracted_payload"]) if isinstance(inv["extracted_payload"], str) else inv["extracted_payload"]
        meta = payload.get("invoice_meta", {})
        seller = payload.get("seller", {})
        for item in payload.get("line_items", []):
            if item.get("nature") == "NOTE":
                continue
            ws.append(
                [
                    seller.get("tax_code", ""),
                    seller.get("name", ""),
                    meta.get("issue_date", ""),
                    f"PN{idx:05d}",
                    meta.get("series", ""),
                    meta.get("invoice_number", ""),
                    meta.get("issue_date", ""),
                    f"HĐ {meta.get('invoice_number', '')} - {seller.get('name', '')}",
                    item.get("credit_account") or "331",
                    item.get("mapped_item_code") or item.get("item_code") or "",
                    item.get("description", ""),
                    item.get("unit") or "",
                    item.get("quantity") if item.get("quantity") is not None else 1,
                    item.get("unit_price") if item.get("unit_price") is not None else item.get("amount_before_tax", 0),
                    item.get("amount_before_tax", 0),
                    item.get("debit_account") or "6427",
                    item.get("vat_rate", "10%"),
                    item.get("vat_amount", 0),
                    "1331" if (item.get("vat_amount") or 0) > 0 else "",
                ]
            )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_odoo_csv(invoice_rows: list[dict[str, Any]]) -> bytes:
    """Export invoices into Odoo Vendor Bill import CSV (UTF-8 with BOM for Excel compatibility)."""
    import csv

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "ref",
            "partner_id/vat",
            "partner_id/name",
            "invoice_date",
            "currency_id",
            "invoice_line_ids/name",
            "invoice_line_ids/quantity",
            "invoice_line_ids/price_unit",
            "invoice_line_ids/account_id/code",
            "invoice_line_ids/tax_ids",
        ]
    )
    for inv in invoice_rows:
        payload = json.loads(inv["extracted_payload"]) if isinstance(inv["extracted_payload"], str) else inv["extracted_payload"]
        meta = payload.get("invoice_meta", {})
        seller = payload.get("seller", {})
        ref = f"{meta.get('series', '')}-{meta.get('invoice_number', '')}".strip("-")
        for item in payload.get("line_items", []):
            if item.get("nature") == "NOTE":
                continue
            writer.writerow(
                [
                    ref,
                    seller.get("tax_code", ""),
                    seller.get("name", ""),
                    meta.get("issue_date", ""),
                    meta.get("currency", "VND"),
                    item.get("description", ""),
                    item.get("quantity") if item.get("quantity") is not None else 1,
                    item.get("unit_price") if item.get("unit_price") is not None else item.get("amount_before_tax", 0),
                    item.get("debit_account") or "6427",
                    f"VAT {item.get('vat_rate', '10%')}",
                ]
            )
    return ("\ufeff" + buf.getvalue()).encode("utf-8")
