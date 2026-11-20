"""Generate the demo/eval invoice set: PDFs in three different layouts, a few
"scanned" images, and a ground-truth JSON next to each file.

Some invoices are clean; others carry a known problem (price above the PO,
wrong total, duplicate, unknown vendor...) so the eval can check that the
pipeline catches it. Everything is fictional.

    python -m scripts.make_samples          # writes samples/
"""
from __future__ import annotations

import io
import json
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.pdfgen import canvas

from app.reference import BUYER, PURCHASE_ORDERS, VENDORS

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"

VENDOR = {v[0]: v for v in VENDORS}
PO = {p[0]: p for p in PURCHASE_ORDERS}
ADDRESSES = {
    "V-01": "88 Pier Street, Seattle, WA 98101", "V-02": "1500 Rail Yard Rd, Chicago, IL 60609",
    "V-03": "22 Mill Lane, Spokane, WA 99201", "V-04": "Lindenstrasse 14, 10969 Berlin, Germany",
    "V-05": "7 Roast House Ave, Portland, OR 97214", "V-06": "301 Bay Road, Tacoma, WA 98402",
    "X-01": "55 Party Lane, Boise, ID 83702",
}


def r2(x: float) -> float:
    return round(x + 1e-9, 2)


def money(x: float, cur: str = "USD") -> str:
    sym = {"USD": "$", "EUR": "€"}.get(cur, "")
    if cur == "EUR":  # European formatting: 1.234,56 €
        s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{s} €"
    return f"{sym}{x:,.2f}"


def fmt_date(d: date, style: str) -> str:
    return {"long": d.strftime("%d %b %Y"), "us": d.strftime("%m/%d/%Y"),
            "eu": d.strftime("%d.%m.%Y"), "iso": d.isoformat()}[style]


def build(vendor_id, number, inv_date, lines, tax_rate, *, po=None, currency="USD", name=None,
          due_days=30, tax_label="Tax"):
    items = [{"sku": s, "description": d, "quantity": q, "unit_price": p, "amount": r2(q * p)}
             for s, d, q, p in lines]
    subtotal = r2(sum(i["amount"] for i in items))
    tax = r2(subtotal * tax_rate)
    v = VENDOR.get(vendor_id)
    return {
        "vendor_name": name or (v[1] if v else ""), "vendor_tax_id": v[2] if v else None,
        "invoice_number": number, "invoice_date": inv_date.isoformat(),
        "due_date": (inv_date + timedelta(days=due_days)).isoformat(), "po_number": po,
        "currency": currency, "line_items": items, "subtotal": subtotal, "tax": tax,
        "total": r2(subtotal + tax), "_tax_rate": tax_rate, "_tax_label": tax_label, "_vendor_id": vendor_id,
    }


def po_lines(po_number, overrides=None):
    overrides = overrides or {}
    out = []
    for sku, desc, qty, price in PO[po_number][4]:
        o = overrides.get(sku, {})
        out.append((sku, desc, o.get("qty", qty), o.get("price", price)))
    return out


# ------------------------------------------------------------------ layouts

def _header_block(c, inv, x, y, labels, date_style, po_label, show_po=True):
    rows = [(labels[0], inv["invoice_number"]), (labels[1], fmt_date(date.fromisoformat(inv["invoice_date"]), date_style)),
            (labels[2], fmt_date(date.fromisoformat(inv["due_date"]), date_style))]
    if show_po and inv.get("_po_printed"):
        rows.append((po_label, inv["_po_printed"]))
    for k, v in rows:
        c.setFont("Helvetica-Bold", 9); c.drawString(x, y, k)
        c.setFont("Helvetica", 9); c.drawString(x + 78, y, v)
        y -= 14
    return y


