"""The app's views. Each render_* function draws one section of the UI."""
from __future__ import annotations

import io
import logging
import os
import time
import uuid

import pandas as pd
import streamlit as st

import config
import data_import as di
import db
import demo_data
import exports
import setup_db
import shopping_agent as agent_mod
import shopping_list
import ui

log = logging.getLogger("shopai")

SORT_LABELS = {"relevance": "Most relevant", "rating": "Top rated", "price_low": "Price: low to high",
               "price_high": "Price: high to low", "reviews": "Most reviewed", "name": "Name A–Z"}


# =========================================================================== shared helpers
@st.cache_data(show_spinner=False, max_entries=128)
def cached_search(version: int, query: str, categories: tuple, min_price, max_price, organic,
                  min_rating, sort: str, limit: int, offset: int) -> dict:
    return db.search_products(
        query, categories=list(categories) or None, min_price=min_price, max_price=max_price,
        organic=organic, min_rating=min_rating, sort=sort, limit=limit, offset=offset)


@st.cache_data(show_spinner=False)
def cached_categories(version: int) -> list[dict]:
    return db.categories()


@st.cache_data(show_spinner=False)
def cached_bounds(version: int) -> tuple:
    return db.price_bounds()


def ai_enabled() -> bool:
    return agent_mod.llm_available()


# =========================================================================== assistant
def queue_turn(text: str, display: str | None = None, image: bytes | None = None) -> None:
    st.session_state.pending_turn = {"text": text, "display": display or text, "image": image}
    st.session_state.view = "💬 Assistant"


def queue_image() -> None:
    up = st.session_state.get("img_up")
    if up is None:
        return
    from PIL import Image
    config.ensure_dirs()
    for old in config.UPLOAD_DIR.glob("*"):  # housekeeping: drop uploads older than 1h
        if time.time() - old.stat().st_mtime > 3600:
            old.unlink(missing_ok=True)
    img = Image.open(io.BytesIO(up.getvalue())).convert("RGB")
    img.thumbnail((1024, 1024))
    path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}.jpg"
    img.save(path, "JPEG", quality=88)
    buf = io.BytesIO()
    img.thumbnail((320, 320))
    img.save(buf, "JPEG", quality=80)
    queue_turn(f"I uploaded a product image. Please analyze it and find similar products in the "
               f"store. Image path: {path}", display=f"Searching by image: **{up.name}**",
               image=buf.getvalue())


