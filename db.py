"""
Data layer: schema + migrations, full-text search, orders, analytics queries.

Design notes
- Search uses SQLite FTS5 (bm25 ranking, stemming, prefix matching) with an automatic
  LIKE fallback if the SQLite build has no FTS5.
- Average rating / review count are cached on the products row (refresh_ratings) so a
  search over 100k products never has to aggregate the reviews table.
- Every write path ends in finalize(): rebuild FTS, refresh ratings, bump catalog version
  (the UI uses the version number as its cache key).
"""
from __future__ import annotations

import contextlib
import re
import sqlite3
from typing import Iterable, Optional

import config
from config import ensure_dirs

DB_PATH = config.DB_PATH  # module-level so tests can monkeypatch it

STOPWORDS = {
    "a", "an", "the", "with", "for", "of", "and", "in", "on", "to", "me", "my", "some", "any",
    "i", "want", "need", "buy", "find", "show", "get", "please", "looking", "like", "would",
    "under", "over", "below", "above", "less", "than", "more", "at", "least", "rating", "rated",
    "best", "top", "good", "stars", "star", "price", "cheap", "cheapest", "is", "are", "that",
}

SORTS = ("relevance", "rating", "price_low", "price_high", "name", "reviews")


# --------------------------------------------------------------------------- connection
def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


@contextlib.contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- schema
def _columns(conn, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def has_fts(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'products_fts'"
    ).fetchone() is not None


def init_schema(conn=None) -> None:
    """Create tables if missing and migrate older databases in place (idempotent)."""
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS products (
                   id INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   category TEXT,
                   price REAL,
                   description TEXT,
                   is_organic INTEGER DEFAULT 0,
                   brand TEXT,
                   avg_rating REAL DEFAULT 0,
                   review_count INTEGER DEFAULT 0
               )"""
        )
        existing = _columns(conn, "products")
        migrated = False
        for col, ddl in (("brand", "TEXT"), ("avg_rating", "REAL DEFAULT 0"),
                         ("review_count", "INTEGER DEFAULT 0")):
            if col not in existing:
                conn.execute(f"ALTER TABLE products ADD COLUMN {col} {ddl}")
                migrated = True

        conn.execute(
            """CREATE TABLE IF NOT EXISTS reviews (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   product_id INTEGER,
                   rating REAL,
                   reviewer_name TEXT,
                   review_text TEXT,
                   FOREIGN KEY (product_id) REFERENCES products(id)
               )"""
        )

        # --- orders: migrate the legacy single-product table to header + line items
        legacy_rows = []
        ocols = _columns(conn, "orders")
        if ocols and "product_id" in ocols:
            legacy_rows = conn.execute(
                "SELECT id, product_id, product_name, price, ordered_at FROM orders"
            ).fetchall()
            conn.execute("DROP TABLE orders")
            ocols = []
        if not ocols:
            conn.execute(
                """CREATE TABLE orders (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       ordered_at TEXT NOT NULL DEFAULT (datetime('now')),
                       total REAL NOT NULL DEFAULT 0,
                       customer_name TEXT,
                       source TEXT DEFAULT 'app'
                   )"""
            )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS order_items (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   order_id INTEGER NOT NULL,
                   product_id INTEGER,
                   product_name TEXT NOT NULL,
                   unit_price REAL NOT NULL,
                   quantity INTEGER NOT NULL DEFAULT 1,
                   FOREIGN KEY (order_id) REFERENCES orders(id)
               )"""
        )
        for r in legacy_rows:
            conn.execute(
                "INSERT INTO orders (id, ordered_at, total, source) VALUES (?, ?, ?, 'legacy')",
                (r["id"], r["ordered_at"], r["price"]),
            )
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity) "
                "VALUES (?, ?, ?, ?, 1)",
                (r["id"], r["product_id"], r["product_name"], r["price"]),
            )

        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_product ON reviews(product_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_price ON products(price)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id)")

        created_fts = False
        if not has_fts(conn):
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE products_fts USING fts5("
                    "name, description, category, brand, tokenize='porter unicode61')"
                )
                created_fts = True
            except sqlite3.OperationalError:
                pass  # no FTS5 in this SQLite build -> LIKE fallback

        conn.commit()
        if created_fts or legacy_rows or migrated:
            rebuild_fts(conn)
            refresh_ratings(conn)
            conn.commit()
    finally:
        if own:
            conn.close()


def rebuild_fts(conn) -> None:
    if not has_fts(conn):
        return
    conn.execute("DELETE FROM products_fts")
    conn.execute(
        "INSERT INTO products_fts(rowid, name, description, category, brand) "
        "SELECT id, name, COALESCE(description,''), COALESCE(category,''), COALESCE(brand,'') "
        "FROM products"
    )


