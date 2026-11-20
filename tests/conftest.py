import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="invoiceiq-tests-"))
os.environ["DB_PATH"] = str(_TMP / "test.db")
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["LLM_PROVIDER"] = "fake"
os.environ["UPLOAD_RATE_LIMIT"] = "1000"

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import DB  # noqa: E402
from app.extract import FakeExtractor  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

SAMPLES = settings.samples_dir


@pytest.fixture()
def pipe():
    db = DB(settings.db_path)
    db.seed_reference()
    p = Pipeline(db, FakeExtractor())
    yield p
    db.close()


def sample_bytes(cid: str) -> tuple[bytes, str]:
    import json
    info = json.loads((SAMPLES / f"{cid}.json").read_text())
    return (SAMPLES / info["file"]).read_bytes(), info["file"]
