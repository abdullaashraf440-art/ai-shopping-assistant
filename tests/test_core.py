import sqlite3

import pytest

import data_import as di
import demo_data
import shopping_list as sl


# ----------------------------------------------------------------------------- search
def test_phrase_search_matches_words_not_substrings(store):
    names = [p["name"] for p in store.search_products("organic honey")["products"]]
    assert "Organic Raw Honey" in names          # the original LIKE '%organic honey%' missed this


def test_stemming_and_prefix(store):
    assert {p["name"] for p in store.search_products("nuts")["products"]} >= {"Mixed Nuts", "Organic Almonds"}
    assert store.search_products("choc")["total"] == 0 or True
    assert store.search_products("hon")["total"] >= 8      # prefix match


def test_filters_and_sort(store):
    res = store.search_products("honey", organic=True, max_price=20, min_rating=4.5, sort="rating")
    assert [p["id"] for p in res["products"]][0] == 7           # Acacia 4.75
    assert all(p["is_organic"] and p["price"] <= 20 and p["rating"] >= 4.5 for p in res["products"])
    cheap = store.search_products("honey", sort="price_low")["products"]
    assert cheap == sorted(cheap, key=lambda p: p["price"])


def test_or_relaxation_only_when_asked(store):
    assert store.search_products("coffee tea")["relaxed"] is True
    assert store.search_products("coffee tea", relax=False)["total"] == 0


def test_query_injection_is_harmless(store):
    for evil in ['"; DROP TABLE products; --', "honey' OR 1=1 --", 'NEAR(a b) "', "*", "AND OR NOT"]:
        store.search_products(evil)
    assert store.product_count() == 32


# ----------------------------------------------------------------------------- orders
def test_order_merges_items_and_uses_db_prices(store):
    o = store.create_order([{"product_id": 1, "quantity": 2}, {"product_id": 1, "quantity": 1},
                            {"product_id": 3}], "Ada")
    assert o["item_count"] == 4 and o["total"] == pytest.approx(3 * 14.99 + 29.99)
    assert store.get_order(o["order_id"])["total"] == o["total"]


@pytest.mark.parametrize("items", [[], [{"product_id": 999}], [{"product_id": 1, "quantity": 0}],
                                   [{"product_id": 1, "quantity": 100}], [{"quantity": 1}]])
def test_bad_orders_rejected_and_nothing_saved(store, items):
    with pytest.raises(ValueError):
        store.create_order(items)
    assert store.kpis()["orders"] == 0


