"""Processing pipeline: store file -> extract -> validate -> match -> decide."""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
import uuid
from threading import Thread

from app import checks
from app.config import settings
from app.db import DB, now
from app.extract import Extractor, ExtractionError, pdf_text
from app.schema import InvoiceData

log = logging.getLogger("invoiceiq")

ALLOWED = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class UploadError(ValueError):
    pass


def sniff_mime(content: bytes) -> str:
    """Trust the bytes, not the file name or the browser's content type."""
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    raise UploadError("Only PDF, PNG, JPEG or WEBP files are accepted.")


class Pipeline:
    def __init__(self, db: DB, extractor: Extractor):
        self.db, self.extractor = db, extractor
        settings.upload_dir.mkdir(parents=True, exist_ok=True)

    def _cost(self, tin: int, tout: int) -> float:
        return round(tin / 1e6 * settings.price_in_per_m + tout / 1e6 * settings.price_out_per_m, 6)

    def _evaluate(self, data: InvoiceData, text: str, sha: str, exclude_id: int | None = None):
        issues = checks.validate(data, text)
        m, match_issues = checks.match(data, self.db, sha, exclude_id)
        issues += match_issues
        return issues, m, checks.decide(issues)

    def process(self, content: bytes, file_name: str) -> dict:
        if len(content) > settings.max_upload_mb * 1024 * 1024:
            raise UploadError(f"File is larger than {settings.max_upload_mb} MB.")
        if not content:
            raise UploadError("The file is empty.")
        mime = sniff_mime(content)
        sha = hashlib.sha256(content).hexdigest()
        stored = f"{uuid.uuid4().hex}{ALLOWED[mime]}"
        (settings.upload_dir / stored).write_bytes(content)
        text = pdf_text(content) if mime == "application/pdf" else ""
        base = {"file_name": file_name[:200], "stored_name": stored, "mime": mime, "sha256": sha,
                "extractor": self.extractor.name, "has_text_layer": int(bool(text.strip())), "created_at": now()}
        try:
            ex = self.extractor.extract(content, mime)
        except ExtractionError as exc:
            invoice_id = self.db.insert_invoice(
                **base, status="failed", data={}, issues=[checks.issue("EXTRACTION_FAILED", checks.ERROR, str(exc))],
                match={}, tokens_in=0, tokens_out=0, cost_usd=0, extract_ms=0, decided_at=None, decided_by=None)
            return self.db.get_invoice(invoice_id)

        issues, m, status = self._evaluate(ex.data, text, sha)
        invoice_id = self.db.insert_invoice(
            **base, status=status, data=ex.data.model_dump(), issues=issues, match=m,
            tokens_in=ex.tokens_in, tokens_out=ex.tokens_out, cost_usd=self._cost(ex.tokens_in, ex.tokens_out),
            extract_ms=ex.ms, decided_at=now() if status != "needs_review" else None,
            decided_by="rules" if status != "needs_review" else None)
        if status == "auto_approved":
            self.db.post_to_ledger(invoice_id, checks.ledger_entries(ex.data, m))
            self._notify(invoice_id)
        return self.db.get_invoice(invoice_id)

    def update_fields(self, invoice_id: int, data: dict) -> dict:
        """A reviewer corrected fields: validate again and re-run matching."""
        inv = self.db.get_invoice(invoice_id)
        if not inv:
            raise KeyError(invoice_id)
        if inv["status"] not in {"needs_review", "failed"}:
            raise ValueError(f"Invoice is '{inv['status']}' and can no longer be edited.")
        parsed = InvoiceData.model_validate(data)
        text = pdf_text((settings.upload_dir / inv["stored_name"]).read_bytes()) if inv["mime"] == "application/pdf" else ""
        issues, m, _ = self._evaluate(parsed, text, inv["sha256"], exclude_id=invoice_id)
        self.db.update_invoice(invoice_id, data=parsed.model_dump(), issues=issues, match=m, edited=1,
                               status="needs_review")
        return self.db.get_invoice(invoice_id)

    def decide(self, invoice_id: int, approve: bool, reviewer: str, note: str) -> dict:
        inv = self.db.get_invoice(invoice_id)
        if not inv:
            raise KeyError(invoice_id)
        if inv["status"] != "needs_review":
            raise ValueError(f"Invoice is '{inv['status']}', not waiting for review.")
        blocking = {i["code"] for i in inv["issues"]} & {"DUPLICATE", "PO_CLOSED", "UNKNOWN_VENDOR", "MISSING_FIELD"}
        if approve and blocking:
            raise ValueError("Cannot approve while these remain: " + ", ".join(sorted(blocking)) +
                             ". Fix the fields or reject the invoice.")
        status = "approved" if approve else "rejected"
        self.db.update_invoice(invoice_id, status=status, decided_at=now(), decided_by=reviewer[:60], note=note[:500])
        if approve:
            self.db.post_to_ledger(invoice_id, checks.ledger_entries(InvoiceData.model_validate(inv["data"]), inv["match"]))
            self._notify(invoice_id)
        return self.db.get_invoice(invoice_id)

    def _notify(self, invoice_id: int) -> None:
        """POST approved invoices to a webhook (n8n, Zapier, Make, Slack...). Fire and forget."""
        if not settings.webhook_url:
            return
        inv = self.db.get_invoice(invoice_id)
        payload = json.dumps({"event": "invoice.approved", "id": invoice_id, "status": inv["status"],
                              "invoice": inv["data"], "po": (inv["match"] or {}).get("po"),
                              "vendor": (inv["match"] or {}).get("vendor")}).encode()

        def send():
            try:
                req = urllib.request.Request(settings.webhook_url, data=payload,
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=10).close()
            except Exception:  # noqa: BLE001
                log.warning("Approval webhook failed for invoice %s", invoice_id, exc_info=True)
        Thread(target=send, daemon=True).start()
