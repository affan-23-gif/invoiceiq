"""FastAPI app: review console + JSON API (also usable from n8n / Zapier / scripts)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import ROOT, settings
from app.db import DB
from app.extract import ExtractionError, make_extractor
from app.pipeline import Pipeline, UploadError

log = logging.getLogger("invoiceiq")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class RateLimiter:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window, self.hits = limit, window_s, defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


upload_limiter = RateLimiter(settings.upload_rate_limit, 600)
reset_limiter = RateLimiter(3, 600)


def _reset_uploads() -> None:
    shutil.rmtree(settings.upload_dir, ignore_errors=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = DB(settings.db_path)
    if os.environ.get("DEMO_RESET_ON_START", "false").lower() == "true":
        db.seed_reference()
        _reset_uploads()
    try:
        extractor, err = make_extractor(), None
    except ExtractionError as exc:
        extractor, err = None, str(exc)
        log.warning("Extractor not configured: %s", exc)
    app.state.db = db
    app.state.pipeline = Pipeline(db, extractor) if extractor else None
    app.state.extractor_error = err
    yield
    db.close()


app = FastAPI(title="InvoiceIQ", lifespan=lifespan, docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def headers(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if not request.url.path.endswith("/file"):
        response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/"):
        log.info("%s %s %s %.0fms", request.method, request.url.path, response.status_code, (time.perf_counter() - t0) * 1000)
    return response


def _client(request: Request) -> str:
    if settings.trust_proxy and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _pipeline(request: Request) -> Pipeline:
    p = request.app.state.pipeline
    if p is None:
        raise HTTPException(503, request.app.state.extractor_error or "Extraction is not configured.")
    return p


def _check_rate(request: Request, api_key: str | None) -> None:
    if settings.ingest_api_key and api_key and secrets.compare_digest(api_key, settings.ingest_api_key):
        return  # trusted integration (n8n, scripts)
    if not upload_limiter.allow(_client(request)):
        raise HTTPException(429, "Too many invoices from your address. Please wait a few minutes.")


def _public(inv: dict | None) -> dict:
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    inv = dict(inv)
    inv.pop("stored_name", None)
    inv["file_url"] = f"/api/invoices/{inv['id']}/file"
    return inv


# ----------------------------------------------------------------- models

class FieldsIn(BaseModel):
    data: dict


class DecisionIn(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=500)
    reviewer: str = Field(default="reviewer", max_length=60)


# ----------------------------------------------------------------- routes

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health(request: Request):
    p = request.app.state.pipeline
    return {"ok": True, "extractor": p.extractor.name if p else None, "error": request.app.state.extractor_error}


@app.get("/api/samples")
def samples():
    out = []
    for meta in sorted(settings.samples_dir.glob("c*.json")):
        info = json.loads(meta.read_text())
        out.append({"id": info["id"], "file": info["file"], "kind": info["kind"], "note": info["note"],
                    "vendor": info["truth"]["vendor_name"], "expected": info["expect"]["decision"]})
    return out


@app.post("/api/samples/{sample_id}/process")
async def process_sample(sample_id: str, request: Request):
    p = _pipeline(request)
    _check_rate(request, None)
    meta = settings.samples_dir / f"{sample_id}.json"
    if not sample_id.isalnum() or not meta.exists():
        raise HTTPException(404, "Unknown sample.")
    info = json.loads(meta.read_text())
    # A duplicate needs its original first.
    for req in info["requires"]:
        r = json.loads((settings.samples_dir / f"{req}.json").read_text())
        content = (settings.samples_dir / r["file"]).read_bytes()
        if not request.app.state.db.find_duplicates(hashlib.sha256(content).hexdigest(), None, None):
            await run_in_threadpool(p.process, content, r["file"])
    content = (settings.samples_dir / info["file"]).read_bytes()
    return _public(await run_in_threadpool(p.process, content, info["file"]))


@app.post("/api/invoices")
async def upload(request: Request, file: UploadFile = File(...), x_api_key: str | None = Header(default=None)):
    p = _pipeline(request)
    _check_rate(request, x_api_key)
    content = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    try:
        return _public(await run_in_threadpool(p.process, content, file.filename or "upload"))
    except UploadError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/invoices")
def list_invoices(request: Request):
    return request.app.state.db.list_invoices()


@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: int, request: Request):
    return _public(request.app.state.db.get_invoice(invoice_id))


@app.get("/api/invoices/{invoice_id}/file")
def invoice_file(invoice_id: int, request: Request):
    inv = request.app.state.db.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    path = settings.upload_dir / inv["stored_name"]
    if not path.exists():
        raise HTTPException(410, "The file was removed (demo data is reset regularly).")
    return Response(path.read_bytes(), media_type=inv["mime"], headers={
        "Content-Disposition": "inline", "Cache-Control": "private, max-age=3600"})


@app.put("/api/invoices/{invoice_id}")
def update_invoice(invoice_id: int, body: FieldsIn, request: Request):
    try:
        return _public(_pipeline(request).update_fields(invoice_id, body.data))
    except KeyError:
        raise HTTPException(404, "Invoice not found.") from None
    except ValueError as exc:
        raise HTTPException(409 if "can no longer" in str(exc) else 422, str(exc)) from None


@app.post("/api/invoices/{invoice_id}/decision")
def decide(invoice_id: int, body: DecisionIn, request: Request):
    try:
        return _public(_pipeline(request).decide(invoice_id, body.approve, body.reviewer, body.note))
    except KeyError:
        raise HTTPException(404, "Invoice not found.") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/api/purchase-orders")
def purchase_orders(request: Request):
    db: DB = request.app.state.db
    with db.lock:
        numbers = [r[0] for r in db.conn.execute("SELECT number FROM purchase_orders ORDER BY number")]
    vendors = {v["id"]: v["name"] for v in db.vendors()}
    out = []
    for n in numbers:
        po = db.purchase_order(n)
        total = sum(l["quantity"] * l["unit_price"] for l in po["lines"])
        billed = sum(l["invoiced_qty"] * l["unit_price"] for l in po["lines"])
        out.append({"number": n, "vendor": vendors.get(po["vendor_id"]), "status": po["status"],
                    "currency": po["currency"], "total": round(total, 2), "billed": round(billed, 2)})
    return out


@app.get("/api/stats")
def stats(request: Request):
    db: DB = request.app.state.db
    with db.lock:
        rows = db.conn.execute("SELECT status, COUNT(*), SUM(cost_usd), AVG(extract_ms) FROM invoices GROUP BY status").fetchall()
    by = {r[0]: r[1] for r in rows}
    n = sum(by.values())
    return {"total": n, "by_status": by, "cost_usd": round(sum(r[2] or 0 for r in rows), 5),
            "auto_rate": round(by.get("auto_approved", 0) / n, 3) if n else None}


@app.get("/api/export.csv")
def export_csv(request: Request):
    db: DB = request.app.state.db
    with db.lock:
        rows = db.conn.execute(
            "SELECT id, status, data, match, decided_at FROM invoices WHERE status IN ('approved','auto_approved') ORDER BY id"
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["invoice_id", "status", "vendor_id", "vendor", "invoice_number", "invoice_date", "due_date", "po_number",
                "currency", "subtotal", "tax", "total", "approved_at"])
    for r in rows:
        d, m = json.loads(r["data"]), json.loads(r["match"])
        w.writerow([r["id"], r["status"], (m.get("vendor") or {}).get("id"), (m.get("vendor") or {}).get("name"),
                    d.get("invoice_number"), d.get("invoice_date"), d.get("due_date"), (m.get("po") or {}).get("number"),
                    d.get("currency"), d.get("subtotal"), d.get("tax"), d.get("total"), r["decided_at"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="approved_invoices.csv"'})


@app.post("/api/reset")
def reset(request: Request):
    if not reset_limiter.allow(_client(request)):
        raise HTTPException(429, "Reset limit reached. Try again later.")
    request.app.state.db.seed_reference()
    _reset_uploads()
    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Internal error."}, status_code=500)
