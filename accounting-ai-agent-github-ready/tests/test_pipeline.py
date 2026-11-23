from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.coding_agent import record_coding_feedback, suggest_journal_entries
from app.db import Database
from app.main import create_app
from app.rule_engine import validate_invoice, validate_vn_tax_code
from app.xml_parser import parse_tt78_xml

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def test_vn_mst_mod11_checksum():
    assert validate_vn_tax_code("0100109106")[0] is True
    assert validate_vn_tax_code("0300588569")[0] is True
    assert validate_vn_tax_code("0101243150-001")[0] is True
    assert validate_vn_tax_code("0100109107")[0] is False  # bad check digit
    assert validate_vn_tax_code("12345")[0] is False  # bad format


def test_xml_tt78_parser_multi_vat():
    xml_bytes = (SAMPLES_DIR / "01_hd_gtgt_hop_le_da_thue_suat.xml").read_bytes()
    inv = parse_tt78_xml(xml_bytes)

    assert inv.extraction_source == "xml_tt78"
    assert inv.invoice_meta.series == "C26THH"
    assert inv.invoice_meta.invoice_number == "00001234"
    assert inv.invoice_meta.has_valid_signature is True
    assert inv.seller.tax_code == "0100109106"
    assert len(inv.line_items) == 3
    assert [li.vat_rate for li in inv.line_items] == ["8%", "10%", "0%"]
    assert inv.totals.grand_total == 5460000.0

    issues = validate_invoice(inv)
    assert issues == []


def test_rule_engine_catches_math_error_and_replacement_warning():
    bad_xml = (SAMPLES_DIR / "02_hd_loi_toan_hoc.xml").read_bytes()
    bad_inv = parse_tt78_xml(bad_xml)
    bad_issues = validate_invoice(bad_inv)
    codes = {i.code for i in bad_issues}
    assert "LINE_MATH_MISMATCH" in codes
    assert "GRAND_TOTAL_MISMATCH" in codes

    rep_xml = (SAMPLES_DIR / "03_hd_thay_the_tt78.xml").read_bytes()
    rep_inv = parse_tt78_xml(rep_xml)
    assert rep_inv.invoice_meta.invoice_type == "REPLACEMENT"
    assert rep_inv.invoice_meta.related_invoice_ref == "C26TVC-00005599"
    rep_issues = validate_invoice(rep_inv)
    assert any(i.code == "RELATED_INVOICE_ALERT" for i in rep_issues)


