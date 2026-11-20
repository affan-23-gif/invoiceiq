"""Validation, vendor/PO matching and the approve/review/reject decision.

Everything here is deterministic Python: the LLM only reads the document.
Whether an invoice gets paid is decided by rules a finance team can read.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from rapidfuzz import fuzz

from app.config import settings
from app.db import DB
from app.schema import InvoiceData

ERROR, WARNING, INFO = "error", "warning", "info"

_LEGAL = {"inc", "llc", "ltd", "co", "gmbh", "corp", "corporation", "company", "limited", "plc", "sa", "ag", "bv"}


def issue(code: str, severity: str, message: str, field: str | None = None) -> dict:
    return {"code": code, "severity": severity, "message": message, "field": field}


def norm_name(s: str | None) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in _LEGAL)


def norm_invoice_number(s: str | None) -> str | None:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper()) or None


def norm_po(s: str | None) -> str | None:
    """'P.O. #1046', 'po 1046', 'PO-1046' -> 'PO-1046'."""
    digits = re.findall(r"\d+", s or "")
    return f"PO-{digits[-1]}" if digits else None


def norm_tax_id(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _close(a: float | None, b: float | None, tol: float = settings.amount_tolerance) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def _amount_variants(x: float) -> set[str]:
    us = f"{x:,.2f}"
    eu = us.replace(",", "X").replace(".", ",").replace("X", ".")
    return {us, eu, f"{x:.2f}", f"{x:.2f}".replace(".", ",")}


# ------------------------------------------------------------- validation

def validate(inv: InvoiceData, text: str, today: date | None = None) -> list[dict]:
    today = today or date.today()
    out: list[dict] = []
    for f in ("vendor_name", "invoice_number", "invoice_date", "currency", "total"):
        if getattr(inv, f) in (None, ""):
            out.append(issue("MISSING_FIELD", ERROR, f"Could not find the {f.replace('_', ' ')}.", f))

    # Dates
    try:
        d_inv = date.fromisoformat(inv.invoice_date) if inv.invoice_date else None
    except ValueError:
        d_inv = None
        out.append(issue("DATE_INVALID", WARNING, f"Invoice date '{inv.invoice_date}' is not a valid date.", "invoice_date"))
    if d_inv and d_inv > today + timedelta(days=7):
        out.append(issue("DATE_INVALID", WARNING, f"Invoice date {d_inv} is in the future.", "invoice_date"))
    if inv.due_date:
        try:
            d_due = date.fromisoformat(inv.due_date)
            if d_inv and d_due < d_inv:
                out.append(issue("DATE_INVALID", WARNING, "Due date is before the invoice date.", "due_date"))
        except ValueError:
            out.append(issue("DATE_INVALID", WARNING, f"Due date '{inv.due_date}' is not a valid date.", "due_date"))

    # Arithmetic, on the PRINTED numbers
    for i, li in enumerate(inv.line_items):
        if li.quantity is not None and li.unit_price is not None and li.amount is not None:
            expected = round(li.quantity * li.unit_price, 2)
            if not _close(expected, li.amount):
                out.append(issue("MATH_LINE", ERROR,
                                 f"Line {i + 1} ({li.description[:40]}): {li.quantity:g} × {li.unit_price:,.2f} = "
                                 f"{expected:,.2f}, but the invoice says {li.amount:,.2f}.", f"line_items.{i}"))
    if inv.line_items and inv.subtotal is not None:
        s = round(sum(li.amount or 0 for li in inv.line_items), 2)
        if not _close(s, inv.subtotal):
            out.append(issue("MATH_SUBTOTAL", ERROR,
                             f"Line items add up to {s:,.2f}, but the subtotal says {inv.subtotal:,.2f}.", "subtotal"))
    if inv.subtotal is not None and inv.total is not None:
        expected = round(inv.subtotal + (inv.tax or 0), 2)
        if not _close(expected, inv.total):
            out.append(issue("MATH_TOTAL", ERROR,
                             f"Subtotal + tax = {expected:,.2f}, but the total says {inv.total:,.2f}.", "total"))

    # Anti-hallucination: key values must appear in the PDF's own text layer.
    if text:
        flat = re.sub(r"\s+", " ", text)
        if inv.invoice_number and inv.invoice_number not in flat:
            out.append(issue("UNVERIFIED_TEXT", WARNING,
                             f"Invoice number '{inv.invoice_number}' was not found in the PDF text.", "invoice_number"))
        if inv.total is not None and not any(v in flat for v in _amount_variants(inv.total)):
            out.append(issue("UNVERIFIED_TEXT", WARNING,
                             f"Total {inv.total:,.2f} was not found in the PDF text.", "total"))

    if inv.unclear_fields:
        out.append(issue("LOW_CONFIDENCE", INFO, "The model was unsure about: " + ", ".join(inv.unclear_fields)))
    return out


# --------------------------------------------------------------- matching

def resolve_vendor(inv: InvoiceData, vendors: list[dict]) -> tuple[dict | None, list[dict]]:
    tid = norm_tax_id(inv.vendor_tax_id)
    if tid:
        for v in vendors:
            if v["tax_id"] and norm_tax_id(v["tax_id"]) == tid:
                return {"id": v["id"], "name": v["name"], "method": "tax id", "score": 100}, []
    target = norm_name(inv.vendor_name)
    best, best_score = None, 0.0
    for v in vendors:
        for candidate in [v["name"], *v["aliases"]]:
            score = fuzz.token_sort_ratio(target, norm_name(candidate))
            if score > best_score:
                best, best_score = v, score
    if best and best_score >= settings.vendor_match_threshold:
        method = "exact name" if best_score == 100 else "fuzzy name"
        notes = [] if best_score == 100 else [issue("VENDOR_NAME_DIFFERS", INFO,
                 f"'{inv.vendor_name}' matched to approved vendor '{best['name']}' (similarity {best_score:.0f}%).")]
        return {"id": best["id"], "name": best["name"], "method": method, "score": round(best_score)}, notes
    hint = f" Closest: {best['name']} ({best_score:.0f}%)." if best else ""
    return None, [issue("UNKNOWN_VENDOR", ERROR, f"'{inv.vendor_name}' is not an approved vendor.{hint}", "vendor_name")]


def _match_lines(inv: InvoiceData, po: dict) -> tuple[list[dict], list[dict]]:
    issues, rows, used = [], [], set()
    for i, li in enumerate(inv.line_items):
        cand = None
        if li.sku:
            cand = next((pl for pl in po["lines"] if pl["sku"] and norm_invoice_number(pl["sku"]) == norm_invoice_number(li.sku)
                         and pl["line_no"] not in used), None)
        if cand is None:
            scored = [(fuzz.token_set_ratio(li.description.lower(), pl["description"].lower()), pl)
                      for pl in po["lines"] if pl["line_no"] not in used]
            scored.sort(key=lambda x: -x[0])
            if scored and scored[0][0] >= 80:
                cand = scored[0][1]
        if cand is None:
            issues.append(issue("LINE_NOT_ON_PO", ERROR, f"Line {i + 1} ({li.description[:40]}) is not on {po['number']}.",
                                f"line_items.{i}"))
            rows.append({"line": i, "po_line_no": None})
            continue
        used.add(cand["line_no"])
        remaining = cand["quantity"] - cand["invoiced_qty"]
        row = {"line": i, "po_line_no": cand["line_no"], "po_description": cand["description"],
               "po_qty_remaining": remaining, "po_unit_price": cand["unit_price"], "status": "ok"}
        if li.quantity is not None and li.quantity > remaining + 1e-9:
            issues.append(issue("QTY_OVER_PO", ERROR,
                                f"Line {i + 1}: {li.quantity:g} billed but only {remaining:g} left on {po['number']} "
                                f"(ordered {cand['quantity']:g}).", f"line_items.{i}"))
            row["status"] = "qty over PO"
        if li.unit_price is not None and li.unit_price > cand["unit_price"] * (1 + settings.price_tolerance):
            pct = (li.unit_price / cand["unit_price"] - 1) * 100
            issues.append(issue("PRICE_VARIANCE", ERROR,
                                f"Line {i + 1}: billed {li.unit_price:,.2f} per unit, PO price is {cand['unit_price']:,.2f} "
                                f"(+{pct:.1f}%, tolerance {settings.price_tolerance * 100:g}%).", f"line_items.{i}"))
            row["status"] = "price above PO"
        rows.append(row)
    return rows, issues


def match(inv: InvoiceData, db: DB, sha: str, exclude_id: int | None = None) -> tuple[dict, list[dict]]:
    result: dict = {"vendor": None, "po": None, "po_method": None, "lines": [], "duplicate_of": None}
    vendor, issues = resolve_vendor(inv, db.vendors())
    result["vendor"] = vendor

    dups = db.find_duplicates(sha, vendor["id"] if vendor else None, norm_invoice_number(inv.invoice_number), exclude_id)
    if dups:
        result["duplicate_of"] = dups[0]
        return result, issues + [issue("DUPLICATE", ERROR,
                                       f"Already received as invoice #{dups[0]['id']} ({dups[0]['reason']}).")]
    if not vendor:
        if not inv.po_number:
            issues.append(issue("NO_PO", ERROR, "No purchase order number on the invoice.", "po_number"))
        return result, issues

    po = None
    if inv.po_number:
        number = norm_po(inv.po_number)
        po = db.purchase_order(number) if number else None
        if not po:
            return result, issues + [issue("PO_NOT_FOUND", ERROR, f"PO '{inv.po_number}' does not exist.", "po_number")]
        result["po_method"] = "printed on invoice"
        if po["vendor_id"] != vendor["id"]:
            issues.append(issue("PO_VENDOR_MISMATCH", ERROR, f"{po['number']} belongs to another vendor.", "po_number"))
    else:
        # No PO printed: look for exactly one open PO of this vendor that the invoice fits.
        fits = []
        for cand in db.open_pos_for_vendor(vendor["id"]):
            remaining = sum((l["quantity"] - l["invoiced_qty"]) * l["unit_price"] for l in cand["lines"])
            if inv.subtotal is not None and abs(remaining - inv.subtotal) <= max(1.0, 0.01 * remaining):
                fits.append(cand)
        if len(fits) == 1:
            po = fits[0]
            result["po_method"] = "inferred from vendor and amount"
            issues.append(issue("PO_INFERRED", WARNING,
                                f"No PO number printed; {po['number']} was matched by vendor and amount. Please confirm.",
                                "po_number"))
        else:
            issues.append(issue("NO_PO", ERROR, "No purchase order number on the invoice, and no single open PO fits it.",
                                "po_number"))
            return result, issues

    result["po"] = {"number": po["number"], "status": po["status"], "currency": po["currency"]}
    if po["status"] != "open":
        issues.append(issue("PO_CLOSED", ERROR, f"{po['number']} is {po['status']}; nothing more can be billed against it.",
                            "po_number"))
        return result, issues
    if inv.currency and inv.currency.upper() != po["currency"]:
        issues.append(issue("CURRENCY_MISMATCH", ERROR,
                            f"Invoice is in {inv.currency}, {po['number']} is in {po['currency']}.", "currency"))

    rows, line_issues = _match_lines(inv, po)
    result["lines"] = rows
    issues += line_issues
    remaining_value = round(sum((l["quantity"] - l["invoiced_qty"]) * l["unit_price"] for l in po["lines"]), 2)
    result["po"]["remaining_value"] = remaining_value
    explained = {i["code"] for i in line_issues} & {"QTY_OVER_PO", "PRICE_VARIANCE", "LINE_NOT_ON_PO"}
    if inv.subtotal is not None and not explained and inv.subtotal > remaining_value * (1 + settings.price_tolerance) + 0.01:
        issues.append(issue("TOTAL_VS_PO", ERROR,
                            f"Subtotal {inv.subtotal:,.2f} is more than what is left on {po['number']} "
                            f"({remaining_value:,.2f}).", "subtotal"))
    return result, issues


# --------------------------------------------------------------- decision

def decide(issues: list[dict]) -> str:
    codes = {i["code"] for i in issues if i["severity"] != INFO}
    if "DUPLICATE" in codes:
        return "rejected"
    return "needs_review" if codes else "auto_approved"


def ledger_entries(inv: InvoiceData, m: dict) -> list[tuple[str, int, float, float]]:
    if not m.get("po"):
        return []
    out = []
    for row in m.get("lines", []):
        if row.get("po_line_no") is None:
            continue
        li = inv.line_items[row["line"]]
        out.append((m["po"]["number"], row["po_line_no"], li.quantity or 0, li.amount or 0))
    return out
