"""
Bulk data ingestion: read CSV / TSV / Excel / JSON / JSONL, auto-detect columns,
validate every row, and load products or reviews in a single transaction.

Bad rows never abort an import. They are collected in the report with their row number
and reason so the user can fix and re-upload.
"""
from __future__ import annotations

import io
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

import db
from config import MAX_IMPORT_ROWS

# canonical field -> accepted header names (compared after normalisation)
PRODUCT_ALIASES = {
    "id": ["id", "product_id", "sku", "item_id", "productid"],
    "name": ["name", "title", "product", "product_name", "item", "item_name", "productname"],
    "price": ["price", "unit_price", "cost", "amount", "price_usd", "sale_price", "retail_price"],
    "category": ["category", "type", "department", "aisle", "product_type", "categories"],
    "description": ["description", "desc", "details", "summary", "about"],
    "is_organic": ["is_organic", "organic", "bio", "isorganic"],
    "brand": ["brand", "manufacturer", "vendor", "maker", "supplier"],
}
REVIEW_ALIASES = {
    "product_id": ["product_id", "productid", "item_id", "id", "sku"],
    "product_name": ["product_name", "product", "name", "item", "title"],
    "rating": ["rating", "stars", "score", "star_rating", "review_rating"],
    "reviewer_name": ["reviewer_name", "reviewer", "author", "user", "username", "customer"],
    "review_text": ["review_text", "review", "text", "comment", "comments", "body", "content"],
}
TRUE_WORDS = {"1", "true", "t", "yes", "y", "organic", "bio", "x", "✓"}
FALSE_WORDS = {"0", "false", "f", "no", "n", "", "non-organic", "none", "nan", "conventional"}

TEMPLATES = {
    "products": "id,name,brand,category,price,is_organic,description\n"
                ",Organic Maple Syrup,Northwoods,syrup,13.49,yes,Grade A dark maple syrup\n"
                ",Sourdough Loaf,Hearth & Co,bakery,5.25,no,Slow-fermented country sourdough\n",
    "reviews": "product_name,rating,reviewer_name,review_text\n"
               "Organic Maple Syrup,5,Jo,Rich and smooth\n"
               "Sourdough Loaf,4,Sam,Great crust\n",
}


@dataclass
class ImportReport:
    kind: str
    total: int = 0
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    error_count: int = 0
    errors: list = field(default_factory=list)  # [(row_number, message)]
    mapping: dict = field(default_factory=dict)
    seconds: float = 0.0

    def add_error(self, row: int, msg: str) -> None:
        self.error_count += 1
        if len(self.errors) < 1000:
            self.errors.append((row, msg))

    def errors_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.errors, columns=["row", "problem"])


# --------------------------------------------------------------------------- reading files
def _norm(h) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(h).strip().lower()).strip("_")