def _history(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        content = m.get("agent_content") or m["content"]
        if m["role"] == "assistant":
            content += agent_mod.history_note(m.get("products", []))
        out.append({"role": m["role"], "content": content})
    return out


def render_body(i: int, msg: dict) -> None:
    if msg.get("image"):
        st.image(msg["image"], width=150)
    st.markdown(ui.esc_md(msg["content"]))
    if msg.get("notice"):
        st.caption(msg["notice"])
    if msg.get("order"):
        ui.order_summary(msg["order"], f"chat{i}")
    if msg.get("comparison"):
        ui.comparison_table(msg["comparison"])
    if msg.get("products"):
        shown = msg["products"]
        ui.product_grid(shown[:6], f"m{i}")
        if len(shown) > 6:
            with st.expander(f"Show {len(shown) - 6} more"):
                ui.product_grid(shown[6:], f"m{i}x")


def _run_turn(turn: dict) -> None:
    msgs = st.session_state.messages
    user = {"role": "user", "content": turn["display"], "agent_content": turn["text"],
            "image": turn.get("image")}
    msgs.append(user)
    with st.chat_message("user"):
        render_body(len(msgs) - 1, user)

    bubble = st.chat_message("assistant", avatar="🛒")
    with bubble:
        status = st.status("Thinking…", expanded=False)

    reply, notice = None, None
    if ai_enabled():
        def on_status(label: str) -> None:
            status.update(label=label)
            status.write(label)
        try:
            res = agent_mod.run_agent(_history(msgs), on_status=on_status)
            reply = {"content": res.text, "products": res.products, "comparison": res.comparison,
                     "order": res.order}
            if res.order:
                st.toast(f"Order #{res.order['order_id']} placed", icon="✅")
        except Exception as e:  # network, rate limit, bad key...
            log.exception("agent failure")
            busy = any(w in str(e).lower() for w in ("rate", "429", "quota", "overloaded"))
            notice = ("⚠️ The AI service is busy right now, so I used basic search instead."
                      if busy else "⚠️ The AI service hit a problem, so I used basic search instead.")
    if reply is None:
        if turn["text"].startswith("I uploaded a product image"):
            reply = {"content": "Image search needs the AI service, which isn't available right now.",
                     "products": []}
        else:
            reply = shopping_list.basic_reply(turn["text"])
            reply = {"content": reply["text"], "products": reply["products"]}
        if not ai_enabled():
            notice = "Basic mode: add a GROQ_API_KEY to unlock conversation, comparisons and chat checkout."

    reply.update(role="assistant", notice=notice)
    msgs.append(reply)
    status.update(label="Done", state="complete", expanded=False)
    with bubble:
        render_body(len(msgs) - 1, reply)


def render_assistant() -> None:
    msgs = st.session_state.setdefault("messages", [])
    typed = st.chat_input("e.g. organic honey under $15 with 4+ rating")  # pinned to the bottom
    turn = st.session_state.pop("pending_turn", None)
    if typed:
        turn = {"text": typed, "display": typed}
    if not ai_enabled():
        st.info("**Basic mode**: smart keyword search with price / rating / organic filters works "
                "now. Add a `GROQ_API_KEY` to enable the conversational AI, comparisons, "
                "image search and checkout by chat.", icon="ℹ️")

    if not msgs and not turn:
        st.markdown(
            '<div class="welcome"><h3>👋 What are you shopping for today?</h3>'
            '<div class="feat">Ask in plain English with prices, ratings or organic preferences, '
            'upload a photo in the sidebar, or try one of these:</div></div>', unsafe_allow_html=True)
        cols = st.columns(2)
        for i, ex in enumerate(ui.EXAMPLES):
            cols[i % 2].button(ex, key=f"ex_{i}", on_click=queue_turn, args=(ex,),
                               width="stretch", icon="✨")

    for i, m in enumerate(msgs):
        with st.chat_message(m["role"], avatar="🛒" if m["role"] == "assistant" else None):
            render_body(i, m)

    if turn:
        _run_turn(turn)

    if msgs:
        st.button("Clear conversation", on_click=lambda: st.session_state.update(messages=[]),
                  type="tertiary", icon="🧹")


# =========================================================================== browse
def render_browse() -> None:
    ver = db.catalog_version()
    cats = [c["category"] for c in cached_categories(ver)]
    lo, hi = cached_bounds(ver)

    with st.container(border=True):
        a, b, c = st.columns([3, 1.4, 1.2], vertical_alignment="bottom")
        q = a.text_input("Search", key="b_q", placeholder="Search products, brands, categories…")
        sort = b.selectbox("Sort by", list(SORT_LABELS), format_func=SORT_LABELS.get, key="b_sort")
        mode = c.segmented_control("View", ["Cards", "Table"], default="Cards", key="b_mode") or "Cards"
        d, e, f, g = st.columns([2, 2, 2, 1.4], vertical_alignment="bottom")
        sel_cats = d.multiselect("Categories", cats, key="b_cats", placeholder="All categories")
        if hi > lo:
            price = e.slider("Price", float(lo), float(hi), (float(lo), float(hi)), step=0.5,
                             format="$%.2f", key="b_price")
        else:
            price = (lo, hi)
        min_rating = f.slider("Min. rating", 0.0, 5.0, 0.0, step=0.5, key="b_rating")
        org = g.segmented_control("Organic", ["All", "Organic", "Not organic"], default="All",
                                  key="b_org") or "All"

    organic = {"All": None, "Organic": True, "Not organic": False}[org]
    pmin = price[0] if price[0] > lo else None
    pmax = price[1] if price[1] < hi else None

    sig = (q, sort, tuple(sel_cats), price, min_rating, org, mode)
    if st.session_state.get("b_sig") != sig:
        st.session_state.b_sig = sig
        st.session_state.b_page = 1

    if mode == "Table":
        res = cached_search(ver, q, tuple(sel_cats), pmin, pmax, organic, min_rating or None, sort,
                            config.MAX_TABLE_ROWS, 0)
        st.markdown(f"**{res['total']:,}** matching products")
        if res["total"] > config.MAX_TABLE_ROWS:
            st.caption(f"Showing the first {config.MAX_TABLE_ROWS:,}. Narrow the filters, or export "
                       "everything from the **Data** tab.")
        if not res["products"]:
            return _empty_state()
        df = pd.DataFrame(res["products"])[["id", "name", "brand", "category", "price", "is_organic",
                                            "rating", "review_count"]]
        event = st.dataframe(
            df, hide_index=True, width="stretch", height=460, on_select="rerun",
            selection_mode="multi-row", key="b_table",
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "name": st.column_config.TextColumn("Product", width="large"),
                "price": st.column_config.NumberColumn("Price", format="$%.2f"),
                "is_organic": st.column_config.CheckboxColumn("Organic", width="small"),
                "rating": st.column_config.ProgressColumn("Rating", min_value=0, max_value=5,
                                                          format="%.2f ★"),
                "review_count": st.column_config.NumberColumn("Reviews"),
            })
        rows = event.selection.rows
        x, y, z = st.columns([1.3, 1, 1])
        if x.button(f"Add {len(rows)} selected to cart", disabled=not rows, type="primary",
                    icon="🛒", width="stretch"):
            for r in rows:
                ui.cart_add(res["products"][r])
            st.rerun()
        y.download_button("CSV", exports.to_csv(df), "products.csv", "text/csv", width="stretch",
                          icon="📄")
        z.download_button("Excel", exports.to_xlsx({"Products": df}), "products.xlsx",
                          width="stretch", icon="📊")
        return

    page_size = 12
    res = cached_search(ver, q, tuple(sel_cats), pmin, pmax, organic, min_rating or None, sort,
                        page_size, (st.session_state.get("b_page", 1) - 1) * page_size)
    pages = max(1, -(-res["total"] // page_size))
    st.session_state.b_page = min(max(1, st.session_state.get("b_page", 1)), pages)
    if not res["products"] and res["total"]:
        res = cached_search(ver, q, tuple(sel_cats), pmin, pmax, organic, min_rating or None, sort,
                            page_size, (st.session_state.b_page - 1) * page_size)
    st.markdown(f"**{res['total']:,}** products · sorted by *{SORT_LABELS[sort].lower()}*")
    if not res["products"]:
        return _empty_state()
    ui.product_grid(res["products"], f"b{st.session_state.b_page}")
    if pages > 1:
        l, m, r = st.columns([1, 2, 1], vertical_alignment="center")
        l.button("◀ Previous", disabled=st.session_state.b_page <= 1, width="stretch",
                 on_click=lambda: st.session_state.update(b_page=st.session_state.b_page - 1))
        m.markdown(f"<div style='text-align:center'>Page <b>{st.session_state.b_page}</b> of "
                   f"<b>{pages:,}</b></div>", unsafe_allow_html=True)
        r.button("Next ▶", disabled=st.session_state.b_page >= pages, width="stretch",
                 on_click=lambda: st.session_state.update(b_page=st.session_state.b_page + 1))


def _empty_state() -> None:
    st.markdown("### 🔍 No products match")
    st.caption("Try removing a filter, lowering the minimum rating, or widening the price range.")


# =========================================================================== bulk list
SAMPLE_LIST = """2 x organic honey under $15
coffee 4.5+
almond milk max 5
best rated olive oil
quinoa
dark chocolate
3 chia seeds"""


def render_bulk() -> None:
    st.markdown("#### 📝 Bulk shopping list")
    st.caption("Paste or upload a whole list. One item per line, with optional quantity, price cap, "
               "rating or organic. I'll pick the best-rated match for every line. No AI quota used.")
    left, right = st.columns([3, 2])
    with left:
        if st.button("Load an example", type="tertiary", icon="✨"):
            st.session_state.bulk_text = SAMPLE_LIST
        text = st.text_area("Your list", key="bulk_text", height=220,
                            placeholder="2 x organic honey under $15\ncoffee 4.5+\nalmond milk max 5")
    with right:
        up = st.file_uploader("…or upload a .txt / .csv list", type=["txt", "csv"], key="bulk_up")
        st.caption("For CSV, put one request per row in the first column.")
        if up:
            raw = up.getvalue().decode("utf-8-sig", errors="replace")
            if up.name.lower().endswith(".csv"):
                raw = "\n".join(l.split(",")[0] for l in raw.splitlines())
            text = (text + "\n" + raw) if text else raw

    if st.button("Match my list", type="primary", icon="🔎", disabled=not (text or "").strip()):
        t0 = time.time()
        st.session_state.bulk_result = shopping_list.match_lines(text)
        st.session_state.bulk_secs = time.time() - t0

    rows = st.session_state.get("bulk_result")
    if not rows:
        return
    matched = [r for r in rows if r["status"] == "matched"]
    total = sum(r["line_total"] for r in matched)
    m1, m2, m3 = st.columns(3)
    m1.metric("Lines", len(rows), border=True)
    m2.metric("Matched", f"{len(matched)} / {len(rows)}", border=True)
    m3.metric("Estimated total", f"${total:,.2f}", border=True)

    table = pd.DataFrame([{
        "Request": r["request"],
        "Status": "✅ matched" if r["status"] == "matched" else "❌ " + r["status"],
        "Product": r["product"]["name"] if r["status"] == "matched" else "",
        "ID": r["product"]["id"] if r["status"] == "matched" else None,
        "Qty": r.get("quantity"),
        "Unit price": r["product"]["price"] if r["status"] == "matched" else None,
        "Rating": r["product"]["rating"] if r["status"] == "matched" else None,
        "Line total": r.get("line_total"),
        "Other options": ", ".join(r.get("alternatives", [])),
    } for r in rows])
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        "Unit price": st.column_config.NumberColumn(format="$%.2f"),
        "Line total": st.column_config.NumberColumn(format="$%.2f"),
        "Rating": st.column_config.NumberColumn(format="%.2f ★"),
    })
    a, b, c = st.columns(3)
    if a.button(f"Add {len(matched)} matched items to cart", type="primary", icon="🛒",
                disabled=not matched, width="stretch"):
        for r in matched:
            ui.cart_add(r["product"], r["quantity"])
        st.rerun()
    b.download_button("CSV", exports.to_csv(table), "shopping_list.csv", "text/csv",
                      width="stretch", icon="📄")
    c.download_button("Excel", exports.to_xlsx({"Shopping list": table}), "shopping_list.xlsx",
                      width="stretch", icon="📊")


