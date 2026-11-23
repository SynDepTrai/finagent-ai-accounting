from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.auth import UserContext, check_approval_permission, get_current_user, require_roles
from app.coding_agent import record_coding_feedback, suggest_journal_entries
from app.db import DEFAULT_DB_PATH, Database, new_id, utc_now
from app.email_ingestor import fetch_email_attachments
from app.misa_exporter import build_fast_excel, build_misa_excel, build_odoo_csv
from app.reconciler import determine_invoice_status, reconcile_invoice
from app.rule_engine import validate_invoice
from app.schemas import InvoiceReviewPatch, ReferenceDocInput, VietnamVATInvoiceExtraction
from app.vlm_extractor import extract_with_vlm_or_pdf
from app.xml_parser import parse_tt78_xml


class EmailSyncRequest(BaseModel):
    imap_host: str = "imap.gmail.com"
    username: str
    password: str
    folder: str = "INBOX"
    limit: int = 20


class SettingsPatch(BaseModel):
    accounting_regime: Optional[str] = None
    stp_max_amount_vnd: Optional[float] = None

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
STATIC_DIR = BASE_DIR / "static"
SAMPLES_DIR = BASE_DIR / "samples"
DEFAULT_TENANT = os.getenv("DEFAULT_TENANT_ID", "default-sme")