def test_coding_feedback_loop_learns_from_accountant(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    xml_bytes = (SAMPLES_DIR / "01_hd_gtgt_hop_le_da_thue_suat.xml").read_bytes()
    inv = parse_tt78_xml(xml_bytes)

    entries_before, count_before = suggest_journal_entries(inv, db)
    assert count_before == 0
    assert any(e.debit_account == "1331" for e in entries_before)

    # Accountant changes line 1 debit account to 6423-CUSTOM and approves
    inv.line_items[0].debit_account = "6428"
    inv.line_items[0].credit_account = "331"
    inv.line_items[0].mapped_item_code = "VPP-001"
    saved = record_coding_feedback(db, "default-sme", inv.seller.tax_code or "", inv.line_items)
    assert saved == 3

    # Re-parse a fresh invoice from same vendor -> should recall 6428 & VPP-001
    inv_next = parse_tt78_xml(xml_bytes)
    entries_after, count_after = suggest_journal_entries(inv_next, db)
    assert count_after == 1
    assert inv_next.line_items[0].debit_account == "6428"
    assert inv_next.line_items[0].mapped_item_code == "VPP-001"
    assert any("đã duyệt 1 lần" in e.reason for e in entries_after)


def test_end_to_end_api_reconciliation_duplicate_and_misa_export(tmp_path: Path):
    app = create_app(db_path=tmp_path / "e2e.db")
    client = TestClient(app)

    # 1. Seed demo data (3 XML invoices + PO + GRN + Bank Statement)
    seed_resp = client.post("/api/demo/seed")
    assert seed_resp.status_code == 200
    seeded = seed_resp.json()["seeded_invoices"]
    assert len(seeded) == 3

    by_file = {s["file"]: s for s in seeded}
    assert by_file["01_hd_gtgt_hop_le_da_thue_suat.xml"]["status"] == "MATCHED"
    assert by_file["02_hd_loi_toan_hoc.xml"]["status"] == "VALIDATION_FAILED"
    assert by_file["03_hd_thay_the_tt78.xml"]["status"] == "NEEDS_REVIEW"

    # 2. Verify 3-way reconciliation matches on invoice 01
    inv1_id = by_file["01_hd_gtgt_hop_le_da_thue_suat.xml"]["id"]
    detail = client.get(f"/api/invoices/{inv1_id}").json()
    match_types = {m["match_type"] for m in detail["reconciliation_matches"]}
    assert match_types == {"2WAY_PO", "3WAY_GRN", "BANK_STMT"}
    assert all(m["status"] == "MATCHED" for m in detail["reconciliation_matches"])

    # 3. Uploading the exact same invoice again must be flagged as DUPLICATE_INVOICE
    dup_resp = client.post(
        "/api/invoices/upload",
        files={"file": ("dup.xml", (SAMPLES_DIR / "01_hd_gtgt_hop_le_da_thue_suat.xml").read_bytes(), "application/xml")},
    )
    assert dup_resp.status_code == 200
    dup_data = dup_resp.json()
    assert dup_data["status"] == "FLAGGED"
    assert any(e["code"] == "DUPLICATE_INVOICE" for e in dup_data["validation_errors"])

    # 4. Approve invoice 01 -> audit trail updated & rules learned
    appr = client.post(f"/api/invoices/{inv1_id}/approve?apply_vendor_default=true")
    assert appr.status_code == 200
    assert appr.json()["status"] == "APPROVED"
    assert appr.json()["learned_rules_count"] == 3

    # 5. Export MISA Excel (.xlsx) and verify workbook structure
    exp = client.get("/api/export/misa?only_approved=true&mark_synced=true")
    assert exp.status_code == 200
    wb = load_workbook(io.BytesIO(exp.content))
    ws = wb.active
    assert ws.title == "MISA_ChungTuMuaHang"
    assert ws.cell(row=1, column=1).value == "Ngày hạch toán"
    assert ws.max_row == 4  # 1 header + 3 line items from invoice 01

    # Verify invoice 01 moved to SYNCED
    after_sync = client.get(f"/api/invoices/{inv1_id}").json()
    assert after_sync["status"] == "SYNCED"

    # 6. Duplicate report & safe delete endpoints
    dup_report = client.get("/api/invoices/duplicates").json()
    assert dup_report["total_duplicate_groups"] == 1
    assert dup_report["total_extra_duplicates"] == 1
    dup_id = dup_report["duplicate_groups"][0]["duplicates"][0]["id"]

    # Delete without confirm=true must be blocked (400)
    assert client.delete(f"/api/invoices/{dup_id}").status_code == 400
    assert client.delete(f"/api/invoices/{dup_id}?confirm=true").status_code == 200
    assert client.get("/api/invoices/duplicates").json()["total_extra_duplicates"] == 0

    # Delete ALL without exact phrase must be blocked (400)
    assert client.delete("/api/invoices?confirm_phrase=WRONG").status_code == 400
    # RBAC: accountant cannot purge all data (403)
    assert client.delete("/api/invoices?confirm_phrase=XOA%20TAT%20CA", headers={"X-User-Role": "accountant"}).status_code == 403

    # Multi-ERP exports (Fast Excel & Odoo CSV) + live metrics
    fast_resp = client.get("/api/export/fast")
    assert fast_resp.status_code == 200
    assert load_workbook(io.BytesIO(fast_resp.content)).active.title == "FAST_PN_MuaHang"

    odoo_resp = client.get("/api/export/odoo")
    assert odoo_resp.status_code == 200
    assert "partner_id/vat" in odoo_resp.text

    metrics_resp = client.get("/api/metrics")
    assert metrics_resp.status_code == 200
    assert metrics_resp.json()["total_invoices"] == 3

    purge = client.delete("/api/invoices?confirm_phrase=XOA%20TAT%20CA", headers={"X-User-Role": "chief_accountant"})
    assert purge.status_code == 200
    assert purge.json()["deleted_invoices"] == 3
    assert client.get("/api/invoices").json()["summary"]["total"] == 0


def test_rbac_approval_limits_and_batch_upload(tmp_path: Path):
    app = create_app(db_path=tmp_path / "rbac.db")
    client = TestClient(app)

    # Batch upload 2 XML invoices at once
    f1 = (SAMPLES_DIR / "01_hd_gtgt_hop_le_da_thue_suat.xml").read_bytes()
    f2 = (SAMPLES_DIR / "02_hd_loi_toan_hoc.xml").read_bytes()
    batch = client.post(
        "/api/invoices/upload-batch",
        files=[("files", ("a.xml", f1, "application/xml")), ("files", ("b.xml", f2, "application/xml"))],
    )
    assert batch.status_code == 200
    assert len(batch.json()["succeeded"]) == 2
    failed_inv_id = batch.json()["succeeded"][1]["id"]

    # Accountant cannot approve a VALIDATION_FAILED invoice (403), Chief Accountant can (200)
    assert client.post(f"/api/invoices/{failed_inv_id}/approve", headers={"X-User-Role": "accountant"}).status_code == 403
    assert client.post(f"/api/invoices/{failed_inv_id}/approve", headers={"X-User-Role": "chief_accountant"}).status_code == 200

    # Settings RBAC: accountant blocked (403), admin allowed (200)
    assert client.patch("/api/settings", json={"accounting_regime": "TT133"}, headers={"X-User-Role": "accountant"}).status_code == 403
    ok_set = client.patch("/api/settings", json={"accounting_regime": "TT200"}, headers={"X-User-Role": "admin"})
    assert ok_set.status_code == 200
    assert ok_set.json()["accounting_regime"] == "TT200"
