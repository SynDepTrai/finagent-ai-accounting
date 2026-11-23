from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class BoundingBox(BaseModel):
    page: int = Field(default=1, ge=1)
    bbox: list[float] = Field(min_length=4, max_length=4, description="[x1, y1, x2, y2] normalized 0-1000")


class FieldMeta(BaseModel):
    confidence_score: float = Field(ge=0.0, le=1.0)
    page: int = Field(default=1, ge=1)
    bbox: Optional[list[float]] = Field(default=None, min_length=4, max_length=4)


class InvoiceMeta(BaseModel):
    form_code: str = "1"
    series: str = ""
    invoice_number: str = ""
    issue_date: str = ""
    tax_authority_code: Optional[str] = None
    invoice_type: Literal["ORIGINAL", "ADJUSTMENT", "REPLACEMENT", "CANCELLED"] = "ORIGINAL"
    related_invoice_ref: Optional[str] = None
    currency: str = "VND"
    exchange_rate: float = 1.0
    payment_method: Optional[str] = None
    has_valid_signature: Optional[bool] = None


class PartyInfo(BaseModel):
    tax_code: Optional[str] = None
    name: str = ""
    address: Optional[str] = None
    bank_account: Optional[str] = None


class LineItem(BaseModel):
    line_no: int
    nature: Literal["GOODS", "PROMOTION", "DISCOUNT", "NOTE"] = "GOODS"
    item_code: Optional[str] = None
    description: str
    unit: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    discount_amount: float = 0.0
    amount_before_tax: float = 0.0
    vat_rate: str = "10%"
    vat_amount: float = 0.0
    total_amount: float = 0.0
    confidence_score: float = 1.0
    bounding_box: Optional[BoundingBox] = None
    mapped_item_code: Optional[str] = None
    debit_account: Optional[str] = None
    credit_account: Optional[str] = None


class Totals(BaseModel):
    subtotal: float = 0.0
    total_discount: float = 0.0
    total_vat: float = 0.0
    grand_total: float = 0.0
    amount_in_words: Optional[str] = None


class VietnamVATInvoiceExtraction(BaseModel):
    extraction_source: Literal["xml_tt78", "pdf_text", "vlm_ocr"]
    invoice_meta: InvoiceMeta
    seller: PartyInfo
    buyer: PartyInfo
    line_items: list[LineItem] = Field(default_factory=list)
    totals: Totals
    field_metadata: dict[str, FieldMeta] = Field(default_factory=dict)


class ValidationIssue(BaseModel):
    code: str
    severity: Literal["ERROR", "WARNING"]
    field: str
    message: str


class JournalEntry(BaseModel):
    line_no: int
    description: str
    debit_account: str
    credit_account: str
    amount: float
    reason: str
    confidence: float = 0.9


class ReferenceDocInput(BaseModel):
    doc_type: Literal["PO", "GRN", "BANK_STMT"]
    ref_code: str
    vendor_tax_code: Optional[str] = None
    vendor_name: Optional[str] = None
    doc_date: str
    total_amount: float
    currency: str = "VND"
    items: list[dict] = Field(default_factory=list)
    note: Optional[str] = None


class InvoiceReviewPatch(BaseModel):
    invoice_meta: Optional[InvoiceMeta] = None
    seller: Optional[PartyInfo] = None
    buyer: Optional[PartyInfo] = None
    line_items: Optional[list[LineItem]] = None
    totals: Optional[Totals] = None
    suggested_journal_entries: Optional[list[JournalEntry]] = None
    apply_vendor_default: bool = False
    actor_id: str = "accountant-1"
