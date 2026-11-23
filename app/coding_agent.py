from __future__ import annotations

import os
import re
from typing import Optional

from app.db import Database, new_id, utc_now
from app.schemas import JournalEntry, LineItem, VietnamVATInvoiceExtraction

# Built-in Vietnamese Circular 200 / Circular 133 semantic coding rules (most specific first)
TT200_RULES: list[tuple[list[str], str, str]] = [
    (["vận chuyển", "giao hàng", "chuyển phát", "logistics", "quảng cáo", "marketing", "facebook", "google ads"], "6417", "Chi phí bán hàng / vận chuyển"),
    (["điện", "nước", "internet", "viễn thông", "cước viễn thông", "hosting", "tên miền", "thuê văn phòng", "phí dịch vụ"], "6427", "Chi phí dịch vụ mua ngoài"),
    (["văn phòng phẩm", "giấy in", "mực in", "bút", "sổ", "công cụ"], "6423", "Chi phí đồ dùng văn phòng"),
    (["tiếp khách", "ăn uống", "nhà hàng", "khách sạn", "công tác", "vé máy bay"], "6428", "Chi phí bằng tiền khác"),
    (["máy tính", "laptop", "màn hình", "máy in", "bàn ghế", "thiết bị"], "242", "Chi phí trả trước / CCDC chờ phân bổ"),
    (["nguyên vật liệu", "vật tư", "thép", "xi măng", "bao bì", "hóa chất"], "152", "Nguyên liệu, vật liệu"),
    (["hàng hóa", "nhập kho", "thương phẩm", "sản phẩm"], "1561", "Giá mua hàng hóa"),
]

TT133_ACCOUNT_MAP = {
    "6427": "6422",
    "6423": "6422",
    "6428": "6422",
    "6417": "6421",
    "1561": "156",
}


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _token_overlap(a: str, b: str) -> float:
    ta = set(re.findall(r"\w+", _normalize_text(a)))
    tb = set(re.findall(r"\w+", _normalize_text(b)))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _adapt_regime(account: str, regime: str) -> str:
    if regime.upper() == "TT133":
        return TT133_ACCOUNT_MAP.get(account, account)
    return account


