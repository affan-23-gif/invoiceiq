import io
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import checks
from app.extract import ExtractionError, RegexExtractor, parse_amount
from app.schema import InvoiceData, LineItem
from tests.conftest import SAMPLES, sample_bytes

CASES = [json.loads(p.read_text()) for p in sorted(SAMPLES.glob("c*.json"))]


def codes(inv):
    return sorted({i["code"] for i in inv["issues"] if i["severity"] != "info"})


# ------------------------------------------------------- normalisation

@pytest.mark.parametrize("raw,expected", [
    ("PO-1046", "PO-1046"), ("P.O. #1046", "PO-1046"), ("po 1046", "PO-1046"), ("Order 1046", "PO-1046"), (None, None)])
def test_po_numbers_are_normalised(raw, expected):
    assert checks.norm_po(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("$1,240.00", 1240.0), ("2.400,00 €", 2400.0), ("3.213,00", 3213.0), ("85.00", 85.0), ("1,021.00", 1021.0)])
def test_amounts_parse_in_us_and_eu_formats(raw, expected):
    assert parse_amount(raw) == expected


def test_vendor_matching_by_tax_id_fuzzy_name_and_unknown(pipe):
    vendors = pipe.db.vendors()
    v, notes = checks.resolve_vendor(InvoiceData(vendor_name="Whatever", vendor_tax_id="de 318 274 551"), vendors)
    assert v["id"] == "V-04" and v["method"] == "tax id"
    v, notes = checks.resolve_vendor(InvoiceData(vendor_name="Harbour Office Supplies"), vendors)
    assert v["id"] == "V-01" and v["method"] == "fuzzy name" and notes[0]["code"] == "VENDOR_NAME_DIFFERS"
    v, notes = checks.resolve_vendor(InvoiceData(vendor_name="Sunrise Bakery"), vendors)
    assert v is None and notes[0]["code"] == "UNKNOWN_VENDOR"


# ------------------------------------------------------------ validation

def test_math_checks_use_printed_numbers():
    inv = InvoiceData(vendor_name="x", invoice_number="1", invoice_date="2026-11-01", currency="USD",
                      line_items=[LineItem(description="a", quantity=2, unit_price=10, amount=25)],
                      subtotal=30, tax=1, total=40)
    got = {i["code"] for i in checks.validate(inv, "", today=date(2026, 11, 20))}
    assert got == {"MATH_LINE", "MATH_SUBTOTAL", "MATH_TOTAL"}


def test_values_missing_from_pdf_text_are_flagged():
    inv = InvoiceData(vendor_name="x", invoice_number="INV-9", invoice_date="2026-11-01", currency="USD",
                      subtotal=10, tax=0, total=10)
    got = [i for i in checks.validate(inv, "Invoice INV-7 Total $12.00") if i["code"] == "UNVERIFIED_TEXT"]
    assert {i["field"] for i in got} == {"invoice_number", "total"}
    assert not [i for i in checks.validate(inv, "Invoice INV-9 Total $10.00") if i["code"] == "UNVERIFIED_TEXT"]


def test_missing_fields_and_bad_dates():
    inv = InvoiceData(invoice_date="2026-02-30", due_date="2026-01-01")
    got = {i["code"] for i in checks.validate(inv, "")}
    assert {"MISSING_FIELD", "DATE_INVALID"} <= got


# ---------------------------------------- full pipeline on every sample

@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_every_sample_gets_the_expected_decision(pipe, case):
    for req in case["requires"]:
        pipe.process(*sample_bytes(req))
    inv = pipe.process(*sample_bytes(case["id"]))
    assert inv["status"] == case["expect"]["decision"]
    assert codes(inv) == case["expect"]["issues"]


def test_auto_approved_invoices_consume_the_po(pipe):
    assert pipe.process(*sample_bytes("c13"))["status"] == "auto_approved"   # bills all of PO-1048
    inv = pipe.process(*sample_bytes("c06"))                                   # bills PO-1048 again
    assert "QTY_OVER_PO" in codes(inv)