# =========================================================================== orders
def render_orders() -> None:
    orders = db.orders_df()
    if orders.empty:
        st.markdown("### 📦 No orders yet")
        st.caption("Place an order from your cart or by chatting with the assistant. It will appear here.")
        return
    m1, m2, m3 = st.columns(3)
    m1.metric("Orders", f"{len(orders):,}", border=True)
    m2.metric("Items sold", f"{int(orders['items'].sum()):,}", border=True)
    m3.metric("Revenue", f"${orders['total'].sum():,.2f}", border=True)

    st.dataframe(orders, hide_index=True, width="stretch", height=280, column_config={
        "order_id": st.column_config.NumberColumn("Order #", width="small"),
        "ordered_at": st.column_config.TextColumn("Placed (UTC)"),
        "customer_name": "Customer",
        "total": st.column_config.NumberColumn("Total", format="$%.2f"),
        "items": st.column_config.NumberColumn("Items", width="small"),
    })
    pick = st.selectbox("View receipt", orders["order_id"].tolist(),
                        format_func=lambda i: f"Order #{i}")
    order = db.get_order(int(pick))
    if order:
        with st.container(border=True):
            ui.order_summary(order, f"orders{pick}")
    a, b = st.columns(2)
    a.download_button("Export orders (CSV)", exports.to_csv(orders), "orders.csv", "text/csv",
                      width="stretch", icon="📄")
    b.download_button("Export orders + items (Excel)",
                      exports.to_xlsx({"Orders": orders, "Items": db.order_items_df()}),
                      "orders.xlsx", width="stretch", icon="📊")