def refresh_ratings(conn) -> None:
    conn.execute(
        """UPDATE products SET
               avg_rating   = COALESCE((SELECT ROUND(AVG(rating), 2) FROM reviews r
                                        WHERE r.product_id = products.id), 0),
               review_count = (SELECT COUNT(*) FROM reviews r WHERE r.product_id = products.id)"""
    )


def bump_version(conn) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('version', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
    )


def finalize(conn) -> None:
    """Call after any catalog/review write."""
    rebuild_fts(conn)
    refresh_ratings(conn)
    bump_version(conn)


def catalog_version() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        return int(row["value"]) if row else 0


def product_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]


# --------------------------------------------------------------------------- search
def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9][a-z0-9'\-]*", (text or "").lower())
    toks = [t.strip("'-") for t in toks]
    return [t for t in toks if t and t not in STOPWORDS]


def _row_to_product(r) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "category": r["category"] or "",
        "brand": r["brand"] or "",
        "price": float(r["price"] or 0),
        "description": r["description"] or "",
        "is_organic": bool(r["is_organic"]),
        "rating": float(r["avg_rating"] or 0),
        "review_count": int(r["review_count"] or 0),
    }


def _build_query(conn, query, *, category, categories, min_price, max_price, organic,
                 min_rating, mode):
    tokens = tokenize(query)
    use_fts = bool(tokens) and has_fts(conn)
    joins, where, params = "", ["1=1"], []

    if tokens:
        if use_fts:
            joins = "JOIN products_fts ON products_fts.rowid = p.id"
            op = " AND " if mode == "and" else " OR "
            expr = op.join(f'("{t}" OR "{t}"*)' for t in tokens)
            where.append("products_fts MATCH ?")
            params.append(expr)
        else:
            clauses = []
            for t in tokens:
                like = f"%{t}%"
                clauses.append("(p.name LIKE ? OR p.description LIKE ? OR p.category LIKE ? "
                               "OR p.brand LIKE ?)")
                params.extend([like] * 4)
            where.append("(" + (" AND " if mode == "and" else " OR ").join(clauses) + ")")

    if category:
        where.append("LOWER(p.category) LIKE ?")
        params.append(f"%{category.lower().strip()}%")
    if categories:
        where.append(f"p.category IN ({','.join('?' * len(categories))})")
        params.extend(categories)
    if min_price is not None:
        where.append("p.price >= ?")
        params.append(min_price)
    if max_price is not None:
        where.append("p.price <= ?")
        params.append(max_price)
    if organic is not None:
        where.append("p.is_organic = ?")
        params.append(1 if organic else 0)
    if min_rating:
        where.append("p.avg_rating >= ?")
        params.append(min_rating)
    return tokens, use_fts, joins, " AND ".join(where), params


def _order_by(sort: str, use_fts: bool) -> str:
    if sort == "rating":
        return "p.avg_rating DESC, p.review_count DESC, p.price ASC"
    if sort == "price_low":
        return "p.price ASC, p.avg_rating DESC"
    if sort == "price_high":
        return "p.price DESC, p.avg_rating DESC"
    if sort == "name":
        return "p.name COLLATE NOCASE ASC"
    if sort == "reviews":
        return "p.review_count DESC, p.avg_rating DESC"
    if use_fts:  # relevance
        return "bm25(products_fts, 10.0, 2.0, 3.0, 5.0), p.avg_rating DESC, p.review_count DESC"
    return "p.avg_rating DESC, p.review_count DESC, p.name"