def read_upload(filename: str, data: bytes) -> pd.DataFrame:
    """Parse an uploaded file into an all-string DataFrame."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("csv", "tsv", "txt"):
        df = _read_delimited(data, "\t" if ext == "tsv" else ",")
    elif ext in ("xlsx", "xlsm"):
        df = pd.read_excel(io.BytesIO(data), sheet_name=0, dtype=str, engine="openpyxl")
    elif ext == "json":
        df = _from_records(json.loads(data.decode("utf-8-sig")))
    elif ext in ("jsonl", "ndjson"):
        lines = [json.loads(l) for l in data.decode("utf-8-sig").splitlines() if l.strip()]
        df = pd.json_normalize(lines)
    elif ext == "xls":
        raise ValueError("Legacy .xls files aren't supported. Re-save as .xlsx or .csv.")
    else:
        raise ValueError(f"Unsupported file type '.{ext}'. Use CSV, TSV, XLSX, JSON or JSONL.")

    df = df.dropna(how="all").reset_index(drop=True)
    df = df.astype(object).where(df.notna(), "")
    df.columns = [str(c).strip() for c in df.columns]
    if len(df) > MAX_IMPORT_ROWS:
        raise ValueError(f"File has {len(df):,} rows; the limit is {MAX_IMPORT_ROWS:,}.")
    if df.empty:
        raise ValueError("The file contains no data rows.")
    return df


def _read_delimited(data: bytes, sep: str) -> pd.DataFrame:
    last_err: Optional[Exception] = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError as e:
            last_err = e
    else:
        raise ValueError(f"Could not decode file: {last_err}")
    df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
    if df.shape[1] == 1:  # wrong delimiter guess: try the usual suspects
        header = text.splitlines()[0] if text else ""
        for alt in (";", "\t", "|"):
            if alt in header:
                return pd.read_csv(io.StringIO(text), sep=alt, dtype=str, keep_default_na=False)
    return df


def _from_records(obj) -> pd.DataFrame:
    if isinstance(obj, dict):
        lists = [v for v in obj.values() if isinstance(v, list)]
        if not lists:
            raise ValueError("JSON must be a list of records or an object containing one.")
        obj = max(lists, key=len)
    if not isinstance(obj, list):
        raise ValueError("JSON must be a list of records.")
    return pd.json_normalize(obj)


# --------------------------------------------------------------------------- mapping
def detect_mapping(columns, kind: str) -> dict:
    """Return {canonical_field: original_column_or_None}."""
    aliases = PRODUCT_ALIASES if kind == "products" else REVIEW_ALIASES
    norm = {_norm(c): c for c in columns}
    used, mapping = set(), {}
    for fld, names in aliases.items():
        mapping[fld] = None
        for n in names:
            col = norm.get(n)
            if col is not None and col not in used:
                mapping[fld] = col
                used.add(col)
                break
    return mapping


def missing_required(mapping: dict, kind: str) -> list[str]:
    if kind == "products":
        return [f for f in ("name", "price") if not mapping.get(f)]
    miss = [] if mapping.get("rating") else ["rating"]
    if not (mapping.get("product_id") or mapping.get("product_name")):
        miss.append("product_id or product_name")
    return miss


# --------------------------------------------------------------------------- parsing helpers
def parse_price(v) -> float:
    s = re.sub(r"[^\d.,\-]", "", str(v))
    if not re.search(r"\d", s):
        raise ValueError("price isn't a number" if str(v).strip() else "price is empty")
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    x = float(s)
    if not math.isfinite(x) or x < 0:
        raise ValueError("price must be zero or positive")
    return round(x, 2)


def parse_bool(v) -> bool:
    s = str(v).strip().lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    raise ValueError(f"can't read '{v}' as yes/no")


def parse_int(v) -> Optional[int]:
    s = str(v).strip()
    if not s:
        return None
    return int(float(s))


def _get(row, col) -> str:
    return "" if col is None else str(row.get(col, "")).strip()


# --------------------------------------------------------------------------- imports
def import_products(df: pd.DataFrame, mapping: dict, *, update_existing: bool = True,
                    infer_organic: bool = True, default_category: str = "") -> ImportReport:
    t0 = time.time()
    rep = ImportReport("products", total=len(df), mapping=dict(mapping))
    miss = missing_required(mapping, "products")
    if miss:
        raise ValueError("Map these required columns first: " + ", ".join(miss))

    clean, seen_ids = [], set()
    for i, row in enumerate(df.to_dict("records")):
        rownum = i + 2  # header is row 1
        try:
            name = _get(row, mapping["name"])
            if not name:
                raise ValueError("name is empty")
            price = parse_price(_get(row, mapping["price"]))
            try:
                pid = parse_int(_get(row, mapping.get("id")))
            except ValueError:
                pid = None  # SKU-style ids like "A-123" aren't numeric: auto-assign
            if pid is not None and pid <= 0:
                pid = None
            if pid is not None and pid in seen_ids:
                raise ValueError(f"duplicate id {pid} in file")
            desc = _get(row, mapping.get("description"))
            raw_org = _get(row, mapping.get("is_organic"))
            if raw_org:
                organic = parse_bool(raw_org)
            elif infer_organic:
                organic = "organic" in f"{name} {desc}".lower()
            else:
                organic = False
            if pid is not None:
                seen_ids.add(pid)
            clean.append({
                "id": pid, "name": name[:200], "price": price,
                "category": (_get(row, mapping.get("category")) or default_category).lower()[:60] or None,
                "description": desc[:1000] or None, "is_organic": organic,
                "brand": _get(row, mapping.get("brand"))[:80] or None,
            })
        except ValueError as e:
            rep.add_error(rownum, str(e))

    if clean:
        with db.get_conn() as conn:
            res = db.upsert_products(conn, clean, update_existing)
            db.finalize(conn)
        rep.imported, rep.updated, rep.skipped = res["inserted"], res["updated"], res["skipped"]
    rep.seconds = time.time() - t0
    return rep


def import_reviews(df: pd.DataFrame, mapping: dict) -> ImportReport:
    t0 = time.time()
    rep = ImportReport("reviews", total=len(df), mapping=dict(mapping))
    miss = missing_required(mapping, "reviews")
    if miss:
        raise ValueError("Map these required columns first: " + ", ".join(miss))

    with db.get_conn() as conn:
        valid_ids = db.valid_product_ids(conn)
        names = db.name_lookup(conn)
        rows = []
        for i, row in enumerate(df.to_dict("records")):
            rownum = i + 2
            try:
                pid = None
                if mapping.get("product_id"):
                    try:
                        pid = parse_int(_get(row, mapping["product_id"]))
                    except ValueError:
                        pid = None
                if pid is None and mapping.get("product_name"):
                    pid = names.get(_get(row, mapping["product_name"]).lower())
                if pid is None or pid not in valid_ids:
                    raise ValueError("unknown product")
                try:
                    rating = float(_get(row, mapping["rating"]))
                except ValueError:
                    raise ValueError("rating isn't a number")
                if not 1 <= rating <= 5:
                    raise ValueError(f"rating {rating:g} is outside 1–5")
                rows.append((pid, rating, _get(row, mapping.get("reviewer_name"))[:80] or None,
                             _get(row, mapping.get("review_text"))[:2000] or None))
            except ValueError as e:
                rep.add_error(rownum, str(e))
        if rows:
            rep.imported = db.insert_reviews(conn, rows)
            db.finalize(conn)
    rep.seconds = time.time() - t0
    return rep
