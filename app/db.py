"""SQLite storage: reference data (vendors, POs), processed invoices and the
ledger of approved invoice lines (which is how PO consumption is tracked)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app import reference

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, tax_id TEXT, currency TEXT NOT NULL, aliases TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    number TEXT PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendors(id), status TEXT NOT NULL, currency TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS po_lines (
    po_number TEXT NOT NULL REFERENCES purchase_orders(number), line_no INTEGER NOT NULL,
    sku TEXT, description TEXT NOT NULL, quantity REAL NOT NULL, unit_price REAL NOT NULL,
    PRIMARY KEY (po_number, line_no)
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT NOT NULL, stored_name TEXT NOT NULL, mime TEXT NOT NULL,
    sha256 TEXT NOT NULL, status TEXT NOT NULL, extractor TEXT NOT NULL,
    data TEXT NOT NULL, original_data TEXT NOT NULL, issues TEXT NOT NULL, match TEXT NOT NULL,
    has_text_layer INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0, extract_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT, note TEXT NOT NULL DEFAULT '', edited INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS invoices_sha ON invoices(sha256);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    po_number TEXT NOT NULL, po_line_no INTEGER NOT NULL, quantity REAL NOT NULL, amount REAL NOT NULL
);
"""

# Invoice statuses: auto_approved, needs_review, approved, rejected, failed


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        if not self.conn.execute("SELECT 1 FROM vendors LIMIT 1").fetchone():
            self.seed_reference()

    def seed_reference(self) -> None:
        with self.lock:
            c = self.conn
            c.execute("DELETE FROM ledger"); c.execute("DELETE FROM invoices")
            c.execute("DELETE FROM po_lines"); c.execute("DELETE FROM purchase_orders"); c.execute("DELETE FROM vendors")
            c.executemany("INSERT INTO vendors VALUES (?,?,?,?,?)",
                          [(v[0], v[1], v[2], v[3], json.dumps(v[4])) for v in reference.VENDORS])
            for number, vendor, status, cur, lines in reference.PURCHASE_ORDERS:
                c.execute("INSERT INTO purchase_orders VALUES (?,?,?,?)", (number, vendor, status, cur))
                c.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?)",
                              [(number, i + 1, sku, desc, qty, price) for i, (sku, desc, qty, price) in enumerate(lines)])
            c.commit()

    # ------------------------------------------------------------ reference
    def vendors(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM vendors").fetchall()
        return [{**dict(r), "aliases": json.loads(r["aliases"])} for r in rows]

    def purchase_order(self, number: str) -> dict | None:
        with self.lock:
            po = self.conn.execute("SELECT * FROM purchase_orders WHERE number = ?", (number,)).fetchone()
            if not po:
                return None
            lines = self.conn.execute(
                """SELECT l.*, COALESCE((SELECT SUM(quantity) FROM ledger g
                                         WHERE g.po_number = l.po_number AND g.po_line_no = l.line_no), 0) AS invoiced_qty
                   FROM po_lines l WHERE po_number = ? ORDER BY line_no""", (number,)).fetchall()
        return {**dict(po), "lines": [dict(r) for r in lines]}

    def open_pos_for_vendor(self, vendor_id: str) -> list[dict]:
        with self.lock:
            nums = [r[0] for r in self.conn.execute(
                "SELECT number FROM purchase_orders WHERE vendor_id = ? AND status = 'open'", (vendor_id,))]
        return [self.purchase_order(n) for n in nums]

    # ------------------------------------------------------------- invoices
    def find_duplicates(self, sha: str, vendor_id: str | None, invoice_number_norm: str | None,
                        exclude_id: int | None = None) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, sha256, status, data, match FROM invoices WHERE status != 'rejected' AND status != 'failed' "
                "AND id != ?", (exclude_id or -1,)).fetchall()
        out = []
        for r in rows:
            if r["sha256"] == sha:
                out.append({"id": r["id"], "reason": "identical file"})
                continue
            m, d = json.loads(r["match"]), json.loads(r["data"])
            from app.checks import norm_invoice_number  # local import avoids a cycle
            if vendor_id and invoice_number_norm and m.get("vendor", {}).get("id") == vendor_id \
                    and norm_invoice_number(d.get("invoice_number")) == invoice_number_norm:
                out.append({"id": r["id"], "reason": "same vendor and invoice number"})
        return out

    def insert_invoice(self, **f) -> int:
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO invoices (file_name, stored_name, mime, sha256, status, extractor, data, original_data,
                   issues, match, has_text_layer, tokens_in, tokens_out, cost_usd, extract_ms, created_at, decided_at, decided_by)
                   VALUES (:file_name, :stored_name, :mime, :sha256, :status, :extractor, :data, :data, :issues, :match,
                   :has_text_layer, :tokens_in, :tokens_out, :cost_usd, :extract_ms, :created_at, :decided_at, :decided_by)""",
                {**f, "data": json.dumps(f["data"]), "issues": json.dumps(f["issues"]), "match": json.dumps(f["match"])})
            self.conn.commit()
            return cur.lastrowid

    def update_invoice(self, invoice_id: int, **f) -> None:
        sets, vals = [], []
        for k, v in f.items():
            sets.append(f"{k} = ?")
            vals.append(json.dumps(v) if k in {"data", "issues", "match"} else v)
        with self.lock:
            self.conn.execute(f"UPDATE invoices SET {', '.join(sets)} WHERE id = ?", (*vals, invoice_id))
            self.conn.commit()

    def get_invoice(self, invoice_id: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("data", "original_data", "issues", "match"):
            d[k] = json.loads(d[k])
        return d

    def list_invoices(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, file_name, status, data, issues, created_at, cost_usd FROM invoices ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["data"])
            out.append({"id": r["id"], "file_name": r["file_name"], "status": r["status"], "created_at": r["created_at"],
                        "vendor_name": d.get("vendor_name"), "invoice_number": d.get("invoice_number"),
                        "total": d.get("total"), "currency": d.get("currency"),
                        "issue_count": len([i for i in json.loads(r["issues"]) if i["severity"] != "info"]),
                        "cost_usd": r["cost_usd"]})
        return out

    def post_to_ledger(self, invoice_id: int, entries: list[tuple[str, int, float, float]]) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM ledger WHERE invoice_id = ?", (invoice_id,))
            self.conn.executemany("INSERT INTO ledger (invoice_id, po_number, po_line_no, quantity, amount) VALUES (?,?,?,?,?)",
                                  [(invoice_id, *e) for e in entries])
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()