def search_products(query: str = "", *, category: Optional[str] = None,
                    categories: Optional[list[str]] = None,
                    min_price: Optional[float] = None, max_price: Optional[float] = None,
                    organic: Optional[bool] = None, min_rating: Optional[float] = None,
                    sort: str = "relevance", limit: int = 20, offset: int = 0,
                    mode: str = "and", relax: bool = True) -> dict:
    """Return {'total', 'products', 'relaxed'}; `relaxed` is True if OR-matching was needed."""
    if sort not in SORTS:
        sort = "relevance"
    limit = max(1, min(int(limit), 50000))
    offset = max(0, int(offset))
    with get_conn() as conn:
        def run(m):
            tokens, use_fts, joins, where, params = _build_query(
                conn, query, category=category, categories=categories, min_price=min_price,
                max_price=max_price, organic=organic, min_rating=min_rating, mode=m)
            total = conn.execute(
                f"SELECT COUNT(*) FROM products p {joins} WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT p.* FROM products p {joins} WHERE {where} "
                f"ORDER BY {_order_by(sort, use_fts)} LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
            return tokens, total, [_row_to_product(r) for r in rows]

        tokens, total, products = run(mode)
        relaxed = False
        if relax and total == 0 and mode == "and" and len(tokens) > 1:
            _, total, products = run("or")
            relaxed = total > 0
    return {"total": total, "products": products, "relaxed": relaxed}


def get_products(ids: Iterable[int]) -> list[dict]:
    ids = [int(i) for i in ids]
    if not ids:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM products WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    by_id = {r["id"]: _row_to_product(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def categories() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT COALESCE(NULLIF(category,''),'uncategorized') AS category,
                      COUNT(*) AS products, ROUND(AVG(price),2) AS avg_price,
                      ROUND(AVG(CASE WHEN review_count > 0 THEN avg_rating END),2) AS avg_rating
               FROM products GROUP BY 1 ORDER BY products DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def price_bounds() -> tuple[float, float]:
    with get_conn() as conn:
        lo, hi = conn.execute("SELECT MIN(price), MAX(price) FROM products").fetchone()
    return (float(lo or 0), float(hi or 0))


# --------------------------------------------------------------------------- reviews
def get_reviews(product_id: int, limit: int = 5) -> dict:
    with get_conn() as conn:
        agg = conn.execute(
            "SELECT ROUND(AVG(rating),2), COUNT(*) FROM reviews WHERE product_id = ?",
            (product_id,)).fetchone()
        dist = conn.execute(
            "SELECT CAST(ROUND(rating) AS INTEGER) AS star, COUNT(*) AS n FROM reviews "
            "WHERE product_id = ? GROUP BY star", (product_id,)).fetchall()
        rows = conn.execute(
            "SELECT rating, reviewer_name, review_text FROM reviews WHERE product_id = ? "
            "ORDER BY id DESC LIMIT ?", (product_id, max(1, min(limit, 25)))).fetchall()
    return {
        "product_id": product_id,
        "average_rating": agg[0] or 0.0,
        "review_count": agg[1] or 0,
        "distribution": {int(r["star"]): r["n"] for r in dist},
        "reviews": [dict(r) for r in rows],
    }


# --------------------------------------------------------------------------- bulk writes
def upsert_products(conn, rows: list[dict], update_existing: bool = True) -> dict:
    """Insert/update products in one transaction. Matches on id, else on (name, brand)."""
    existing = conn.execute("SELECT id, name, COALESCE(brand,'') AS brand FROM products").fetchall()
    ids = {r["id"] for r in existing}
    keymap = {(r["name"].lower(), r["brand"].lower()): r["id"] for r in existing}
    next_id = (max(ids) if ids else 0) + 1
    batch, inserted, updated, skipped = [], 0, 0, 0

    for row in rows:
        key = (row["name"].lower(), (row.get("brand") or "").lower())
        pid = row.get("id")
        if pid is not None and pid in ids:
            is_new = False
        elif pid is None and key in keymap:
            pid, is_new = keymap[key], False
        else:
            is_new = True
            if pid is None:
                pid = next_id
        if not is_new and not update_existing:
            skipped += 1
            continue
        if is_new:
            inserted += 1
            ids.add(pid)
            keymap[key] = pid
            next_id = max(next_id, pid + 1)
        else:
            updated += 1
        batch.append((pid, row["name"], row.get("category"), row["price"], row.get("description"),
                      1 if row.get("is_organic") else 0, row.get("brand")))

    conn.executemany(
        """INSERT INTO products (id, name, category, price, description, is_organic, brand)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET name = excluded.name, category = excluded.category,
               price = excluded.price, description = excluded.description,
               is_organic = excluded.is_organic, brand = excluded.brand""",
        batch)
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def insert_reviews(conn, rows: list[tuple]) -> int:
    """rows: (product_id, rating, reviewer_name, review_text)."""
    conn.executemany(
        "INSERT INTO reviews (product_id, rating, reviewer_name, review_text) VALUES (?, ?, ?, ?)",
        rows)
    return len(rows)


def name_lookup(conn) -> dict[str, int]:
    return {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM products")}


def valid_product_ids(conn) -> set[int]:
    return {r[0] for r in conn.execute("SELECT id FROM products")}


# --------------------------------------------------------------------------- orders
def create_order(items: list[dict], customer_name: Optional[str] = None,
                 source: str = "app") -> dict:
    """items: [{'product_id': int, 'quantity': int}]. Prices always come from the database."""
    merged: dict[int, int] = {}
    for it in items:
        try:
            pid, qty = int(it["product_id"]), int(it.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            raise ValueError("Each item needs a numeric product_id and quantity.")
        if not 1 <= qty <= 99:
            raise ValueError(f"Quantity for product {pid} must be between 1 and 99.")
        merged[pid] = merged.get(pid, 0) + qty
    if not merged:
        raise ValueError("The order is empty.")

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, name, price FROM products WHERE id IN ({','.join('?' * len(merged))})",
            list(merged)).fetchall()
        found = {r["id"]: r for r in rows}
        missing = [p for p in merged if p not in found]
        if missing:
            raise ValueError(f"Product ID(s) not found: {', '.join(map(str, missing))}")

        lines, total_cents = [], 0
        for pid, qty in merged.items():
            cents = round(found[pid]["price"] * 100)
            total_cents += cents * qty
            lines.append({"product_id": pid, "name": found[pid]["name"],
                          "unit_price": cents / 100, "quantity": qty,
                          "line_total": cents * qty / 100})
        cur = conn.execute(
            "INSERT INTO orders (total, customer_name, source) VALUES (?, ?, ?)",
            (total_cents / 100, (customer_name or "").strip() or None, source))
        order_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity) "
            "VALUES (?, ?, ?, ?, ?)",
            [(order_id, l["product_id"], l["name"], l["unit_price"], l["quantity"]) for l in lines])
        placed = conn.execute("SELECT ordered_at FROM orders WHERE id = ?", (order_id,)).fetchone()[0]
    return {"order_id": order_id, "ordered_at": placed,
            "customer_name": (customer_name or "").strip() or None, "items": lines,
            "total": total_cents / 100, "item_count": sum(l["quantity"] for l in lines)}


def get_order(order_id: int) -> Optional[dict]:
    with get_conn() as conn:
        o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not o:
            return None
        items = conn.execute(
            "SELECT product_id, product_name AS name, unit_price, quantity, "
            "ROUND(unit_price * quantity, 2) AS line_total FROM order_items WHERE order_id = ?",
            (order_id,)).fetchall()
    return {"order_id": o["id"], "ordered_at": o["ordered_at"], "customer_name": o["customer_name"],
            "items": [dict(i) for i in items], "total": o["total"],
            "item_count": sum(i["quantity"] for i in items)}


# --------------------------------------------------------------------------- dataframes
def _df(sql: str, params=()):
    import pandas as pd
    with get_conn() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def products_df():
    return _df("SELECT id, name, brand, category, price, is_organic, avg_rating AS rating, "
               "review_count, description FROM products ORDER BY id")


def reviews_df():
    return _df("SELECT r.id, r.product_id, p.name AS product, r.rating, r.reviewer_name, "
               "r.review_text FROM reviews r LEFT JOIN products p ON p.id = r.product_id "
               "ORDER BY r.id")


def orders_df():
    return _df("SELECT o.id AS order_id, o.ordered_at, o.customer_name, o.total, "
               "COALESCE((SELECT SUM(quantity) FROM order_items i WHERE i.order_id = o.id),0) "
               "AS items FROM orders o ORDER BY o.id DESC")


def order_items_df():
    return _df("SELECT order_id, product_id, product_name, unit_price, quantity, "
               "ROUND(unit_price * quantity, 2) AS line_total FROM order_items ORDER BY order_id DESC")


def category_stats_df():
    return _df("SELECT COALESCE(NULLIF(category,''),'uncategorized') AS category, "
               "COUNT(*) AS products, ROUND(AVG(price),2) AS avg_price, "
               "ROUND(AVG(CASE WHEN review_count > 0 THEN avg_rating END),2) AS avg_rating, "
               "SUM(review_count) AS reviews FROM products GROUP BY 1 ORDER BY products DESC")


def rating_distribution_df():
    return _df("SELECT CAST(ROUND(rating) AS INTEGER) AS stars, COUNT(*) AS reviews FROM reviews "
               "GROUP BY stars ORDER BY stars")


def best_value_df(n: int = 10):
    return _df("SELECT id, name, category, price, avg_rating AS rating, review_count, "
               "ROUND(avg_rating / NULLIF(price, 0), 3) AS rating_per_dollar FROM products "
               "WHERE review_count >= 3 ORDER BY rating_per_dollar DESC LIMIT ?", (n,))


def orders_by_day_df():
    return _df("SELECT date(ordered_at) AS day, COUNT(*) AS orders, ROUND(SUM(total),2) AS revenue "
               "FROM orders GROUP BY day ORDER BY day")


def revenue_by_product_df(n: int = 10):
    return _df("SELECT product_name, SUM(quantity) AS units, ROUND(SUM(unit_price*quantity),2) AS "
               "revenue FROM order_items GROUP BY product_name ORDER BY revenue DESC LIMIT ?", (n,))


def kpis() -> dict:
    with get_conn() as conn:
        p, c, r = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT category), "
            "ROUND(AVG(CASE WHEN review_count > 0 THEN avg_rating END), 2) FROM products").fetchone()
        reviews = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        orders, revenue = conn.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders").fetchone()
        organic = conn.execute("SELECT COUNT(*) FROM products WHERE is_organic = 1").fetchone()[0]
    return {"products": p, "categories": c, "avg_rating": r or 0, "reviews": reviews,
            "orders": orders, "revenue": revenue, "organic": organic}