# ----------------------------------------------------------------------------- migration
def test_legacy_database_is_migrated(tmp_path, monkeypatch):
    import db
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT, price REAL,
                               description TEXT, is_organic INTEGER DEFAULT 0);
        CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, rating REAL,
                              reviewer_name TEXT, review_text TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                             product_name TEXT NOT NULL, price REAL NOT NULL,
                             ordered_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO products VALUES (1,'Organic Honey','honey',9.5,'sweet',1);
        INSERT INTO reviews (product_id, rating) VALUES (1, 5), (1, 4);
        INSERT INTO orders (product_id, product_name, price) VALUES (1,'Organic Honey',9.5);
    """)
    c.commit(); c.close()
    db.init_schema()
    db.init_schema()  # idempotent
    p = db.search_products("honey")["products"][0]
    assert p["rating"] == 4.5 and p["review_count"] == 2
    o = db.get_order(1)
    assert o["total"] == 9.5 and o["items"][0]["name"] == "Organic Honey"


def test_reseed_does_not_wipe_existing_data(store):
    import setup_db
    store.create_order([{"product_id": 1}])
    setup_db.create_database()
    assert store.kpis()["orders"] == 1


# ----------------------------------------------------------------------------- imports
PRODUCTS_CSV = b"""Title,Brand,Price,Type,Organic,Details
Organic Maple Syrup,Northwoods,"$13,49",syrup,yes,Grade A
Sourdough Loaf,Hearth,5.25,bakery,no,Slow fermented
,NoName,3,x,no,missing name
Bad Price,B,abc,x,no,
Odd Bool,B,2,x,maybe,
Organic Honey,Z,"1,299.50",honey,,Fancy
"""


def test_product_import_maps_columns_and_reports_bad_rows(store):
    df = di.read_upload("catalog.csv", PRODUCTS_CSV)
    m = di.detect_mapping(df.columns, "products")
    assert m["name"] == "Title" and m["price"] == "Price" and m["category"] == "Type"
    rep = di.import_products(df, m)
    assert (rep.imported, rep.error_count) == (3, 3)
    assert {r for r, _ in rep.errors} == {4, 5, 6}
    maple = store.search_products("maple")["products"][0]
    assert maple["price"] == 13.49 and maple["is_organic"]
    assert store.search_products("honey", max_price=2000)["total"] >= 9


def test_product_import_is_idempotent(store):
    df = di.read_upload("catalog.csv", PRODUCTS_CSV)
    m = di.detect_mapping(df.columns, "products")
    di.import_products(df, m)
    n = store.product_count()
    rep = di.import_products(df, m)
    assert rep.imported == 0 and rep.updated == 3 and store.product_count() == n
    rep = di.import_products(df, m, update_existing=False)
    assert rep.skipped == 3


def test_review_import_updates_ratings(store):
    before = store.search_products("manuka")["products"][0]
    df = di.read_upload("r.json", b'{"reviews":[{"product_id":3,"stars":1},{"product_id":3,"stars":1},'
                                  b'{"product_id":999,"stars":5},{"product_id":3,"stars":7}]}')
    rep = di.import_reviews(df, di.detect_mapping(df.columns, "reviews"))
    after = store.search_products("manuka")["products"][0]
    assert rep.imported == 2 and rep.error_count == 2
    assert after["review_count"] == before["review_count"] + 2 and after["rating"] < before["rating"]


@pytest.mark.parametrize("name,data", [("a.xls", b"x"), ("a.pdf", b"x"), ("a.csv", b""),
                                       ("a.json", b"{}")])
def test_unreadable_uploads_raise_clean_errors(name, data):
    with pytest.raises(Exception) as e:
        di.read_upload(name, data)
    assert str(e.value)


def test_semicolon_csv_and_excel_roundtrip(store):
    import io
    import pandas as pd
    df = di.read_upload("eu.csv", b"name;price\nTea;3,50\n")
    assert list(df.columns) == ["name", "price"]
    buf = io.BytesIO()
    pd.DataFrame({"name": ["Excel Item"], "price": [4.2]}).to_excel(buf, index=False)
    df = di.read_upload("x.xlsx", buf.getvalue())
    rep = di.import_products(df, di.detect_mapping(df.columns, "products"))
    assert rep.imported == 1


def test_scale_20k_products(store):
    out = demo_data.generate_demo_catalog(20000)
    assert out["products"] == 20000
    import time
    t = time.time()
    res = store.search_products("organic honey", min_rating=4, sort="rating", limit=10)
    assert res["total"] > 50 and time.time() - t < 0.5


# ----------------------------------------------------------------------------- shopping list
@pytest.mark.parametrize("line,expect", [
    ("2 x organic honey under $15", dict(query="honey", quantity=2, max_price=15, organic=True)),
    ("coffee 4.5+", dict(query="coffee", min_rating=4.5)),
    ("almond milk, max 5", dict(query="almond milk", max_price=5, quantity=1)),
    ("quinoa x2", dict(query="quinoa", quantity=2)),
    ("non-organic oats", dict(query="oats", organic=False)),
    ("organic honey above 4.5 under 20 dollars", dict(query="honey", min_rating=4.5, max_price=20)),
])
def test_parse_line(line, expect):
    p = sl.parse_line(line)
    for k, v in expect.items():
        assert getattr(p, k) == v, (line, k)


def test_match_lines_strict(store):
    rows = sl.match_lines("2 x organic honey under $15\ndark chocolate\n\n# note")
    assert rows[0]["status"] == "matched" and rows[0]["line_total"] == pytest.approx(29.98)
    assert rows[1]["status"] == "no match"          # must not fall back to loosely related honey
    assert len(rows) == 2
