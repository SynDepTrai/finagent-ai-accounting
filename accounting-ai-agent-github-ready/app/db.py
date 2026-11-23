from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "accounting.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


class Database:
    """SQLite persistence matching the 6 core tables in the design doc (+ reference_docs for PO/GRN/Bank)."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    source_channel TEXT NOT NULL DEFAULT 'web',
                    mime_type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    direction TEXT NOT NULL DEFAULT 'INBOUND',
                    extraction_source TEXT NOT NULL,
                    series TEXT NOT NULL DEFAULT '',
                    invoice_number TEXT NOT NULL DEFAULT '',
                    issue_date TEXT NOT NULL DEFAULT '',
                    seller_tax_code TEXT NOT NULL DEFAULT '',
                    seller_name TEXT NOT NULL DEFAULT '',
                    buyer_tax_code TEXT NOT NULL DEFAULT '',
                    grand_total REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'VND',
                    status TEXT NOT NULL DEFAULT 'RECEIVED',
                    extracted_payload TEXT NOT NULL,
                    validation_errors TEXT NOT NULL DEFAULT '[]',
                    suggested_journal_entries TEXT NOT NULL DEFAULT '[]',
                    erp_voucher_id TEXT,
                    processing_ms REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_active_key
                ON invoices(tenant_id, seller_tax_code, series, invoice_number)
                WHERE status != 'REJECTED' AND seller_tax_code != '' AND invoice_number != '';

                CREATE TABLE IF NOT EXISTS line_items (
                    id TEXT PRIMARY KEY,
                    invoice_id TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                    line_no INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    quantity REAL,
                    unit_price REAL,
                    amount_before_tax REAL NOT NULL DEFAULT 0,
                    vat_rate TEXT NOT NULL DEFAULT '10%',
                    vat_amount REAL NOT NULL DEFAULT 0,
                    mapped_item_code TEXT,
                    debit_account TEXT,
                    credit_account TEXT
                );

                CREATE TABLE IF NOT EXISTS reference_docs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    doc_type TEXT NOT NULL,
                    ref_code TEXT NOT NULL,
                    vendor_tax_code TEXT,
                    vendor_name TEXT,
                    doc_date TEXT NOT NULL,
                    total_amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'VND',
                    items_json TEXT NOT NULL DEFAULT '[]',
                    note TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reconciliation_matches (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    invoice_id TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                    match_type TEXT NOT NULL,
                    ref_document_id TEXT NOT NULL,
                    amount_variance REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    diff_details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vendor_account_mappings (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    seller_tax_code TEXT NOT NULL,
                    item_keyword_pattern TEXT NOT NULL,
                    is_vendor_default INTEGER NOT NULL DEFAULT 0,
                    debit_account TEXT NOT NULL,
                    credit_account TEXT NOT NULL,
                    mapped_item_code TEXT,
                    approved_count INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    before_state TEXT,
                    after_state TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    def log_audit(
        self,
        tenant_id: str,
        entity_type: str,
        entity_id: str,
        actor_id: str,
        action: str,
        before_state: Any = None,
        after_state: Any = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (tenant_id, entity_type, entity_id, actor_id, action, before_state, after_state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    entity_type,
                    entity_id,
                    actor_id,
                    action,
                    json.dumps(before_state, ensure_ascii=False) if before_state is not None else None,
                    json.dumps(after_state, ensure_ascii=False) if after_state is not None else None,
                    utc_now(),
                ),
            )
