"""Extractors turn an invoice file (PDF or image) into InvoiceData.

- GeminiExtractor: sends the document itself (PDF or image) to Gemini and asks
  for JSON matching LLM_SCHEMA. Works on any layout and on scans.
- RegexExtractor: the "classic" approach (read the PDF text layer, apply
  label regexes). Kept as a baseline for the eval; it cannot read scans.
- FakeExtractor: offline stand-in that returns the ground truth of the bundled
  samples. Used by tests and by the demo when no API key is configured.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.config import settings
from app.schema import LLM_SCHEMA, InvoiceData


class ExtractionError(RuntimeError):
    pass


class DailyQuotaExceeded(ExtractionError):
    pass


@dataclass
class Extraction:
    data: InvoiceData
    tokens_in: int = 0
    tokens_out: int = 0
    ms: int = 0


class Extractor(Protocol):
    name: str

    def extract(self, content: bytes, mime: str) -> Extraction: ...


def pdf_text(content: bytes) -> str:
    """Text layer of a PDF ('' for scans/images or unreadable files)."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:  # noqa: BLE001 - a broken text layer just means "no text"
        return ""


def _clean(raw: dict) -> InvoiceData:
    """Empty strings -> None, then validate."""
    def blank(v):
        return None if isinstance(v, str) and not v.strip() else v
    data = {k: blank(v) for k, v in raw.items()}
    data["line_items"] = [{k: blank(v) for k, v in (li or {}).items()} for li in (raw.get("line_items") or [])]
    return InvoiceData.model_validate(data)


# ------------------------------------------------------------------ Gemini

PROMPT = """You are reading a supplier invoice for an accounts-payable team.
Extract the fields in the JSON schema. Rules:
- Transcribe what is PRINTED. Never calculate, correct or "fix" numbers: if a line total or the invoice total looks
  wrong, still return the printed value. Validation happens later.
- Numbers as plain decimals: "2.400,00 €" -> 2400.00, "$1,240.00" -> 1240.00.
- Dates as YYYY-MM-DD. "03/11/2026" on a US invoice is March 11; "11.03.2026" is 11 March.
- vendor_name is the company that issued the invoice, not the "bill to" customer.
- po_number: the purchase order / order reference / "your ref" exactly as printed, or empty string if there is none.
- Include every line item on every page, in order. Do not include subtotal/tax/total rows as line items.
- tax: the tax/VAT amount (0 if there is none).
- unclear_fields: list any field you could not read with confidence (e.g. blurry digits)."""


