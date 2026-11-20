"""Reference data for the demo buyer, Brightline Supply: approved vendors and
purchase orders. In a real deployment this comes from the ERP (NetSuite, Xero,
QuickBooks, SAP...) and these tables become a sync or an API call."""
from __future__ import annotations

BUYER = {"name": "Brightline Supply Inc.", "address": "410 Commerce Way, Portland, OR 97209"}

# id, legal name, tax id, currency, known aliases
VENDORS = [
    ("V-01", "Harbor Office Supply LLC", "US-41-2290871", "USD", ["Harbor Office Supply", "Harbor Office"]),
    ("V-02", "Kestrel Logistics Inc.", "US-88-1034456", "USD", ["Kestrel Logistics", "Kestrel Freight"]),
    ("V-03", "Maple & Finch Packaging Ltd", "US-52-6610934", "USD", ["Maple and Finch Packaging", "Maple & Finch"]),
    ("V-04", "Cobalt IT Services GmbH", "DE318274551", "EUR", ["Cobalt IT", "Cobalt IT-Services"]),
    ("V-05", "Sunrise Coffee Roasters", "US-27-5519034", "USD", ["Sunrise Roasters"]),
    ("V-06", "Tidewater Cleaning Co.", "US-63-7720118", "USD", ["Tidewater Cleaning"]),
]

_PACKAGING = [
    ("MFP-BX-1010", "Shipping box 10x10x10", 1.02), ("MFP-BX-1208", "Shipping box 12x12x8", 1.09),
    ("MFP-BX-1612", "Shipping box 16x12x12", 1.21), ("MFP-BX-2016", "Shipping box 20x16x16", 1.64),
    ("MFP-BX-2424", "Shipping box 24x24x24", 2.38), ("MFP-ML-0609", "Poly mailer 6x9", 0.14),
    ("MFP-ML-1013", "Poly mailer 10x13", 0.19), ("MFP-ML-1419", "Poly mailer 14x19", 0.27),
    ("MFP-BM-0610", "Bubble mailer 6x10", 0.36), ("MFP-BM-0915", "Bubble mailer 9x15", 0.48),
    ("MFP-TP-48C", "Packing tape 48mm clear", 3.40), ("MFP-TP-48B", "Packing tape 48mm brown", 3.40),
    ("MFP-TP-72C", "Packing tape 72mm clear", 4.95), ("MFP-DS-TAPE", "Tape dispenser", 11.90),
    ("MFP-VF-10", "Void fill paper roll", 22.00), ("MFP-BW-12", "Bubble wrap 12in roll", 18.60),
    ("MFP-BW-24", "Bubble wrap 24in roll", 31.20), ("MFP-LB-4X6", "Shipping labels 4x6 (500)", 16.75),
    ("MFP-ST-FR", "Fragile stickers (500)", 7.80), ("MFP-ST-UP", "This side up stickers (500)", 7.80),
    ("MFP-CN-08", "Corner protectors (100)", 12.40), ("MFP-SF-18", "Stretch film 18in", 24.90),
    ("MFP-SF-HD", "Stretch film handle", 6.25), ("MFP-PL-48", "Pallet wrap 48in", 41.00),
]

# po number, vendor id, status, currency, [(sku, description, qty, unit_price)]
PURCHASE_ORDERS = [
    ("PO-1042", "V-01", "open", "USD", [
        ("HOS-PAP-A4", "A4 copy paper, 5-ream box", 40, 6.50),
        ("HOS-TON-58", "Toner cartridge 58A", 6, 89.00),
        ("HOS-ORG-3", "Desk organizer, 3 tier", 10, 14.25)]),
    ("PO-1043", "V-02", "open", "USD", [
        ("KL-LTL-CHI-DEN", "LTL freight Chicago to Denver", 1, 1240.00),
        ("KL-LIFT", "Liftgate delivery fee", 1, 85.00),
        ("KL-FUEL", "Fuel surcharge", 1, 96.20)]),
    ("PO-1044", "V-03", "open", "USD", [
        ("MFP-BX-1812", "Shipping box 18x12x12", 500, 1.18),
        ("MFP-TP-48C", "Packing tape 48mm clear", 48, 3.40),
        ("MFP-VF-10", "Void fill paper roll", 20, 22.00)]),
    ("PO-1045", "V-04", "open", "EUR", [
        ("CIT-MSP-OCT", "Managed IT support, October", 1, 2400.00),
        ("CIT-SETUP", "Laptop setup and imaging", 4, 75.00)]),
    ("PO-1046", "V-05", "open", "USD", [
        ("SCR-ESP-1", "Espresso blend beans 1kg", 30, 24.50),
        ("SCR-DEC-1", "Decaf beans 1kg", 10, 26.00)]),
    ("PO-1047", "V-06", "closed", "USD", [
        ("TW-OFF-OCT", "Office cleaning, October", 1, 1150.00)]),
    ("PO-1048", "V-01", "open", "USD", [
        ("HOS-CHR-ERG", "Ergonomic office chair", 8, 245.00),
        ("HOS-ARM-2", "Dual monitor arm", 8, 64.00)]),
    ("PO-1049", "V-03", "open", "USD", [(sku, desc, 100 if price < 1 else 20, price) for sku, desc, price in _PACKAGING]),
]
