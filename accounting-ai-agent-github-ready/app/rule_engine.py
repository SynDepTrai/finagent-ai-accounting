from __future__ import annotations

import re
from typing import Optional

from app.db import Database
from app.schemas import ValidationIssue, VietnamVATInvoiceExtraction

MST_PATTERN = re.compile(r"^\d{10}(-\d{3})?$")
MST_WEIGHTS = [31, 29, 23, 19, 17, 13, 7, 5, 3]
MATH_TOLERANCE = 10.0  # Allow <= 10 VND rounding diff per line/total
VAT_RATES = {"0%": 0.0, "5%": 0.05, "8%": 0.08, "10%": 0.10}


def validate_vn_tax_code(tax_code: Optional[str]) -> tuple[bool, str]:
    """Validate Vietnam MST format (10 or 10-3 digits) and Mod-11 check digit."""
    if not tax_code:
        return False, "Thiếu mã số thuế."
    code = tax_code.strip()
    if not MST_PATTERN.match(code):
        return False, f"Mã số thuế '{code}' không đúng định dạng 10 số hoặc 10-3 số."
    digits = [int(ch) for ch in code[:10]]
    checksum = sum(d * w for d, w in zip(digits[:9], MST_WEIGHTS))
    expected_digit = 10 - (checksum % 11)
    if expected_digit != digits[9]:
        return False, f"Mã số thuế '{code}' sai chữ số kiểm tra (checksum không hợp lệ)."
    return True, ""