class GeminiExtractor:
    def __init__(self, api_key: str, model: str, rpm: int):
        if not api_key:
            raise ExtractionError("GEMINI_API_KEY is not set. Put it in .env or use LLM_PROVIDER=fake.")
        from google import genai

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.name = f"gemini:{model}"
        self._interval = 60.0 / max(rpm, 1)
        self._next = 0.0
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            time.sleep(wait)

    def extract(self, content: bytes, mime: str) -> Extraction:
        types = self._genai.types
        config = types.GenerateContentConfig(
            temperature=0, response_mime_type="application/json", response_json_schema=LLM_SCHEMA,
        )
        parts = [types.Part.from_bytes(data=content, mime_type=mime), PROMPT]
        t0 = time.perf_counter()
        for attempt in range(2):  # a truncated or empty JSON answer gets one more try
            resp = self._call(parts, config)
            try:
                raw = json.loads(resp.text or "")
                if isinstance(raw, dict):
                    break
                raise ValueError("expected a JSON object")
            except ValueError as exc:
                if attempt == 1:
                    reason = getattr((resp.candidates or [None])[0], "finish_reason", None)
                    raise ExtractionError(f"Model did not return valid JSON (finish reason: {reason}): {exc}") from exc
        ms = int((time.perf_counter() - t0) * 1000)
        usage = resp.usage_metadata
        return Extraction(
            data=_clean(raw), ms=ms,
            tokens_in=(usage.prompt_token_count or 0) if usage else 0,
            tokens_out=((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0,
        )

    def _call(self, parts, config):
        delay = 2.0
        for attempt in range(5):
            self._throttle()
            try:
                return self.client.models.generate_content(model=self.model, contents=parts, config=config)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "PerDay" in msg or "per day" in msg.lower():
                    raise DailyQuotaExceeded(
                        "Gemini free-tier daily limit reached. It resets at midnight Pacific time (1:30 PM IST).") from exc
                retryable = any(s in msg for s in ("429", "RESOURCE_EXHAUSTED", "500", "502", "503", "504", "UNAVAILABLE",
                                                   "DEADLINE", "INTERNAL", "overloaded", "timed out", "Timeout",
                                                   "disconnect", "Connection", "reset by peer"))
                if not retryable or attempt == 4:
                    raise ExtractionError(f"{type(exc).__name__}: {msg[:500]}") from exc
                m = re.search(r"retry in ([\d.]+)s|retryDelay['\"]?: ?['\"]?(\d+)", msg)
                time.sleep(min(float(m.group(1) or m.group(2)) + 1 if m else delay, 65))
                delay *= 2
        raise ExtractionError("unreachable")


# --------------------------------------------------------- regex baseline

_NUM = r"[-+]?[\d.,]+"


def parse_amount(s: str | None) -> float | None:
    """'$1,240.00' -> 1240.0, '2.400,00 €' -> 2400.0"""
    if not s:
        return None
    s = re.sub(r"[^\d.,-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else s.replace(",", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def parse_date(s: str | None) -> str | None:
    from datetime import datetime

    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%B %d, %Y", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


class RegexExtractor:
    """What most teams try first: label regexes on the PDF text layer."""

    name = "regex-baseline"
    LABELS = {
        "invoice_number": r"(?:Invoice No:|Invoice #|Inv\. No\.)\s*\n?\s*([A-Z0-9/\-]+)",
        "invoice_date": r"(?:Invoice Date:|Issued|Date)\s*\n?\s*([0-9]{1,2}[ ./][A-Za-z0-9]{2,3}[ ./][0-9]{4}|[0-9/]{10})",
        "due_date": r"(?:Due Date:|Due|Payable by)\s*\n?\s*([0-9]{1,2}[ ./][A-Za-z0-9]{2,3}[ ./][0-9]{4}|[0-9/]{10})",
        "po_number": r"(?:PO Number:|Your ref|Order Ref)\s*\n?\s*([A-Za-z0-9.#\- ]{3,12})",
        "total": r"(?:Total Due|Amount due|Gross total)\s*\n?\s*(" + _NUM + r"\s*€?|\$" + _NUM + ")",
        "subtotal": r"(?:Subtotal|Net total)\s*\n?\s*(" + _NUM + r"\s*€?|\$" + _NUM + ")",
    }

    def extract(self, content: bytes, mime: str) -> Extraction:
        t0 = time.perf_counter()
        text = pdf_text(content) if mime == "application/pdf" else ""
        out: dict = {}
        for field, pat in self.LABELS.items():
            m = re.search(pat, text)
            out[field] = m.group(1).strip() if m else None
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        vendor = lines[0] if lines else None
        if vendor in {"INVOICE", "Tax invoice"} and len(lines) > 1:
            vendor = lines[1]
        data = InvoiceData(
            vendor_name=vendor,
            invoice_number=out["invoice_number"],
            invoice_date=parse_date(out["invoice_date"]),
            due_date=parse_date(out["due_date"]),
            po_number=out["po_number"],
            currency="EUR" if "€" in text else ("USD" if "$" in text else None),
            subtotal=parse_amount(out["subtotal"]),
            total=parse_amount(out["total"]),
            line_items=[],  # table rows are not recoverable reliably from the text layer
        )
        return Extraction(data=data, ms=int((time.perf_counter() - t0) * 1000))


# ------------------------------------------------------------------- Fake

class FakeExtractor:
    """Returns the ground truth for bundled samples (matched by file hash)."""

    name = "fake"

    def __init__(self, samples_dir: Path | None = None):
        self._truth: dict[str, dict] = {}
        d = samples_dir or settings.samples_dir
        for meta in d.glob("*.json"):
            info = json.loads(meta.read_text())
            f = d / info["file"]
            if f.exists():
                self._truth[hashlib.sha256(f.read_bytes()).hexdigest()] = info["truth"]

    def extract(self, content: bytes, mime: str) -> Extraction:
        truth = self._truth.get(hashlib.sha256(content).hexdigest())
        if truth is None:
            return Extraction(data=InvoiceData(unclear_fields=["all (offline mode only knows the bundled samples)"]))
        return Extraction(data=_clean(truth))


def make_extractor() -> Extractor:
    if settings.llm_provider == "fake":
        return FakeExtractor()
    return GeminiExtractor(settings.gemini_api_key, settings.extract_model, settings.gemini_rpm)