def classic(c, inv, page_w, page_h):
    """Grey table, labels: Invoice No / Invoice Date / Due Date / PO Number."""
    cur = inv["currency"]
    c.setFont("Helvetica-Bold", 22); c.drawString(40, page_h - 60, "INVOICE")
    c.setFont("Helvetica-Bold", 11); c.drawRightString(page_w - 40, page_h - 50, inv["vendor_name"])
    c.setFont("Helvetica", 8.5)
    c.drawRightString(page_w - 40, page_h - 63, ADDRESSES.get(inv["_vendor_id"], ""))
    if inv.get("vendor_tax_id"):
        c.drawRightString(page_w - 40, page_h - 75, f"Tax ID: {inv['vendor_tax_id']}")
    _header_block(c, inv, 40, page_h - 100, ("Invoice No:", "Invoice Date:", "Due Date:"), inv["_date_style"], "PO Number:")
    c.setFont("Helvetica-Bold", 9); c.drawString(330, page_h - 100, "Bill To:")
    c.setFont("Helvetica", 9); c.drawString(330, page_h - 114, BUYER["name"]); c.drawString(330, page_h - 128, BUYER["address"])
    cols = [40, 130, 360, 420, 500]
    heads = ["SKU", "Item", "Qty", "Unit Price", "Amount"]

    def table_head(y):
        c.setFillColor(colors.HexColor("#e6e8ee")); c.rect(36, y - 4, page_w - 72, 16, fill=1, stroke=0)
        c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 8.5)
        for x, h in zip(cols, heads):
            c.drawString(x, y, h)
        return y - 18

    y = table_head(page_h - 180)
    c.setFont("Helvetica", 8.5)
    for i, it in enumerate(inv["line_items"]):
        if y < 120:
            c.setFont("Helvetica", 8); c.drawRightString(page_w - 40, 40, "continued on next page")
            c.showPage(); y = table_head(page_h - 60); c.setFont("Helvetica", 8.5)
        c.drawString(cols[0], y, it["sku"] or ""); c.drawString(cols[1], y, it["description"][:42])
        c.drawString(cols[2], y, f"{it['quantity']:g}"); c.drawString(cols[3], y, money(it["unit_price"], cur))
        c.drawString(cols[4], y, money(it["amount"], cur)); y -= 15
    y -= 10
    for label, val, bold in [("Subtotal", inv["_print_subtotal"], False),
                             (f"{inv['_tax_label']} ({inv['_tax_rate'] * 100:g}%)", inv["_print_tax"], False),
                             ("Total Due", inv["_print_total"], True)]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10 if bold else 9)
        c.drawString(400, y, label); c.drawRightString(page_w - 40, y, money(val, cur)); y -= 15
    c.setFont("Helvetica", 8); c.drawString(40, 50, "Payment terms: net 30. Thank you for your business.")


def modern(c, inv, page_w, page_h):
    """Big vendor name, labels: Invoice # / Issued / Due / Your ref, 'Rate' and 'Line total'."""
    cur = inv["currency"]
    c.setFillColor(colors.HexColor("#1f3a5f")); c.rect(0, page_h - 90, page_w, 90, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 20); c.drawString(40, page_h - 52, inv["vendor_name"])
    c.setFont("Helvetica", 9); c.drawString(40, page_h - 70, ADDRESSES.get(inv["_vendor_id"], ""))
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9); c.drawString(40, page_h - 120, "Bill to")
    c.setFont("Helvetica", 9); c.drawString(40, page_h - 134, BUYER["name"]); c.drawString(40, page_h - 148, BUYER["address"])
    _header_block(c, inv, 340, page_h - 120, ("Invoice #", "Issued", "Due"), inv["_date_style"], "Your ref")
    y = page_h - 200
    c.setFont("Helvetica-Bold", 9)
    for x, h in [(40, "Description"), (330, "Qty"), (390, "Rate"), (480, "Line total")]:
        c.drawString(x, y, h)
    c.line(40, y - 5, page_w - 40, y - 5); y -= 20
    c.setFont("Helvetica", 9)
    for it in inv["line_items"]:
        c.drawString(40, y, it["description"]); c.drawString(330, y, f"{it['quantity']:g}")
        c.drawString(390, y, money(it["unit_price"], cur)); c.drawString(480, y, money(it["amount"], cur)); y -= 16
    c.line(330, y + 4, page_w - 40, y + 4); y -= 12
    c.drawString(390, y, "Subtotal"); c.drawRightString(page_w - 40, y, money(inv["_print_subtotal"], cur)); y -= 14
    c.drawString(390, y, inv["_tax_label"]); c.drawRightString(page_w - 40, y, money(inv["_print_tax"], cur)); y -= 18
    c.setFont("Helvetica-Bold", 12); c.drawString(390, y, "Amount due"); c.drawRightString(page_w - 40, y, money(inv["_print_total"], cur))


