from __future__ import annotations

import email
import imaplib
from email.header import decode_header
from pathlib import Path
from typing import Callable

ALLOWED_EXTS = {".xml", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}


def _decode_filename(raw: str) -> str:
    parts = decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(text))
    return "".join(out)


def fetch_email_attachments(
    imap_host: str,
    username: str,
    password: str,
    folder: str = "INBOX",
    limit: int = 20,
    ingest_fn: Callable[[bytes, str, str, str], dict] | None = None,
) -> list[dict]:
    """Connect to an IMAP mailbox via TLS and ingest unread invoice attachments (.xml/.pdf/image)."""
    results: list[dict] = []
    with imaplib.IMAP4_SSL(imap_host) as mail:
        mail.login(username, password)
        mail.select(folder)
        status, data = mail.search(None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return results

        msg_ids = data[0].split()[-limit:]
        for mid in msg_ids:
            typ, msg_data = mail.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data:
                continue
            raw_email = msg_data[0][1]
            if not isinstance(raw_email, bytes):
                continue
            msg = email.message_from_bytes(raw_email)
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                raw_name = part.get_filename()
                if not raw_name:
                    continue
                filename = _decode_filename(raw_name)
                ext = Path(filename).suffix.lower()
                if ext not in ALLOWED_EXTS:
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                mime = part.get_content_type() or "application/octet-stream"
                if ingest_fn is not None:
                    ingested = ingest_fn(payload, filename, mime, "email")
                    results.append({"filename": filename, "invoice_id": ingested["id"], "status": ingested["status"]})
    return results