def validate_invoice(
    invoice: VietnamVATInvoiceExtraction,
    db: Optional[Database] = None,
    tenant_id: str = "default-sme",
    exclude_invoice_id: Optional[str] = None,
) -> list[ValidationIssue]:
    """Deterministic Rule Engine: zero AI, 100% exact math, MST, signature, and duplicate checks."""
    issues: list[ValidationIssue] = []

    # 1. Required metadata
    if not invoice.invoice_meta.invoice_number:
        issues.append(
            ValidationIssue(
                code="MISSING_INVOICE_NUMBER",
                severity="ERROR",
                field="invoice_meta.invoice_number",
                message="Thiếu số hoá đơn.",
            )
        )
    if not invoice.invoice_meta.issue_date:
        issues.append(
            ValidationIssue(
                code="MISSING_ISSUE_DATE",
                severity="ERROR",
                field="invoice_meta.issue_date",
                message="Thiếu ngày lập hoá đơn.",
            )
        )

    # 2. Seller MST format + checksum
    ok_seller, msg_seller = validate_vn_tax_code(invoice.seller.tax_code)
    if not ok_seller:
        issues.append(
            ValidationIssue(
                code="INVALID_SELLER_MST",
                severity="ERROR",
                field="seller.tax_code",
                message=f"MST người bán không hợp lệ: {msg_seller}",
            )
        )

    # Buyer MST (optional for retail/individual, but validate if present)
    if invoice.buyer.tax_code:
        ok_buyer, msg_buyer = validate_vn_tax_code(invoice.buyer.tax_code)
        if not ok_buyer:
            issues.append(
                ValidationIssue(
                    code="INVALID_BUYER_MST",
                    severity="WARNING",
                    field="buyer.tax_code",
                    message=f"MST người mua không hợp lệ: {msg_buyer}",
                )
            )

    # 3. Digital signature check for XML
    if invoice.extraction_source == "xml_tt78" and invoice.invoice_meta.has_valid_signature is False:
        issues.append(
            ValidationIssue(
                code="MISSING_DIGITAL_SIGNATURE",
                severity="WARNING",
                field="invoice_meta.has_valid_signature",
                message="File XML chưa có chữ ký số hợp lệ của người bán (XMLDSig).",
            )
        )

    # 4. Adjustment / Replacement alert
    if invoice.invoice_meta.invoice_type in {"ADJUSTMENT", "REPLACEMENT", "CANCELLED"}:
        issues.append(
            ValidationIssue(
                code="RELATED_INVOICE_ALERT",
                severity="WARNING",
                field="invoice_meta.invoice_type",
                message=(
                    f"Đây là hoá đơn loại {invoice.invoice_meta.invoice_type} "
                    f"(liên quan hoá đơn gốc: {invoice.invoice_meta.related_invoice_ref or 'chưa rõ'}). "
                    "Cần kiểm tra tránh hạch toán trùng."
                ),
            )
        )

    # 4b. Foreign currency exchange rate check (Circular 200)
    if invoice.invoice_meta.currency.upper() != "VND" and invoice.invoice_meta.exchange_rate <= 1.0:
        issues.append(
            ValidationIssue(
                code="MISSING_EXCHANGE_RATE",
                severity="ERROR",
                field="invoice_meta.exchange_rate",
                message=(
                    f"Hoá đơn ngoại tệ ({invoice.invoice_meta.currency}) chưa có tỷ giá quy đổi VNĐ hợp lệ "
                    f"(hiện tại: {invoice.invoice_meta.exchange_rate})."
                ),
            )
        )

    # 5. Line-item arithmetic checks
    calc_subtotal = 0.0
    calc_vat = 0.0
    for item in invoice.line_items:
        if item.nature == "NOTE":
            continue
        if item.nature == "DISCOUNT":
            # Discount lines reduce subtotal when positive or negative
            signed_amt = -abs(item.amount_before_tax) if item.amount_before_tax > 0 else item.amount_before_tax
            calc_subtotal += signed_amt
            calc_vat += -abs(item.vat_amount) if item.vat_amount > 0 else item.vat_amount
            continue

        if item.quantity is not None and item.unit_price is not None and item.nature == "GOODS":
            expected_amt = round(item.quantity * item.unit_price - item.discount_amount, 2)
            if abs(expected_amt - item.amount_before_tax) > MATH_TOLERANCE:
                issues.append(
                    ValidationIssue(
                        code="LINE_MATH_MISMATCH",
                        severity="ERROR",
                        field=f"line_items[{item.line_no}].amount_before_tax",
                        message=(
                            f"Dòng {item.line_no} ({item.description}): SL ({item.quantity}) x Đơn giá "
                            f"({item.unit_price:,.0f}) - CK ({item.discount_amount:,.0f}) = {expected_amt:,.0f}, "
                            f"nhưng thành tiền ghi {item.amount_before_tax:,.0f}."
                        ),
                    )
                )

        mult = VAT_RATES.get(item.vat_rate)
        if mult is not None:
            expected_vat = round(item.amount_before_tax * mult, 2)
            if abs(expected_vat - item.vat_amount) > MATH_TOLERANCE:
                issues.append(
                    ValidationIssue(
                        code="LINE_VAT_MISMATCH",
                        severity="ERROR",
                        field=f"line_items[{item.line_no}].vat_amount",
                        message=(
                            f"Dòng {item.line_no}: Tiền thuế GTGT ({item.vat_rate}) tính ra {expected_vat:,.0f}, "
                            f"nhưng chứng từ ghi {item.vat_amount:,.0f}."
                        ),
                    )
                )

        calc_subtotal += item.amount_before_tax
        calc_vat += item.vat_amount

    # 6. Totals cross-check
    if invoice.line_items:
        # Subtotal may already include or exclude total_discount depending on vendor ERP
        sub_diff_direct = abs(calc_subtotal - invoice.totals.subtotal)
        sub_diff_after_disc = abs((calc_subtotal - invoice.totals.total_discount) - invoice.totals.subtotal)
        if min(sub_diff_direct, sub_diff_after_disc) > MATH_TOLERANCE:
            issues.append(
                ValidationIssue(
                    code="SUBTOTAL_MISMATCH",
                    severity="ERROR",
                    field="totals.subtotal",
                    message=(
                        f"Tổng tiền hàng các dòng = {calc_subtotal:,.0f}, lệch so với "
                        f"tổng cộng tiền hàng trên hoá đơn ({invoice.totals.subtotal:,.0f})."
                    ),
                )
            )

        if abs(calc_vat - invoice.totals.total_vat) > MATH_TOLERANCE:
            issues.append(
                ValidationIssue(
                    code="TOTAL_VAT_MISMATCH",
                    severity="ERROR",
                    field="totals.total_vat",
                    message=(
                        f"Tổng tiền thuế các dòng = {calc_vat:,.0f}, lệch so với "
                        f"tổng tiền thuế trên hoá đơn ({invoice.totals.total_vat:,.0f})."
                    ),
                )
            )

    expected_grand_a = invoice.totals.subtotal + invoice.totals.total_vat
    expected_grand_b = invoice.totals.subtotal - invoice.totals.total_discount + invoice.totals.total_vat
    if min(abs(expected_grand_a - invoice.totals.grand_total), abs(expected_grand_b - invoice.totals.grand_total)) > MATH_TOLERANCE:
        issues.append(
            ValidationIssue(
                code="GRAND_TOTAL_MISMATCH",
                severity="ERROR",
                field="totals.grand_total",
                message=(
                    f"Tổng thanh toán ({invoice.totals.grand_total:,.0f}) không khớp "
                    f"Tiền hàng ({invoice.totals.subtotal:,.0f}) + Thuế ({invoice.totals.total_vat:,.0f})."
                ),
            )
        )

    # 7. Duplicate invoice detection
    if db is not None and invoice.seller.tax_code and invoice.invoice_meta.invoice_number:
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM invoices
                WHERE tenant_id = ?
                  AND seller_tax_code = ?
                  AND series = ?
                  AND invoice_number = ?
                  AND status != 'REJECTED'
                  AND (? IS NULL OR id != ?)
                LIMIT 1
                """,
                (
                    tenant_id,
                    invoice.seller.tax_code,
                    invoice.invoice_meta.series,
                    invoice.invoice_meta.invoice_number,
                    exclude_invoice_id,
                    exclude_invoice_id,
                ),
            ).fetchone()
            if row is not None:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_INVOICE",
                        severity="ERROR",
                        field="invoice_meta.invoice_number",
                        message=(
                            f"Phát hiện hoá đơn trùng lặp: MST {invoice.seller.tax_code}, "
                            f"Ký hiệu {invoice.invoice_meta.series}, Số {invoice.invoice_meta.invoice_number} "
                            f"(đã tồn tại ID: {row['id'][:8]})."
                        ),
                    )
                )

    return issues
