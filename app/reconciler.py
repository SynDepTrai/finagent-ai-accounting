from __future__ import annotations

import json
import os
import re
from typing import Any

from app.db import Database, new_id, utc_now
from app.schemas import ValidationIssue, VietnamVATInvoiceExtraction

EXACT_TOLERANCE_VND = 10.0
MINOR_DIFF_VND = 1000.0
MINOR_DIFF_PCT = 0.005  # 0.5%


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _match_items(inv_items: list[dict], ref_items: list[dict]) -> list[dict]:
    """Compare invoice line items against PO/GRN items by token overlap + quantity/price variance."""
    comparisons: list[dict] = []
    for inv in inv_items:
        inv_tokens = _tokens(inv.get("description", ""))
        best_ref = None
        best_score = 0.0
        for ref in ref_items:
            ref_tokens = _tokens(ref.get("description", ""))
            if not inv_tokens or not ref_tokens:
                continue
            score = len(inv_tokens & ref_tokens) / len(inv_tokens | ref_tokens)
            if score > best_score:
                best_score = score
                best_ref = ref
        if best_ref and best_score >= 0.35:
            qty_diff = (inv.get("quantity") or 0) - (best_ref.get("quantity") or 0)
            price_diff = (inv.get("unit_price") or 0) - (best_ref.get("unit_price") or 0)
            comparisons.append(
                {
                    "invoice_item": inv.get("description"),
                    "ref_item": best_ref.get("description"),
                    "similarity": round(best_score, 2),
                    "qty_diff": qty_diff,
                    "price_diff": price_diff,
                }
            )
    return comparisons


def reconcile_invoice(
    invoice_id: str,
    invoice: VietnamVATInvoiceExtraction,
    db: Database,
    tenant_id: str = "default-sme",
) -> list[dict[str, Any]]:
    """Run 2-way (PO / Bank Statement) and 3-way (PO + GRN) reconciliation against reference_docs."""
    seller_mst = (invoice.seller.tax_code or "").strip()
    grand_total = invoice.totals.grand_total

    with db.connect() as conn:
        # Clear previous matches for re-run
        conn.execute("DELETE FROM reconciliation_matches WHERE invoice_id = ?", (invoice_id,))

        candidates = conn.execute(
            """
            SELECT * FROM reference_docs
            WHERE tenant_id = ?
              AND (vendor_tax_code = ? OR doc_type = 'BANK_STMT')
            ORDER BY doc_date DESC
            """,
            (tenant_id, seller_mst),
        ).fetchall()

    matches: list[dict[str, Any]] = []
    inv_items_dict = [i.model_dump() for i in invoice.line_items]

    by_type: dict[str, list[Any]] = {"PO": [], "GRN": [], "BANK_STMT": []}
    for c in candidates:
        if c["doc_type"] in by_type:
            by_type[c["doc_type"]].append(c)

    # Pick closest amount match per doc_type
    for doc_type, match_type in [("PO", "2WAY_PO"), ("GRN", "3WAY_GRN"), ("BANK_STMT", "BANK_STMT")]:
        pool = by_type[doc_type]
        if not pool:
            continue
        # For bank statements, also require either same vendor_tax_code or invoice number in note or close amount
        if doc_type == "BANK_STMT":
            filtered = [
                r
                for r in pool
                if r["vendor_tax_code"] == seller_mst
                or (invoice.invoice_meta.invoice_number and invoice.invoice_meta.invoice_number in (r["note"] or ""))
                or abs(r["total_amount"] - grand_total) <= MINOR_DIFF_VND
            ]
            pool = filtered
            if not pool:
                continue

        best = min(pool, key=lambda r: abs(r["total_amount"] - grand_total))
        variance = round(grand_total - float(best["total_amount"]), 2)
        abs_var = abs(variance)
        pct_var = abs_var / max(grand_total, 1.0)

        if abs_var <= EXACT_TOLERANCE_VND:
            status = "MATCHED"
        elif abs_var <= MINOR_DIFF_VND or pct_var <= MINOR_DIFF_PCT:
            status = "MINOR_DIFF"
        else:
            status = "FLAGGED"

        ref_items = json.loads(best["items_json"] or "[]")
        item_diffs = _match_items(inv_items_dict, ref_items) if ref_items else []
        if any(d["qty_diff"] != 0 for d in item_diffs) and status == "MATCHED":
            status = "FLAGGED"

        diff_details = {
            "ref_code": best["ref_code"],
            "ref_date": best["doc_date"],
            "ref_total": best["total_amount"],
            "invoice_total": grand_total,
            "item_comparisons": item_diffs,
            "note": best["note"],
        }

        match_record = {
            "id": new_id(),
            "match_type": match_type,
            "ref_document_id": best["ref_code"],
            "amount_variance": variance,
            "status": status,
            "diff_details": diff_details,
        }
        matches.append(match_record)

        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO reconciliation_matches (
                    id, tenant_id, invoice_id, match_type, ref_document_id,
                    amount_variance, status, diff_details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_record["id"],
                    tenant_id,
                    invoice_id,
                    match_type,
                    best["ref_code"],
                    variance,
                    status,
                    json.dumps(diff_details, ensure_ascii=False),
                    utc_now(),
                ),
            )

    return matches


def determine_invoice_status(
    invoice: VietnamVATInvoiceExtraction,
    issues: list[ValidationIssue],
    matches: list[dict[str, Any]],
    vendor_approved_count: int,
) -> str:
    """State Machine decision matching Section 2.2 of the design doc."""
    error_codes = {i.code for i in issues if i.severity == "ERROR"}
    if "DUPLICATE_INVOICE" in error_codes:
        return "FLAGGED"
    if error_codes:
        return "VALIDATION_FAILED"

    match_statuses = {m["status"] for m in matches}
    if "FLAGGED" in match_statuses:
        return "FLAGGED"
    if "MINOR_DIFF" in match_statuses:
        return "MINOR_DIFF"

    # Check confidence threshold
    min_conf = min(
        [m.confidence_score for m in invoice.field_metadata.values()]
        + [li.confidence_score for li in invoice.line_items]
        + [1.0]
    )
    has_warnings = any(i.severity == "WARNING" for i in issues)
    if min_conf < 0.95 or has_warnings:
        return "NEEDS_REVIEW"

    # Evaluate 5 STP (Straight-Through Processing) conditions
    stp_max = float(os.getenv("STP_MAX_AMOUNT_VND", "20000000"))
    if (
        (invoice.extraction_source == "xml_tt78" or min_conf >= 0.95)
        and vendor_approved_count >= 3
        and invoice.totals.grand_total <= stp_max
    ):
        return "APPROVED"

    return "MATCHED" if matches else "NEEDS_REVIEW"
