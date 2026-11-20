"""The structured invoice every extractor must return."""
from __future__ import annotations

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    description: str = ""
    sku: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class InvoiceData(BaseModel):
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None      # YYYY-MM-DD
    due_date: str | None = None          # YYYY-MM-DD
    po_number: str | None = None         # exactly as printed
    currency: str | None = None          # ISO 4217
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    unclear_fields: list[str] = Field(default_factory=list)   # fields the model could not read confidently


# JSON schema sent to the LLM (kept explicit so it reads like documentation).
LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name": {"type": "string", "description": "Company that ISSUED the invoice (not the bill-to customer)."},
        "vendor_tax_id": {"type": "string", "description": "Empty string if not printed."},
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
        "due_date": {"type": "string", "description": "ISO date YYYY-MM-DD, or empty string."},
        "po_number": {"type": "string",
                      "description": "Purchase order / order reference exactly as printed, or empty string if none."},
        "currency": {"type": "string", "description": "ISO 4217 code, e.g. USD, EUR."},
        "line_items": {"type": "array", "items": {"type": "object", "properties": {
            "description": {"type": "string"},
            "sku": {"type": "string", "description": "Item code if printed, else empty string."},
            "quantity": {"type": "number"},
            "unit_price": {"type": "number"},
            "amount": {"type": "number", "description": "The line total exactly as printed."},
        }, "required": ["description", "quantity", "unit_price", "amount"]}},
        "subtotal": {"type": "number", "description": "As printed."},
        "tax": {"type": "number", "description": "As printed; 0 if no tax line."},
        "total": {"type": "number", "description": "As printed."},
        "unclear_fields": {"type": "array", "items": {"type": "string"},
                           "description": "Names of fields you could not read with confidence."},
    },
    "required": ["vendor_name", "invoice_number", "invoice_date", "currency", "line_items", "subtotal", "tax", "total"],
}
