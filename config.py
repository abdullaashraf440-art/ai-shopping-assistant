"""Central configuration. Everything deploy-specific is driven by environment variables."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Database location. Point STORE_DB_PATH at a mounted volume in production
# so orders and imported catalogs survive restarts.
DB_PATH = os.getenv("STORE_DB_PATH", str(BASE_DIR / "store.db"))

# Where resized image uploads are written (the vision tool only reads from here).
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", Path(tempfile.gettempdir()) / "shopai_uploads"))

TEXT_MODEL = os.getenv("TEXT_MODEL", "qwen/qwen3-32b")
VISION_MODEL = os.getenv("VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# Safety limits for bulk data
MAX_IMPORT_ROWS = int(os.getenv("MAX_IMPORT_ROWS", "500000"))
MAX_TABLE_ROWS = int(os.getenv("MAX_TABLE_ROWS", "5000"))
MAX_CHAT_HISTORY = int(os.getenv("MAX_CHAT_HISTORY", "16"))

APP_NAME = "FreshCart AI"
CURRENCY = "$"


def ensure_dirs() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