def suggest_journal_entries(
    invoice: VietnamVATInvoiceExtraction,
    db: Database,
    tenant_id: str = "default-sme",
    regime: Optional[str] = None,
) -> tuple[list[JournalEntry], int]:
    """
    3-tier Accounting Coding Agent:
      Tier 1: Exact vendor/item rule from vendor_account_mappings
      Tier 2: Token-similarity history match (shortcut: upgrade to pgvector embeddings on Postgres)
      Tier 3: Built-in TT200/TT133 keyword rules
    Returns (journal_entries, min_vendor_approved_count_for_stp).
    """
    active_regime = (regime or os.getenv("ACCOUNTING_REGIME", "TT200")).upper()
    seller_mst = (invoice.seller.tax_code or "").strip()

    with db.connect() as conn:
        vendor_rules = conn.execute(
            """
            SELECT * FROM vendor_account_mappings
            WHERE tenant_id = ? AND seller_tax_code = ?
            ORDER BY is_vendor_default DESC, approved_count DESC, last_used_at DESC
            """,
            (tenant_id, seller_mst),
        ).fetchall()

    entries: list[JournalEntry] = []
    min_approved_count = 999
    has_goods_line = False
    default_credit = "1111" if (invoice.invoice_meta.payment_method or "").upper() == "TM" else "331"

    for item in invoice.line_items:
        if item.nature in {"NOTE", "PROMOTION"} or item.amount_before_tax == 0:
            continue
        has_goods_line = True
        norm_desc = _normalize_text(item.description)

        debit = None
        credit = default_credit
        reason = ""
        confidence = 0.78
        matched_count = 0

        # Tier 1: Exact item pattern or vendor default rule
        for rule in vendor_rules:
            if rule["item_keyword_pattern"] == norm_desc:
                debit = rule["debit_account"]
                credit = rule["credit_account"]
                item.mapped_item_code = rule["mapped_item_code"]
                matched_count = int(rule["approved_count"])
                reason = f"Khớp lịch sử đã duyệt {matched_count} lần của NCC này"
                confidence = 0.98
                break
        if debit is None:
            for rule in vendor_rules:
                if rule["is_vendor_default"]:
                    debit = rule["debit_account"]
                    credit = rule["credit_account"]
                    matched_count = int(rule["approved_count"])
                    reason = f"Áp dụng quy tắc mặc định NCC (đã duyệt {matched_count} lần)"
                    confidence = 0.96
                    break

        # Tier 2: Token-similarity historical match (shortcut: replace with pgvector cosine similarity when migrating to Postgres)
        if debit is None and vendor_rules:
            best_score = 0.0
            best_rule = None
            for rule in vendor_rules:
                score = _token_overlap(norm_desc, rule["item_keyword_pattern"])
                if score > best_score:
                    best_score = score
                    best_rule = rule
            if best_rule is not None and best_score >= 0.45:
                debit = best_rule["debit_account"]
                credit = best_rule["credit_account"]
                item.mapped_item_code = best_rule["mapped_item_code"]
                matched_count = int(best_rule["approved_count"])
                reason = f"Tương đồng {int(best_score * 100)}% với lịch sử NCC này"
                confidence = round(0.85 + 0.1 * best_score, 2)

        # Tier 3: Built-in TT200/TT133 semantic rules
        if debit is None:
            matched_count = 0
            for keywords, acc, label in TT200_RULES:
                if any(kw in norm_desc for kw in keywords):
                    # Fixed asset check >= 30M VND per Circular 45
                    if acc == "242" and item.amount_before_tax >= 30_000_000:
                        debit = "211"
                        reason = f"Gợi ý {active_regime}: Tài sản cố định (>= 30 triệu)"
                    else:
                        debit = _adapt_regime(acc, active_regime)
                        reason = f"Gợi ý {active_regime}: {label}"
                    confidence = 0.84
                    break

        if debit is None:
            debit = _adapt_regime("6427", active_regime)
            reason = f"Mặc định {active_regime} (NCC/mặt hàng mới — cần kế toán kiểm tra)"
            confidence = 0.70

        item.debit_account = debit
        item.credit_account = credit
        min_approved_count = min(min_approved_count, matched_count)

        fx_rate = invoice.invoice_meta.exchange_rate if invoice.invoice_meta.currency.upper() != "VND" else 1.0
        vnd_amount = round(item.amount_before_tax * fx_rate, 2)

        entries.append(
            JournalEntry(
                line_no=item.line_no,
                description=f"Mua {item.description}" + (f" (quy đổi tỷ giá {fx_rate:,.0f})" if fx_rate != 1.0 else ""),
                debit_account=debit,
                credit_account=credit,
                amount=vnd_amount,
                reason=reason,
                confidence=confidence,
            )
        )

    # VAT journal entry (Nợ 1331 / Có 331 or 1111)
    if invoice.totals.total_vat > 0:
        fx_rate = invoice.invoice_meta.exchange_rate if invoice.invoice_meta.currency.upper() != "VND" else 1.0
        entries.append(
            JournalEntry(
                line_no=len(entries) + 1,
                description=f"Thuế GTGT được khấu trừ HĐ {invoice.invoice_meta.invoice_number}",
                debit_account="1331",
                credit_account=default_credit,
                amount=round(invoice.totals.total_vat * fx_rate, 2),
                reason="Thuế GTGT đầu vào theo hoá đơn hợp lệ",
                confidence=0.99,
            )
        )

    if not has_goods_line:
        min_approved_count = 0

    return entries, (min_approved_count if min_approved_count < 999 else 0)


def record_coding_feedback(
    db: Database,
    tenant_id: str,
    seller_tax_code: str,
    line_items: list[LineItem],
    apply_vendor_default: bool = False,
) -> int:
    """Persist accountant-approved account mappings so future invoices from this vendor auto-code accurately."""
    if not seller_tax_code:
        return 0
    saved = 0
    now = utc_now()
    with db.connect() as conn:
        for item in line_items:
            if not item.debit_account or not item.credit_account or item.nature in {"NOTE", "PROMOTION"}:
                continue
            pattern = _normalize_text(item.description)
            existing = conn.execute(
                """
                SELECT id, approved_count FROM vendor_account_mappings
                WHERE tenant_id = ? AND seller_tax_code = ? AND item_keyword_pattern = ?
                LIMIT 1
                """,
                (tenant_id, seller_tax_code, pattern),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE vendor_account_mappings
                    SET debit_account = ?, credit_account = ?, mapped_item_code = ?,
                        is_vendor_default = CASE WHEN ? = 1 THEN 1 ELSE is_vendor_default END,
                        approved_count = approved_count + 1, last_used_at = ?
                    WHERE id = ?
                    """,
                    (
                        item.debit_account,
                        item.credit_account,
                        item.mapped_item_code,
                        1 if apply_vendor_default else 0,
                        now,
                        existing["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO vendor_account_mappings (
                        id, tenant_id, seller_tax_code, item_keyword_pattern,
                        is_vendor_default, debit_account, credit_account,
                        mapped_item_code, approved_count, last_used_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        new_id(),
                        tenant_id,
                        seller_tax_code,
                        pattern,
                        1 if apply_vendor_default else 0,
                        item.debit_account,
                        item.credit_account,
                        item.mapped_item_code,
                        now,
                    ),
                )
            saved += 1
    return saved