def create_app(db_path: Path | str = DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(title="AI Accounting Agent MVP (Vietnam)", version="1.0.0")
    db = Database(db_path)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html_path = STATIC_DIR / "index.html"
        if not html_path.exists():
            return HTMLResponse("<h1>AI Accounting Agent API is running</h1>")
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "has_gemini_key": bool(os.getenv("GEMINI_API_KEY", "").strip())}

    @app.get("/api/metrics")
    def metrics() -> dict:
        """Live operational telemetry: STP rate, P95 latency, source breakdown, validation failure rate."""
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT status, extraction_source, processing_ms FROM invoices"
            ).fetchall()
        total = len(rows)
        if total == 0:
            return {"total_invoices": 0, "stp_rate": 0.0, "p95_latency_ms": 0.0, "by_source": {}, "by_status": {}}

        latencies = sorted(float(r["processing_ms"] or 0.0) for r in rows)
        p95_idx = min(int(0.95 * (total - 1)), total - 1)
        by_source: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for r in rows:
            by_source[r["extraction_source"]] = by_source.get(r["extraction_source"], 0) + 1
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1

        stp_count = by_status.get("APPROVED", 0) + by_status.get("SYNCED", 0) + by_status.get("MATCHED", 0)
        return {
            "total_invoices": total,
            "stp_or_matched_rate": round(stp_count / total, 4),
            "validation_fail_rate": round(by_status.get("VALIDATION_FAILED", 0) / total, 4),
            "p95_latency_ms": round(latencies[p95_idx], 2),
            "avg_latency_ms": round(sum(latencies) / total, 2),
            "by_source": by_source,
            "by_status": by_status,
        }

    def _ingest_invoice_bytes(
        file_bytes: bytes,
        filename: str,
        mime_type: str,
        source_channel: str = "web",
        tenant_id: str = DEFAULT_TENANT,
    ) -> dict:
        t0 = time.perf_counter()
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        doc_id = new_id()
        safe_ext = Path(filename).suffix.lower() or ".bin"
        storage_path = UPLOAD_DIR / f"{doc_id}{safe_ext}"
        storage_path.write_bytes(file_bytes)

        now = utc_now()
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO documents (id, tenant_id, file_hash, original_filename, storage_path, source_channel, mime_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (doc_id, tenant_id, file_hash, filename, str(storage_path), source_channel, mime_type, now),
            )

        # Step 2 & 3: Classification + Waterfall Extraction (XML-first -> PDF/VLM)
        if safe_ext == ".xml" or "xml" in mime_type:
            extraction = parse_tt78_xml(file_bytes)
        else:
            extraction = extract_with_vlm_or_pdf(file_bytes, mime_type or "application/pdf")

        # Step 4: Deterministic Rule Engine
        issues = validate_invoice(extraction, db=db, tenant_id=tenant_id)

        # Step 5: Accounting Coding Agent (TT200/TT133 + History Feedback)
        journal_entries, vendor_approved_count = suggest_journal_entries(extraction, db=db, tenant_id=tenant_id)

        inv_id = new_id()
        # Handle duplicate key gracefully in DB while flagging in UI
        is_duplicate = any(i.code == "DUPLICATE_INVOICE" for i in issues)
        series_for_db = extraction.invoice_meta.series
        number_for_db = extraction.invoice_meta.invoice_number
        if is_duplicate:
            # Suffix internal DB unique key so flagged duplicate can still be inspected by Chief Accountant
            number_for_db = f"{number_for_db}-DUP-{inv_id[:4]}"

        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO invoices (
                    id, tenant_id, document_id, direction, extraction_source,
                    series, invoice_number, issue_date, seller_tax_code, seller_name,
                    buyer_tax_code, grand_total, currency, status,
                    extracted_payload, validation_errors, suggested_journal_entries,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'INBOUND', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'EXTRACTED', ?, ?, ?, ?, ?)
                """,
                (
                    inv_id,
                    tenant_id,
                    doc_id,
                    extraction.extraction_source,
                    series_for_db,
                    number_for_db,
                    extraction.invoice_meta.issue_date,
                    extraction.seller.tax_code or "",
                    extraction.seller.name,
                    extraction.buyer.tax_code or "",
                    extraction.totals.grand_total,
                    extraction.invoice_meta.currency,
                    extraction.model_dump_json(),
                    json.dumps([i.model_dump() for i in issues], ensure_ascii=False),
                    json.dumps([j.model_dump() for j in journal_entries], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            for li in extraction.line_items:
                conn.execute(
                    """
                    INSERT INTO line_items (
                        id, invoice_id, line_no, description, quantity, unit_price,
                        amount_before_tax, vat_rate, vat_amount, mapped_item_code,
                        debit_account, credit_account
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id(),
                        inv_id,
                        li.line_no,
                        li.description,
                        li.quantity,
                        li.unit_price,
                        li.amount_before_tax,
                        li.vat_rate,
                        li.vat_amount,
                        li.mapped_item_code,
                        li.debit_account,
                        li.credit_account,
                    ),
                )

        # Step 6: 2-way / 3-way Reconciliation & Final State
        matches = reconcile_invoice(inv_id, extraction, db=db, tenant_id=tenant_id)
        final_status = determine_invoice_status(extraction, issues, matches, vendor_approved_count)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

        with db.connect() as conn:
            conn.execute(
                "UPDATE invoices SET status = ?, processing_ms = ?, updated_at = ? WHERE id = ?",
                (final_status, elapsed_ms, utc_now(), inv_id),
            )

        db.log_audit(
            tenant_id=tenant_id,
            entity_type="invoice",
            entity_id=inv_id,
            actor_id="system-agent",
            action="EXTRACTED",
            after_state={"status": final_status, "source": extraction.extraction_source, "issues": len(issues)},
        )
        return _get_invoice_detail(inv_id)

    def _get_invoice_detail(invoice_id: str) -> dict:
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT i.*, d.original_filename, d.mime_type, d.source_channel
                FROM invoices i JOIN documents d ON d.id = i.document_id
                WHERE i.id = ?
                """,
                (invoice_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Không tìm thấy hoá đơn.")
            matches = [
                {**dict(m), "diff_details": json.loads(m["diff_details"] or "{}")}
                for m in conn.execute("SELECT * FROM reconciliation_matches WHERE invoice_id = ?", (invoice_id,)).fetchall()
            ]
            audits = [
                dict(a)
                for a in conn.execute(
                    "SELECT * FROM audit_logs WHERE entity_id = ? ORDER BY id DESC LIMIT 20", (invoice_id,)
                ).fetchall()
            ]

        data = dict(row)
        data["extracted_payload"] = json.loads(data["extracted_payload"])
        data["validation_errors"] = json.loads(data["validation_errors"])
        data["suggested_journal_entries"] = json.loads(data["suggested_journal_entries"])
        data["reconciliation_matches"] = matches
        data["audit_logs"] = audits
        data["document_url"] = f"/api/documents/{data['document_id']}/raw"
        return data

    @app.post("/api/invoices/upload")
    async def upload_invoice(
        file: UploadFile = File(...),
        source_channel: str = Query(default="web"),
    ) -> dict:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="File tải lên rỗng.")
        try:
            return _ingest_invoice_bytes(
                file_bytes=content,
                filename=file.filename or "invoice.xml",
                mime_type=file.content_type or "application/octet-stream",
                source_channel=source_channel,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/invoices/upload-batch")
    async def upload_invoices_batch(
        files: list[UploadFile] = File(...),
        source_channel: str = Query(default="web"),
    ) -> dict:
        succeeded, failed = [], []
        for f in files:
            content = await f.read()
            if not content:
                failed.append({"filename": f.filename, "error": "File rỗng"})
                continue
            try:
                res = _ingest_invoice_bytes(
                    file_bytes=content,
                    filename=f.filename or "invoice.xml",
                    mime_type=f.content_type or "application/octet-stream",
                    source_channel=source_channel,
                )
                succeeded.append({"id": res["id"], "filename": f.filename, "status": res["status"]})
            except Exception as exc:
                failed.append({"filename": f.filename, "error": str(exc)})
        return {"succeeded": succeeded, "failed": failed}

    @app.post("/api/ingest/email-sync")
    def sync_email_invoices(
        req: EmailSyncRequest,
        user: UserContext = Depends(get_current_user),
    ) -> dict:
        require_roles(user, {"chief_accountant", "admin"}, "Đồng bộ Email IMAP")
        try:
            ingested = fetch_email_attachments(
                imap_host=req.imap_host,
                username=req.username,
                password=req.password,
                folder=req.folder,
                limit=req.limit,
                ingest_fn=_ingest_invoice_bytes,
            )
            return {"ingested_count": len(ingested), "items": ingested}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Lỗi kết nối Email IMAP: {exc}") from exc

    @app.get("/api/invoices")
    def list_invoices(status: Optional[str] = None) -> dict:
        with db.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT id, series, invoice_number, issue_date, seller_tax_code, seller_name, grand_total, currency, status, extraction_source, created_at FROM invoices WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, series, invoice_number, issue_date, seller_tax_code, seller_name, grand_total, currency, status, extraction_source, created_at FROM invoices ORDER BY created_at DESC"
                ).fetchall()
            all_statuses = conn.execute("SELECT status, COUNT(*) as cnt FROM invoices GROUP BY status").fetchall()

        counts = {r["status"]: r["cnt"] for r in all_statuses}
        return {
            "items": [dict(r) for r in rows],
            "summary": {
                "total": sum(counts.values()),
                "approved_or_synced": counts.get("APPROVED", 0) + counts.get("SYNCED", 0) + counts.get("MATCHED", 0),
                "needs_review": counts.get("NEEDS_REVIEW", 0) + counts.get("MINOR_DIFF", 0),
                "flagged_or_failed": counts.get("FLAGGED", 0) + counts.get("VALIDATION_FAILED", 0),
                "by_status": counts,
            },
        }

    @app.get("/api/invoices/duplicates")
    def list_duplicates() -> dict:
        """Scan and report duplicate invoices by (seller_tax_code, series, base_invoice_number) or file_hash."""
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT i.id, i.series, i.invoice_number, i.issue_date, i.seller_tax_code,
                       i.seller_name, i.grand_total, i.currency, i.status, i.validation_errors,
                       i.created_at, d.file_hash, d.original_filename
                FROM invoices i
                JOIN documents d ON d.id = i.document_id
                ORDER BY i.created_at ASC
                """
            ).fetchall()

        groups: dict[str, list[dict]] = {}
        for r in rows:
            item = dict(r)
            base_num = (item["invoice_number"] or "").split("-DUP-")[0]
            item["base_invoice_number"] = base_num
            if item["seller_tax_code"] and base_num:
                key = f"KEY:{item['seller_tax_code']}|{item['series']}|{base_num}"
            else:
                key = f"HASH:{item['file_hash']}"
            groups.setdefault(key, []).append(item)

        duplicate_groups = []
        total_extra_duplicates = 0
        for key, items in groups.items():
            if len(items) > 1:
                original = items[0]
                dups = items[1:]
                total_extra_duplicates += len(dups)
                duplicate_groups.append(
                    {
                        "group_key": key,
                        "original": original,
                        "duplicates": dups,
                    }
                )

        return {
            "duplicate_groups": duplicate_groups,
            "total_duplicate_groups": len(duplicate_groups),
            "total_extra_duplicates": total_extra_duplicates,
        }

    @app.delete("/api/invoices/{invoice_id}")
    def delete_invoice(
        invoice_id: str,
        confirm: bool = Query(default=False),
        actor_id: str = Query(default="accountant-1"),
    ) -> dict:
        if not confirm:
            raise HTTPException(status_code=400, detail="Bắt buộc xác nhận (confirm=true) trước khi xoá hoá đơn.")
        detail = _get_invoice_detail(invoice_id)
        with db.connect() as conn:
            conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
        db.log_audit(
            tenant_id=DEFAULT_TENANT,
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=actor_id,
            action="DELETED",
            before_state={
                "series": detail["series"],
                "invoice_number": detail["invoice_number"],
                "grand_total": detail["grand_total"],
            },
        )
        return {"deleted_id": invoice_id, "status": "deleted"}

    @app.delete("/api/invoices")
    def delete_all_invoices(
        confirm_phrase: str = Query(default=""),
        include_reference_docs: bool = Query(default=True),
        user: UserContext = Depends(get_current_user),
    ) -> dict:
        require_roles(user, {"chief_accountant", "admin"}, "Xoá toàn bộ dữ liệu")
        if confirm_phrase.strip().upper() != "XOA TAT CA":
            raise HTTPException(
                status_code=400,
                detail="Mã xác nhận không đúng. Cần truyền confirm_phrase='XOA TAT CA' để xoá toàn bộ dữ liệu.",
            )
        with db.connect() as conn:
            inv_count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            conn.execute("DELETE FROM reconciliation_matches")
            conn.execute("DELETE FROM line_items")
            conn.execute("DELETE FROM invoices")
            conn.execute("DELETE FROM documents")
            ref_deleted = 0
            if include_reference_docs:
                ref_deleted = conn.execute("SELECT COUNT(*) FROM reference_docs").fetchone()[0]
                conn.execute("DELETE FROM reference_docs")
        db.log_audit(
            tenant_id=DEFAULT_TENANT,
            entity_type="system",
            entity_id="all",
            actor_id=f"{user.user_id} ({user.role})",
            action="PURGE_ALL",
            before_state={"deleted_invoices": inv_count, "deleted_reference_docs": ref_deleted},
        )
        return {"deleted_invoices": inv_count, "deleted_reference_docs": ref_deleted}

    @app.get("/api/invoices/{invoice_id}")
    def get_invoice(invoice_id: str) -> dict:
        return _get_invoice_detail(invoice_id)

    @app.get("/api/documents/{document_id}/raw")
    def get_raw_document(document_id: str) -> FileResponse:
        with db.connect() as conn:
            row = conn.execute("SELECT storage_path, mime_type, original_filename FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None or not Path(row["storage_path"]).exists():
            raise HTTPException(status_code=404, detail="Không tìm thấy file gốc.")
        return FileResponse(row["storage_path"], media_type=row["mime_type"], filename=row["original_filename"])

    @app.patch("/api/invoices/{invoice_id}")
    def update_invoice(invoice_id: str, patch: InvoiceReviewPatch) -> dict:
        current = _get_invoice_detail(invoice_id)
        before_payload = current["extracted_payload"]
        extraction = VietnamVATInvoiceExtraction.model_validate(before_payload)

        if patch.invoice_meta is not None:
            extraction.invoice_meta = patch.invoice_meta
        if patch.seller is not None:
            extraction.seller = patch.seller
        if patch.buyer is not None:
            extraction.buyer = patch.buyer
        if patch.line_items is not None:
            extraction.line_items = patch.line_items
        if patch.totals is not None:
            extraction.totals = patch.totals

        issues = validate_invoice(extraction, db=db, tenant_id=DEFAULT_TENANT, exclude_invoice_id=invoice_id)
        entries = (
            [j.model_dump() for j in patch.suggested_journal_entries]
            if patch.suggested_journal_entries is not None
            else current["suggested_journal_entries"]
        )
        new_status = "VALIDATION_FAILED" if any(i.severity == "ERROR" for i in issues) else "NEEDS_REVIEW"

        with db.connect() as conn:
            conn.execute(
                """
                UPDATE invoices
                SET series = ?, invoice_number = ?, issue_date = ?, seller_tax_code = ?, seller_name = ?,
                    grand_total = ?, status = ?, extracted_payload = ?, validation_errors = ?,
                    suggested_journal_entries = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    extraction.invoice_meta.series,
                    extraction.invoice_meta.invoice_number,
                    extraction.invoice_meta.issue_date,
                    extraction.seller.tax_code or "",
                    extraction.seller.name,
                    extraction.totals.grand_total,
                    new_status,
                    extraction.model_dump_json(),
                    json.dumps([i.model_dump() for i in issues], ensure_ascii=False),
                    json.dumps(entries, ensure_ascii=False),
                    utc_now(),
                    invoice_id,
                ),
            )

        db.log_audit(
            tenant_id=DEFAULT_TENANT,
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=patch.actor_id,
            action="EDITED",
            before_state={"grand_total": before_payload.get("totals", {}).get("grand_total")},
            after_state={"grand_total": extraction.totals.grand_total, "status": new_status},
        )
        return _get_invoice_detail(invoice_id)

    @app.post("/api/invoices/{invoice_id}/approve")
    def approve_invoice(
        invoice_id: str,
        apply_vendor_default: bool = Query(default=False),
        user: UserContext = Depends(get_current_user),
    ) -> dict:
        detail = _get_invoice_detail(invoice_id)
        check_approval_permission(user, detail["status"], float(detail["grand_total"] or 0))
        extraction = VietnamVATInvoiceExtraction.model_validate(detail["extracted_payload"])

        # Trigger Feedback Loop so next invoice from this vendor auto-codes accurately
        learned_rules = record_coding_feedback(
            db=db,
            tenant_id=DEFAULT_TENANT,
            seller_tax_code=extraction.seller.tax_code or "",
            line_items=extraction.line_items,
            apply_vendor_default=apply_vendor_default,
        )

        with db.connect() as conn:
            conn.execute("UPDATE invoices SET status = 'APPROVED', updated_at = ? WHERE id = ?", (utc_now(), invoice_id))

        db.log_audit(
            tenant_id=DEFAULT_TENANT,
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=f"{user.user_id} ({user.role})",
            action="APPROVED",
            before_state={"status": detail["status"]},
            after_state={"status": "APPROVED", "learned_rules": learned_rules},
        )
        result = _get_invoice_detail(invoice_id)
        result["learned_rules_count"] = learned_rules
        return result

    @app.post("/api/invoices/{invoice_id}/status/{new_status}")
    def set_invoice_status(invoice_id: str, new_status: str, actor_id: str = Query(default="accountant-1")) -> dict:
        allowed = {"FLAGGED", "REJECTED", "NEEDS_REVIEW", "SYNCED"}
        if new_status not in allowed:
            raise HTTPException(status_code=400, detail=f"Trạng thái không hợp lệ. Chỉ cho phép: {allowed}")
        detail = _get_invoice_detail(invoice_id)
        with db.connect() as conn:
            conn.execute("UPDATE invoices SET status = ?, updated_at = ? WHERE id = ?", (new_status, utc_now(), invoice_id))
        db.log_audit(
            tenant_id=DEFAULT_TENANT,
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=actor_id,
            action=new_status,
            before_state={"status": detail["status"]},
            after_state={"status": new_status},
        )
        return _get_invoice_detail(invoice_id)

    @app.post("/api/reference-docs")
    def create_reference_doc(doc: ReferenceDocInput) -> dict:
        ref_id = new_id()
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO reference_docs (
                    id, tenant_id, doc_type, ref_code, vendor_tax_code, vendor_name,
                    doc_date, total_amount, currency, items_json, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref_id,
                    DEFAULT_TENANT,
                    doc.doc_type,
                    doc.ref_code,
                    doc.vendor_tax_code,
                    doc.vendor_name,
                    doc.doc_date,
                    doc.total_amount,
                    doc.currency,
                    json.dumps(doc.items, ensure_ascii=False),
                    doc.note,
                    utc_now(),
                ),
            )
        return {"id": ref_id, **doc.model_dump()}

    @app.get("/api/reference-docs")
    def list_reference_docs() -> list[dict]:
        with db.connect() as conn:
            rows = conn.execute("SELECT * FROM reference_docs ORDER BY created_at DESC").fetchall()
        return [{**dict(r), "items": json.loads(r["items_json"] or "[]")} for r in rows]

    @app.post("/api/invoices/{invoice_id}/reconcile")
    def rerun_reconciliation(invoice_id: str) -> dict:
        detail = _get_invoice_detail(invoice_id)
        extraction = VietnamVATInvoiceExtraction.model_validate(detail["extracted_payload"])
        reconcile_invoice(invoice_id, extraction, db=db, tenant_id=DEFAULT_TENANT)
        return _get_invoice_detail(invoice_id)

    @app.get("/api/export/misa")
    def export_misa(
        only_approved: bool = Query(default=False),
        mark_synced: bool = Query(default=False),
    ) -> Response:
        with db.connect() as conn:
            if only_approved:
                rows = conn.execute("SELECT * FROM invoices WHERE status IN ('APPROVED', 'MATCHED', 'SYNCED')").fetchall()
            else:
                rows = conn.execute("SELECT * FROM invoices WHERE status != 'REJECTED'").fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Không có hoá đơn nào để xuất Excel MISA.")

        xlsx_bytes = build_misa_excel([dict(r) for r in rows])

        if mark_synced:
            with db.connect() as conn:
                for r in rows:
                    if r["status"] in {"APPROVED", "MATCHED"}:
                        conn.execute("UPDATE invoices SET status = 'SYNCED', updated_at = ? WHERE id = ?", (utc_now(), r["id"]))

        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="MISA_Import_ChungTuMuaHang.xlsx"'},
        )

    @app.get("/api/export/fast")
    def export_fast(only_approved: bool = Query(default=False)) -> Response:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE status IN ('APPROVED', 'MATCHED', 'SYNCED')"
                if only_approved
                else "SELECT * FROM invoices WHERE status != 'REJECTED'"
            ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Không có hoá đơn nào để xuất Fast Accounting.")
        return Response(
            content=build_fast_excel([dict(r) for r in rows]),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="FAST_Import_PhieuNhapMuaHang.xlsx"'},
        )

    @app.get("/api/export/odoo")
    def export_odoo(only_approved: bool = Query(default=False)) -> Response:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE status IN ('APPROVED', 'MATCHED', 'SYNCED')"
                if only_approved
                else "SELECT * FROM invoices WHERE status != 'REJECTED'"
            ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Không có hoá đơn nào để xuất Odoo CSV.")
        return Response(
            content=build_odoo_csv([dict(r) for r in rows]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="Odoo_Vendor_Bills.csv"'},
        )

    @app.get("/api/settings")
    def get_settings() -> dict:
        return {
            "accounting_regime": os.getenv("ACCOUNTING_REGIME", "TT200").upper(),
            "stp_max_amount_vnd": float(os.getenv("STP_MAX_AMOUNT_VND", "20000000")),
            "has_gemini_key": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        }

    @app.patch("/api/settings")
    def update_settings(patch: SettingsPatch, user: UserContext = Depends(get_current_user)) -> dict:
        require_roles(user, {"chief_accountant", "admin"}, "Cập nhật cấu hình hệ thống")
        if patch.accounting_regime and patch.accounting_regime.upper() in {"TT200", "TT133"}:
            os.environ["ACCOUNTING_REGIME"] = patch.accounting_regime.upper()
        if patch.stp_max_amount_vnd is not None and patch.stp_max_amount_vnd >= 0:
            os.environ["STP_MAX_AMOUNT_VND"] = str(int(patch.stp_max_amount_vnd))
        return get_settings()

    @app.get("/api/eval/report")
    def get_eval_report() -> dict:
        report_path = BASE_DIR / "eval" / "report.json"
        if not report_path.exists():
            return {"passed": None, "metrics": {}, "cases": []}
        return json.loads(report_path.read_text(encoding="utf-8"))

    @app.post("/api/eval/run")
    def trigger_eval_run() -> dict:
        from eval.run_eval import run_eval

        exit_code = run_eval()
        report = get_eval_report()
        report["exit_code"] = exit_code
        return report

    @app.post("/api/demo/seed")
    def seed_demo_data() -> dict:
        """Load realistic Vietnam TT78 XML samples + PO/GRN/Bank Statement for instant demo."""
        if not SAMPLES_DIR.exists():
            raise HTTPException(status_code=404, detail="Không tìm thấy thư mục samples/.")

        # 1. Seed PO, GRN, Bank Statement if empty
        with db.connect() as conn:
            ref_count = conn.execute("SELECT COUNT(*) FROM reference_docs").fetchone()[0]
        if ref_count == 0:
            demo_refs = [
                ReferenceDocInput(
                    doc_type="PO",
                    ref_code="PO-2026-0088",
                    vendor_tax_code="0100109106",
                    vendor_name="CÔNG TY CỔ PHẦN VĂN PHÒNG PHẨM HỒNG HÀ",
                    doc_date="2026-10-12",
                    total_amount=5460000,
                    items=[
                        {"description": "Giấy in A4 Double A 80gsm", "quantity": 50, "unit_price": 80000},
                        {"description": "Mực in laser HP 107A chính hãng", "quantity": 1, "unit_price": 1000000},
                    ],
                ),
                ReferenceDocInput(
                    doc_type="GRN",
                    ref_code="PNK-2026-0142",
                    vendor_tax_code="0100109106",
                    vendor_name="CÔNG TY CỔ PHẦN VĂN PHÒNG PHẨM HỒNG HÀ",
                    doc_date="2026-10-14",
                    total_amount=5460000,
                    items=[
                        {"description": "Giấy in A4 Double A 80gsm", "quantity": 50, "unit_price": 80000},
                        {"description": "Mực in laser HP 107A", "quantity": 1, "unit_price": 1000000},
                    ],
                ),
                ReferenceDocInput(
                    doc_type="BANK_STMT",
                    ref_code="VCB-FT2628800192",
                    vendor_tax_code="0100109106",
                    vendor_name="CONG TY CP VPP HONG HA",
                    doc_date="2026-10-16",
                    total_amount=5460000,
                    note="TT HD 00001234 VPP HONG HA",
                ),
            ]
            for ref in demo_refs:
                create_reference_doc(ref)

        # 2. Ingest sample XML files
        ingested = []
        for xml_file in sorted(SAMPLES_DIR.glob("*.xml")):
            res = _ingest_invoice_bytes(
                file_bytes=xml_file.read_bytes(),
                filename=xml_file.name,
                mime_type="application/xml",
                source_channel="demo",
            )
            ingested.append({"id": res["id"], "file": xml_file.name, "status": res["status"]})

        return {"seeded_invoices": ingested}

    return app


app = create_app()