def test_duplicate_detected_by_vendor_and_number_even_for_a_different_file(pipe):
    pipe.process(*sample_bytes("c01"))
    inv = pipe.process(*sample_bytes("c09"))   # same invoice, scanned
    assert inv["status"] == "rejected" and inv["match"]["duplicate_of"]["reason"] == "same vendor and invoice number"


def test_reviewer_can_fix_fields_and_approve(pipe):
    inv = pipe.process(*sample_bytes("c08"))           # printed total is $100 too high
    assert codes(inv) == ["MATH_TOTAL"]
    data = inv["data"]
    data["total"] = round(data["subtotal"] + data["tax"], 2)
    fixed = pipe.update_fields(inv["id"], data)
    assert "MATH_TOTAL" not in codes(fixed) and fixed["edited"] == 1
    done = pipe.decide(inv["id"], True, "alice", "Vendor confirmed the total")
    assert done["status"] == "approved"
    with pipe.db.lock:
        assert pipe.db.conn.execute("SELECT COUNT(*) FROM ledger WHERE invoice_id = ?", (inv["id"],)).fetchone()[0] == 2


def test_cannot_approve_unknown_vendor_or_decide_twice(pipe):
    inv = pipe.process(*sample_bytes("c10"))
    with pytest.raises(ValueError, match="UNKNOWN_VENDOR"):
        pipe.decide(inv["id"], True, "bob", "")
    pipe.decide(inv["id"], False, "bob", "Not our vendor")
    with pytest.raises(ValueError):
        pipe.decide(inv["id"], False, "bob", "again")


def test_extraction_failure_is_recorded_not_raised(pipe):
    class Broken:
        name = "broken"

        def extract(self, content, mime):
            raise ExtractionError("model unavailable")
    pipe.extractor = Broken()
    inv = pipe.process(*sample_bytes("c01"))
    assert inv["status"] == "failed" and inv["issues"][0]["code"] == "EXTRACTION_FAILED"


def test_regex_baseline_cannot_read_scans_or_line_items():
    pdf, _ = sample_bytes("c01")
    data = RegexExtractor().extract(pdf, "application/pdf").data
    assert data.invoice_number == "HOS-20931" and data.line_items == []
    img, _ = sample_bytes("c12")
    assert RegexExtractor().extract(img, "image/jpeg").data.invoice_number is None


# ------------------------------------------------------------------ API

def test_api_end_to_end():
    from app.main import app
    with TestClient(app) as client:
        client.post("/api/reset")
        assert client.get("/api/health").json()["extractor"] == "fake"
        assert len(client.get("/api/samples").json()) == 18
        bad = client.post("/api/invoices", files={"file": ("x.pdf", io.BytesIO(b"not a pdf"), "application/pdf")})
        assert bad.status_code == 400
        pdf, name = sample_bytes("c06")
        inv = client.post("/api/invoices", files={"file": (name, io.BytesIO(pdf), "application/pdf")}).json()
        assert inv["status"] == "needs_review" and "stored_name" not in inv
        f = client.get(inv["file_url"])
        assert f.status_code == 200 and f.headers["content-type"] == "application/pdf"
        r = client.post(f"/api/invoices/{inv['id']}/decision", json={"approve": True, "note": "price agreed by phone"})
        assert r.json()["status"] == "approved"
        assert client.post(f"/api/invoices/{inv['id']}/decision", json={"approve": True}).status_code == 409
        assert client.post("/api/samples/c09/process").json()["status"] == "rejected"   # pulls in c01 first
        csv_text = client.get("/api/export.csv").text
        assert "HOS-21007" in csv_text and "HOS-20931" in csv_text
        assert client.get("/api/stats").json()["total"] == 3
        assert client.post("/api/samples/..%2Fetc/process").status_code == 404
        assert client.post("/api/reset").json()["ok"] and client.get("/api/invoices").json() == []
