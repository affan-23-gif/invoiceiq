"""Settings from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0]
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "gemini"))  # gemini | fake
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY", ""))
    extract_model: str = field(default_factory=lambda: _env("EXTRACT_MODEL", "gemini-3.5-flash-lite"))
    gemini_rpm: int = field(default_factory=lambda: int(_env("GEMINI_RPM", "12")))
    price_in_per_m: float = field(default_factory=lambda: float(_env("PRICE_IN_PER_M", "0.10")))
    price_out_per_m: float = field(default_factory=lambda: float(_env("PRICE_OUT_PER_M", "0.40")))

    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", str(ROOT / "data" / "invoiceiq.db"))))
    upload_dir: Path = field(default_factory=lambda: Path(_env("UPLOAD_DIR", str(ROOT / "data" / "uploads"))))
    samples_dir: Path = ROOT / "samples"

    # Business tolerances
    price_tolerance: float = field(default_factory=lambda: float(_env("PRICE_TOLERANCE", "0.02")))   # 2 %
    amount_tolerance: float = 0.02                                                                      # cents
    vendor_match_threshold: float = field(default_factory=lambda: float(_env("VENDOR_MATCH_THRESHOLD", "85")))

    # Web
    max_upload_mb: int = field(default_factory=lambda: int(_env("MAX_UPLOAD_MB", "8")))
    upload_rate_limit: int = field(default_factory=lambda: int(_env("UPLOAD_RATE_LIMIT", "20")))  # per IP per 10 min
    ingest_api_key: str = field(default_factory=lambda: _env("INGEST_API_KEY", ""))   # for n8n / scripts
    webhook_url: str = field(default_factory=lambda: _env("APPROVAL_WEBHOOK_URL", ""))
    trust_proxy: bool = field(default_factory=lambda: _env("TRUST_PROXY", "false").lower() == "true")


settings = Settings()