def compact(c, inv, page_w, page_h):
    """European style: Inv. No. / Date / Order Ref, Code / Units / Price / Net, Net total / VAT / Gross total."""
    cur = inv["currency"]
    c.setFont("Helvetica-Bold", 13); c.drawString(40, page_h - 50, inv["vendor_name"])
    c.setFont("Helvetica", 8); c.drawString(40, page_h - 63, ADDRESSES.get(inv["_vendor_id"], ""))
    if inv.get("vendor_tax_id"):
        c.drawString(40, page_h - 74, f"VAT/Tax reg.: {inv['vendor_tax_id']}")
    c.setFont("Helvetica-Bold", 16); c.drawRightString(page_w - 40, page_h - 52, "Tax invoice")
    _header_block(c, inv, 360, page_h - 80, ("Inv. No.", "Date", "Payable by"), inv["_date_style"], "Order Ref")
    c.setFont("Helvetica", 8.5); c.drawString(40, page_h - 100, f"Customer: {BUYER['name']}, {BUYER['address']}")
    y = page_h - 160
    c.setStrokeColor(colors.HexColor("#999999"))
    c.setFont("Helvetica-Bold", 8.5)
    for x, h in [(40, "Code"), (120, "Description"), (340, "Units"), (400, "Price"), (480, "Net")]:
        c.drawString(x, y, h)
    y -= 14
    c.setFont("Helvetica", 8.5)
    for it in inv["line_items"]:
        c.line(40, y + 10, page_w - 40, y + 10)
        c.drawString(40, y, it["sku"] or ""); c.drawString(120, y, it["description"])
        c.drawString(340, y, f"{it['quantity']:g}"); c.drawString(400, y, money(it["unit_price"], cur))
        c.drawString(480, y, money(it["amount"], cur)); y -= 14
    y -= 8
    for label, val in [("Net total", inv["_print_subtotal"]), (f"{inv['_tax_label']} {inv['_tax_rate'] * 100:g}%", inv["_print_tax"]),
                       ("Gross total", inv["_print_total"])]:
        c.setFont("Helvetica-Bold" if label == "Gross total" else "Helvetica", 9)
        c.drawString(400, y, label); c.drawRightString(page_w - 40, y, money(val, cur)); y -= 14


LAYOUTS = {"classic": (classic, LETTER), "modern": (modern, LETTER), "compact": (compact, A4)}


def render_pdf(inv: dict, layout: str) -> bytes:
    fn, size = LAYOUTS[layout]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    c.setTitle(f"Invoice {inv['invoice_number']}")
    fn(c, inv, *size)
    c.showPage(); c.save()
    return buf.getvalue()


def scan(pdf: bytes, seed: int) -> bytes:
    """Make a PDF look like a phone/scanner capture: raster, slight tilt, blur, grain."""
    import pypdfium2 as pdfium

    page = pdfium.PdfDocument(pdf)[0]
    img = page.render(scale=1.6).to_pil().convert("L")
    rnd = random.Random(seed)
    img = img.rotate(rnd.uniform(-1.8, 1.8), expand=True, fillcolor=245)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img = ImageEnhance.Contrast(img).enhance(0.85)
    noise = Image.effect_noise(img.size, 18).convert("L")
    img = Image.blend(img, noise, 0.07)
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=72)
    return out.getvalue()