# =========================================================================== insights
def render_insights() -> None:
    k = db.kpis()
    c = st.columns(5)
    c[0].metric("Products", f"{k['products']:,}", border=True)
    c[1].metric("Organic", f"{k['organic']:,}", border=True)
    c[2].metric("Reviews", f"{k['reviews']:,}", border=True)
    c[3].metric("Avg rating", f"{k['avg_rating']:.2f} ★", border=True)
    c[4].metric("Revenue", f"${k['revenue']:,.2f}", border=True)

    cats = db.category_stats_df()
    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**Average rating by category**")
        st.bar_chart(cats.set_index("category")["avg_rating"], horizontal=True, color="#16a34a")
    with b.container(border=True):
        st.markdown("**Average price by category**")
        st.bar_chart(cats.set_index("category")["avg_price"], horizontal=True, color="#f59e0b")

    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**Rating distribution**")
        dist = db.rating_distribution_df()
        if dist.empty:
            st.caption("No reviews yet.")
        else:
            st.bar_chart(dist.set_index("stars")["reviews"], color="#16a34a")
    with b.container(border=True):
        st.markdown("**Products per category**")
        st.bar_chart(cats.set_index("category")["products"], horizontal=True, color="#0ea5e9")

    with st.container(border=True):
        st.markdown("**💎 Best value: highest rating per dollar** (3+ reviews)")
        st.dataframe(db.best_value_df(10), hide_index=True, width="stretch", column_config={
            "price": st.column_config.NumberColumn(format="$%.2f"),
            "rating": st.column_config.NumberColumn(format="%.2f ★")})

    day, rev = db.orders_by_day_df(), db.revenue_by_product_df(10)
    if not day.empty:
        a, b = st.columns(2)
        with a.container(border=True):
            st.markdown("**Revenue by day**")
            st.bar_chart(day.set_index("day")["revenue"], color="#16a34a")
        with b.container(border=True):
            st.markdown("**Top products by revenue**")
            st.bar_chart(rev.set_index("product_name")["revenue"], horizontal=True, color="#f59e0b")


