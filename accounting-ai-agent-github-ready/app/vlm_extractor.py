from __future__ import annotations

import base64
import io
import json
import os
import re
from typing import Optional

import httpx
from pypdf import PdfReader

from app.schemas import (
    FieldMeta,
    InvoiceMeta,
    LineItem,
    PartyInfo,
    Totals,
    VietnamVATInvoiceExtraction,
)

EXTRACTION_PROMPT = """Bạn là hệ thống trích xuất dữ liệu hoá đơn GTGT Việt Nam (Thông tư 78/2021/TT-BTC).
Hãy đọc chứng từ đính kèm và trả về DUY NHẤT 1 đối tượng JSON đúng cấu trúc sau (không bọc markdown):
{
  "extraction_source": "vlm_ocr",
  "invoice_meta": {
    "form_code": "1",
    "series": "C25TAA",
    "invoice_number": "00001234",
    "issue_date": "YYYY-MM-DD",
    "tax_authority_code": null,
    "invoice_type": "ORIGINAL",
    "related_invoice_ref": null,
    "currency": "VND",
    "exchange_rate": 1.0,
    "payment_method": "TM/CK"
  },
  "seller": {"tax_code": "...", "name": "...", "address": "...", "bank_account": null},
  "buyer": {"tax_code": "...", "name": "...", "address": "..."},
  "line_items": [
    {
      "line_no": 1,
      "nature": "GOODS",
      "item_code": null,
      "description": "...",
      "unit": "...",
      "quantity": 1,
      "unit_price": 100000,
      "discount_amount": 0,
      "amount_before_tax": 100000,
      "vat_rate": "10%",
      "vat_amount": 10000,
      "total_amount": 110000,
      "confidence_score": 0.95
    }
  ],
  "totals": {
    "subtotal": 100000,
    "total_discount": 0,
    "total_vat": 10000,
    "grand_total": 110000,
    "amount_in_words": "..."
  },
  "field_metadata": {
    "seller.tax_code": {"confidence_score": 0.95, "page": 1, "bbox": [100, 120, 400, 150]},
    "totals.grand_total": {"confidence_score": 0.95, "page": 1, "bbox": [600, 780, 920, 820]}
  }
}
Quy tắc:
- Không tự bịa số nếu ảnh mờ; ghi rõ confidence_score thấp (< 0.8) cho trường khó đọc.
- vat_rate phải thuộc ["0%", "5%", "8%", "10%", "KCT", "KKKNT", "KHAC"].
- bbox chuẩn hoá [x1, y1, x2, y2] trên thang 0-1000.
"""


def _extract_pdf_text(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:
        return ""


def _parse_vn_number(raw: str) -> float:
    cleaned = raw.strip().replace(" ", "")
    if "." in cleaned and "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") >= 1 and len(cleaned.split(".")[-1]) == 3:
        cleaned = cleaned.replace(".", "")
    elif cleaned.count(",") >= 1 and len(cleaned.split(",")[-1]) == 3:
        cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _fallback_pdf_text_parse(text: str) -> VietnamVATInvoiceExtraction:
    """Offline fallback parser when PDF has selectable text and no GEMINI_API_KEY is configured."""
    msts = re.findall(r"\b\d{10}(?:-\d{3})?\b", text)
    seller_mst = msts[0] if len(msts) >= 1 else None
    buyer_mst = msts[1] if len(msts) >= 2 else None

    series_match = re.search(r"(?:Ký hiệu|Ky hieu|Serial)[:\s]+([A-Z0-9]{6})", text, re.IGNORECASE)
    num_match = re.search(r"(?:Số|So|No\.?)[:\s]+(\d{1,8})\b", text, re.IGNORECASE)
    date_match = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", text)
    issue_date = ""
    if date_match:
        d, m, y = date_match.groups()
        issue_date = f"{y}-{int(m):02d}-{int(d):02d}"

    grand_match = re.search(r"(?:Tổng cộng tiền thanh toán|Total payment)[:\s]+([\d.,]+)", text, re.IGNORECASE)
    vat_match = re.search(r"(?:Tiền thuế GTGT|VAT amount)[:\s]+([\d.,]+)", text, re.IGNORECASE)
    sub_match = re.search(r"(?:Cộng tiền hàng|Subtotal)[:\s]+([\d.,]+)", text, re.IGNORECASE)

    grand = _parse_vn_number(grand_match.group(1)) if grand_match else 0.0
    vat = _parse_vn_number(vat_match.group(1)) if vat_match else 0.0
    sub = _parse_vn_number(sub_match.group(1)) if sub_match else max(grand - vat, 0.0)

    # Low confidence forces human review on the UI
    low_meta = {
        k: FieldMeta(confidence_score=0.72)
        for k in ["invoice_meta.series", "invoice_meta.invoice_number", "seller.tax_code", "totals.grand_total"]
    }

    return VietnamVATInvoiceExtraction(
        extraction_source="pdf_text",
        invoice_meta=InvoiceMeta(
            series=series_match.group(1) if series_match else "",
            invoice_number=num_match.group(1) if num_match else "",
            issue_date=issue_date,
        ),
        seller=PartyInfo(tax_code=seller_mst, name="(Trích xuất PDF text — cần kiểm tra tên NCC)"),
        buyer=PartyInfo(tax_code=buyer_mst, name=""),
        line_items=[
            LineItem(
                line_no=1,
                description="Hàng hoá/dịch vụ từ PDF (chưa bật VLM API — vui lòng kiểm tra chi tiết)",
                amount_before_tax=sub,
                vat_rate="10%" if vat > 0 else "0%",
                vat_amount=vat,
                total_amount=grand,
                confidence_score=0.72,
            )
        ],
        totals=Totals(subtotal=sub, total_vat=vat, grand_total=grand),
        field_metadata=low_meta,
    )


def extract_with_vlm_or_pdf(file_bytes: bytes, mime_type: str) -> VietnamVATInvoiceExtraction:
    """Waterfall extractor: PDF text check -> Gemini 2.5 Flash VLM (if API key set) -> offline PDF fallback."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

    if not api_key:
        if mime_type == "application/pdf":
            pdf_text = _extract_pdf_text(file_bytes)
            if pdf_text:
                return _fallback_pdf_text_parse(pdf_text)
        raise ValueError(
            "Chưa cấu hình GEMINI_API_KEY để đọc ảnh chụp/PDF scan. "
            "Hãy thiết lập biến môi trường GEMINI_API_KEY hoặc tải lên file hoá đơn .xml Thông tư 78."
        )

    b64_data = base64.b64encode(file_bytes).decode("ascii")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": EXTRACTION_PROMPT},
                    {"inline_data": {"mime_type": mime_type, "data": b64_data}},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
        },
    }

    with httpx.Client(timeout=45.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()

    text_out = body["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text_out)
    parsed["extraction_source"] = "vlm_ocr"
    return VietnamVATInvoiceExtraction.model_validate(parsed)