# ------------------------------------------------------------------- cases

D = date(2026, 11, 3)


def cases():
    c = []

    def add(cid, inv, layout, date_style, expect, issues=(), *, po_printed="=", kind="pdf", requires=(), note="",
            print_overrides=None):
        inv["_date_style"] = date_style
        inv["_po_printed"] = inv["po_number"] if po_printed == "=" else po_printed
        inv["_print_subtotal"], inv["_print_tax"], inv["_print_total"] = inv["subtotal"], inv["tax"], inv["total"]
        for k, v in (print_overrides or {}).items():
            inv[k] = v
        c.append({"id": cid, "layout": layout, "kind": kind, "inv": inv, "requires": list(requires), "note": note,
                  "expect": {"decision": expect, "issues": sorted(issues)}})

    add("c01", build("V-01", "HOS-20931", D, po_lines("PO-1042"), 0.0825, po="PO-1042"), "classic", "long",
        "auto_approved", note="Clean invoice that matches its PO.")
    add("c02", build("V-02", "KL-55120", D + timedelta(1), po_lines("PO-1043"), 0.0, po="PO-1043"), "modern", "us",
        "auto_approved", note="Freight invoice, no tax, US date format.")
    add("c03", build("V-03", "MF/2026/0871", D + timedelta(2), po_lines("PO-1044"), 0.065, po="PO-1044"),
        "compact", "eu", "auto_approved", note="European layout and date format.")
    add("c04", build("V-04", "CIT-2026-1107", D + timedelta(3), po_lines("PO-1045"), 0.19, po="PO-1045",
                     currency="EUR", tax_label="MwSt."), "compact", "eu", "auto_approved",
        note="Euro invoice with German VAT and 1.234,56 number format.")
    add("c05", build("V-05", "SCR-4410", D + timedelta(4), po_lines("PO-1046"), 0.0, po="PO-1046",
                     name="SUNRISE COFFEE ROASTERS"), "modern", "us", "needs_review", ["PO_INFERRED"],
        po_printed=None, note="No PO number printed: the PO is found from vendor and amount, so a person confirms.")
    add("c06", build("V-01", "HOS-21007", D + timedelta(5), po_lines("PO-1048", {"HOS-CHR-ERG": {"price": 259.00}}),
                     0.0825, po="PO-1048"), "classic", "long", "needs_review", ["PRICE_VARIANCE"],
        note="Chair billed at $259.00, PO says $245.00 (+5.7%).")
    add("c07", build("V-03", "MF/2026/0902", D + timedelta(6), po_lines("PO-1044", {"MFP-BX-1812": {"qty": 520}}),
                     0.065, po="PO-1044"), "compact", "eu", "needs_review", ["QTY_OVER_PO"],
        note="520 boxes billed, only 500 ordered.")
    inv = build("V-05", "SCR-4425", D + timedelta(7), po_lines("PO-1046", {"SCR-ESP-1": {"qty": 20}}), 0.0, po="PO-1046")
    add("c08", inv, "modern", "us", "needs_review", ["MATH_TOTAL"],
        print_overrides={"_print_total": r2(inv["total"] + 100)},
        note="Printed total is $100 higher than subtotal + tax.")
    add("c09", build("V-01", "HOS-20931", D, po_lines("PO-1042"), 0.0825, po="PO-1042"), "classic", "long",
        "rejected", ["DUPLICATE"], kind="image", requires=["c01"],
        note="The same invoice as c01, received again as a scan.")
    add("c10", build("X-01", "BER-118", D + timedelta(8), [("", "Folding tables (event)", 12, 18.0),
                                                           ("", "Delivery and pickup", 1, 140.0)], 0.06,
                     name="Bluejay Event Rentals"), "modern", "us", "needs_review", ["UNKNOWN_VENDOR", "NO_PO"],
        po_printed=None, note="Vendor is not in the approved vendor list.")
    add("c11", build("V-06", "TW-3391", D + timedelta(9), po_lines("PO-1047"), 0.0, po="PO-1047"), "classic", "us",
        "needs_review", ["PO_CLOSED"], note="Bills against a PO that is already closed.")
    add("c12", build("V-02", "KL-55188", D + timedelta(10), po_lines("PO-1043"), 0.0, po="PO-1043"), "modern", "us",
        "auto_approved", kind="image", note="Clean invoice, scanned at a slight angle.")
    add("c13", build("V-01", "HOS-21044", D + timedelta(11), po_lines("PO-1048"), 0.0825, po="PO-1048"), "classic",
        "long", "auto_approved", kind="image", note="Clean invoice, scanned.")
    add("c14", build("V-03", "MF/2026/0915", D + timedelta(12), po_lines("PO-1049"), 0.065, po="PO-1049"), "classic",
        "long", "auto_approved", note="Long invoice with 24 line items.")
    inv = build("V-05", "SCR-4431", D + timedelta(13), po_lines("PO-1046"), 0.0, po="PO-1046")
    inv["line_items"][1]["amount"] = r2(inv["line_items"][1]["amount"] + 26.0)   # printed line is wrong
    inv["subtotal"] = r2(sum(i["amount"] for i in inv["line_items"])); inv["total"] = r2(inv["subtotal"] + inv["tax"])
    add("c15", inv, "modern", "us", "needs_review", ["MATH_LINE", "TOTAL_VS_PO"],
        note="One line says 10 x $26.00 = $286.00.")
    add("c16", build("V-05", "SCR-4440", D + timedelta(14), po_lines("PO-1046", {"SCR-ESP-1": {"qty": 12}, "SCR-DEC-1": {"qty": 4}}),
                     0.0, po="PO-1046"), "modern", "us", "auto_approved", po_printed="P.O. #1046",
        note="PO number written as 'P.O. #1046'; partial delivery.")
    add("c17", build("V-04", "CIT-2026-1121", D + timedelta(15), po_lines("PO-1045", {"CIT-SETUP": {"qty": 2}}), 0.19,
                     po="PO-1045", currency="EUR", tax_label="MwSt.", name="Cobalt IT-Services GmbH"),
        "compact", "eu", "auto_approved", kind="image", note="Scanned euro invoice, vendor name spelled differently.")
    add("c18", build("V-01", "HOS-21102", D + timedelta(16), po_lines("PO-1042", {"HOS-PAP-A4": {"qty": 20}}), 0.0825,
                     po="PO-1042", name="Harbour Office Supplies"), "classic", "long", "auto_approved",
        note="Vendor name misspelled ('Harbour ... Supplies'); matched by fuzzy name + PO.")
    return c


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("c*"):
        f.unlink()
    for i, case in enumerate(cases()):
        inv = case["inv"]
        pdf = render_pdf(inv, case["layout"])
        if case["kind"] == "image":
            fname = f"{case['id']}.jpg"
            (OUT / fname).write_bytes(scan(pdf, seed=i))
        else:
            fname = f"{case['id']}.pdf"
            (OUT / fname).write_bytes(pdf)
        truth = {k: v for k, v in inv.items() if not k.startswith("_")}
        truth["subtotal"], truth["tax"], truth["total"] = inv["_print_subtotal"], inv["_print_tax"], inv["_print_total"]
        truth["po_number"] = inv["_po_printed"]  # what is printed on the page
        (OUT / f"{case['id']}.json").write_text(json.dumps({
            "id": case["id"], "file": fname, "layout": case["layout"], "kind": case["kind"], "note": case["note"],
            "requires": case["requires"], "expect": case["expect"], "truth": truth}, indent=2))
    print(f"wrote {len(cases())} invoices to {OUT}")


if __name__ == "__main__":
    main()