# =========================================================================== data
def _import_panel(kind: str) -> None:
    label = "products" if kind == "products" else "reviews"
    st.caption(
        "CSV, TSV, Excel (.xlsx), JSON or JSONL. Columns are matched automatically (e.g. "
        "`title`→name, `cost`→price). Bad rows are skipped and reported, never half-imported."
        if kind == "products" else
        "One row per review. Link each to a product by `product_id` or exact `product_name`; "
        "ratings must be 1–5. Ratings across the store update automatically.")
    up = st.file_uploader(f"Upload {label} file", key=f"up_{kind}",
                          type=["csv", "tsv", "txt", "xlsx", "xlsm", "json", "jsonl", "ndjson"])
    st.download_button(f"Download {label} template (CSV)", di.TEMPLATES[kind],
                       f"{label}_template.csv", "text/csv", type="tertiary", icon="⬇️",
                       key=f"tpl_{kind}")
    if not up:
        return
    try:
        with st.spinner("Reading file…"):
            df = di.read_upload(up.name, up.getvalue())
    except Exception as e:
        st.error(f"Couldn't read that file: {e}")
        return

    st.markdown(f"**{len(df):,}** rows × **{len(df.columns)}** columns detected")
    st.dataframe(df.head(8), hide_index=True, width="stretch")

    detected = di.detect_mapping(df.columns, kind)
    st.markdown("**Column mapping**: check the auto-detected matches")
    none = "— none —"
    mapping, cols = {}, st.columns(3)
    for i, (fld, guess) in enumerate(detected.items()):
        options = [none] + list(df.columns)
        mapping[fld] = cols[i % 3].selectbox(
            fld.replace("_", " ").title() + (" *" if fld in ("name", "price", "rating") else ""),
            options, index=options.index(guess) if guess else 0, key=f"map_{kind}_{up.file_id}_{fld}")
        mapping[fld] = None if mapping[fld] == none else mapping[fld]

    opts = {}
    if kind == "products":
        o1, o2 = st.columns(2)
        opts["update_existing"] = o1.toggle("Update products that already exist", True,
                                            help="Matched by ID, otherwise by name + brand.")
        opts["infer_organic"] = o2.toggle("Detect “organic” from name/description", True,
                                          help="Used when the Organic column is missing or blank.")
    missing = di.missing_required(mapping, kind)
    if missing:
        st.warning("Map the required column(s): " + ", ".join(missing))

    if st.button(f"Import {len(df):,} {label}", type="primary", icon="📥", disabled=bool(missing),
                 key=f"go_{kind}"):
        try:
            with st.spinner(f"Importing {len(df):,} rows…"):
                rep = (di.import_products(df, mapping, **opts) if kind == "products"
                       else di.import_reviews(df, mapping))
        except Exception as e:
            st.error(f"Import failed: {e}")
            return
        if rep.imported or rep.updated:
            st.success(f"Imported in {rep.seconds:.1f}s. The catalog and search index are updated.",
                       icon="✅")
        a, b, c, d = st.columns(4)
        a.metric("Added", f"{rep.imported:,}", border=True)
        b.metric("Updated", f"{rep.updated:,}", border=True)
        c.metric("Skipped", f"{rep.skipped:,}", border=True)
        d.metric("Problem rows", f"{rep.error_count:,}", border=True)
        if rep.error_count:
            with st.expander(f"Review {rep.error_count:,} problem rows"):
                edf = rep.errors_df()
                st.dataframe(edf, hide_index=True, width="stretch")
                st.download_button("Download problem rows", exports.to_csv(edf),
                                   f"{kind}_import_problems.csv", "text/csv", icon="⬇️")
                if rep.error_count > len(rep.errors):
                    st.caption(f"Showing the first {len(rep.errors):,}.")


def render_data() -> None:
    st.markdown("#### 🗄️ Data center")
    t1, t2, t3, t4 = st.tabs(["📥 Import products", "💬 Import reviews", "📤 Export", "🧪 Demo & maintenance"])
    with t1:
        _import_panel("products")
    with t2:
        _import_panel("reviews")
    with t3:
        st.caption("Download your data for spreadsheets and BI tools.")
        a, b, c = st.columns(3)
        if a.button("Prepare product workbook", width="stretch", icon="📊"):
            with st.spinner("Building workbook…"):
                st.session_state.x_products = exports.to_xlsx(
                    {"Products": db.products_df(), "Categories": db.category_stats_df()})
        if st.session_state.get("x_products"):
            a.download_button("⬇️ products.xlsx", st.session_state.x_products, "products.xlsx",
                              width="stretch")
        if b.button("Prepare reviews CSV", width="stretch", icon="💬"):
            with st.spinner("Exporting reviews…"):
                st.session_state.x_reviews = exports.to_csv(db.reviews_df())
        if st.session_state.get("x_reviews"):
            b.download_button("⬇️ reviews.csv", st.session_state.x_reviews, "reviews.csv", "text/csv",
                              width="stretch")
        if c.button("Prepare orders workbook", width="stretch", icon="📦"):
            st.session_state.x_orders = exports.to_xlsx(
                {"Orders": db.orders_df(), "Items": db.order_items_df()})
        if st.session_state.get("x_orders"):
            c.download_button("⬇️ orders.xlsx", st.session_state.x_orders, "orders.xlsx",
                              width="stretch")
    with t4:
        st.markdown("**Generate a large demo catalog**")
        st.caption("Adds realistic synthetic products and reviews: handy for demos and for seeing "
                   "how search, filters and charts behave at scale.")
        n = st.slider("Products to add", 500, 50000, 5000, step=500)
        if st.button(f"Generate {n:,} products", icon="🧪"):
            with st.spinner("Generating and indexing…"):
                t0 = time.time()
                out = demo_data.generate_demo_catalog(n)
            st.success(f"Added {out['products']:,} products and {out['reviews']:,} reviews "
                       f"in {time.time() - t0:.1f}s.", icon="✅")
        st.divider()
        st.markdown("**Reset to the original 32-product demo store**")
        st.caption("Deletes all products, reviews and orders, then re-seeds the demo data.")
        ok = st.checkbox("I understand this permanently deletes current data")
        if st.button("Reset store", disabled=not ok, icon="⚠️"):
            setup_db.create_database(reset=True)
            for k in ("messages", "cart", "bulk_result"):
                st.session_state.pop(k, None)
            st.success("Store reset to demo data.", icon="✅")
