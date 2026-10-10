"""Reusable UI: theme CSS, hero, product cards, comparison table, cart, order summary."""
from __future__ import annotations

import html

import pandas as pd
import streamlit as st

import db
import exports
from config import APP_NAME

CSS = """
<style>
#MainMenu, footer {visibility: hidden;}
.block-container {padding-top: 1.4rem; padding-bottom: 5rem; max-width: 1180px;}
html {scroll-behavior: smooth;}

/* ---------- hero ---------- */
.hero {background: linear-gradient(135deg,#052e16 0%,#166534 55%,#22c55e 130%);
  color:#fff; border-radius:20px; padding:26px 30px; margin-bottom:14px;
  box-shadow:0 10px 30px -12px rgba(22,101,52,.55);}
.hero h1 {margin:0 0 4px 0; font-size:1.9rem; letter-spacing:-.02em; color:#fff; padding:0;}
.hero p {margin:0; opacity:.9; font-size:1.02rem;}
.hero .chips {display:flex; gap:10px; flex-wrap:wrap; margin-top:16px;}
.hero .chip {background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.22);
  padding:5px 13px; border-radius:99px; font-size:.84rem; backdrop-filter:blur(4px);}
.hero .chip b {font-weight:700;}

/* ---------- product cards ---------- */
[class*="st-key-pc_"] {transition: box-shadow .15s ease, transform .15s ease;}
[class*="st-key-pc_"]:hover {box-shadow:0 8px 24px -10px rgba(0,0,0,.28); transform: translateY(-2px);}
.pc-top {display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;}
.pc-emoji {width:54px; height:54px; border-radius:14px; display:flex; align-items:center;
  justify-content:center; font-size:1.9rem;
  background:linear-gradient(135deg,rgba(34,197,94,.20),rgba(250,204,21,.22));}
.pc-name {font-weight:650; font-size:1.0rem; line-height:1.25; margin:2px 0 4px 0;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; min-height:2.5em;}
.pc-meta {font-size:.8rem; opacity:.65; margin-bottom:2px;}
.pc-desc {font-size:.82rem; opacity:.75; display:-webkit-box; -webkit-line-clamp:2;
  -webkit-box-orient:vertical; overflow:hidden; min-height:2.4em; margin:4px 0 8px 0;}
.stars {color:#f59e0b; letter-spacing:1px; font-size:.95rem;}
.stars small {color:inherit; opacity:.7; letter-spacing:0; font-size:.8rem; margin-left:4px;}
.price {font-size:1.35rem; font-weight:750; letter-spacing:-.01em; line-height:1.9rem;}
.badge {display:inline-block; font-size:.7rem; font-weight:650; padding:2px 9px; border-radius:99px;
  margin-left:4px; white-space:nowrap;}
.badge.org {background:rgba(34,197,94,.18); color:#16a34a;}
.badge.cat {background:rgba(128,128,128,.16);}
.badge.top {background:rgba(245,158,11,.20); color:#d97706;}

/* ---------- misc polish ---------- */
.stButton>button, .stDownloadButton>button {border-radius:10px; font-weight:600;}
[data-testid="stChatMessage"] {border-radius:16px; padding:.8rem 1rem;}
[data-testid="stSidebar"] .block-container {padding-top:1.2rem;}
.welcome {border:1px dashed rgba(128,128,128,.4); border-radius:18px; padding:20px 24px; margin-bottom:12px;}
.welcome h3 {margin:0 0 6px 0; padding:0;}
.feat {font-size:.88rem; opacity:.8;}
.cart-line {font-size:.9rem; line-height:1.35;}
@media (max-width: 640px){ .hero{padding:18px;} .hero h1{font-size:1.45rem;} }
</style>
"""

EMOJI = [("honey", "🍯"), ("oil", "🫒"), ("nut", "🥜"), ("seed", "🌱"), ("grain", "🌾"), ("oat", "🌾"),
         ("rice", "🍚"), ("tea", "🍵"), ("coffee", "☕"), ("snack", "🍿"), ("dairy", "🥛"),
         ("milk", "🥛"), ("pasta", "🍝"), ("spice", "🌶️"), ("sauce", "🥫"), ("bakery", "🍞"),
         ("bread", "🍞"), ("fruit", "🍎"), ("veg", "🥦"), ("protein", "💪"), ("spread", "🥜"),
         ("syrup", "🍁"), ("meat", "🥩"), ("fish", "🐟"), ("drink", "🥤"), ("juice", "🧃"),
         ("chocolate", "🍫"), ("egg", "🥚"), ("cheese", "🧀")]
EXAMPLES = [
    "Organic honey under $15 rated 4.5+",
    "What's the best-rated coffee?",
    "Compare organic olive oil and avocado oil",
    "Healthy snacks under $10",
]


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def emoji_for(p: dict) -> str:
    text = f"{p.get('category', '')} {p.get('name', '')}".lower()
    for key, e in EMOJI:
        if key in text:
            return e
    return "🛍️"


def esc_md(text: str) -> str:
    """Escape $ so Streamlit doesn't treat prices as LaTeX."""
    return text.replace("$", r"\$")


def stars_html(rating: float, count: int) -> str:
    if not count:
        return '<span class="stars"><small>No reviews yet</small></span>'
    full = int(round(rating))
    return (f'<span class="stars">{"★" * full}{"☆" * (5 - full)}'
            f'<small>{rating:.1f} · {count:,} review{"s" if count != 1 else ""}</small></span>')


# --------------------------------------------------------------------------- cart state
def cart() -> dict:
    return st.session_state.setdefault("cart", {})


def cart_add(p: dict, qty: int = 1) -> None:
    c = cart()
    if p["id"] in c:
        c[p["id"]]["qty"] = min(99, c[p["id"]]["qty"] + qty)
    else:
        c[p["id"]] = {"name": p["name"], "price": p["price"], "qty": qty, "emoji": emoji_for(p)}
    st.toast(f"Added **{p['name']}** to your cart", icon="🛒")


def cart_change(pid: int, delta: int) -> None:
    c = cart()
    if pid in c:
        c[pid]["qty"] += delta
        if c[pid]["qty"] <= 0:
            del c[pid]


def cart_remove(pid: int) -> None:
    cart().pop(pid, None)


def cart_clear() -> None:
    cart().clear()


def cart_count() -> int:
    return sum(i["qty"] for i in cart().values())


def cart_total() -> float:
    return round(sum(i["qty"] * i["price"] for i in cart().values()), 2)


# --------------------------------------------------------------------------- components
def render_hero() -> None:
    k = _kpis(db.catalog_version())
    n, total = cart_count(), cart_total()
    cart_chip = (f'<span class="chip">🛒 <b>{n}</b> in cart · <b>${total:,.2f}</b></span>'
                 if n else '<span class="chip">🛒 Cart is empty</span>')
    st.markdown(
        f"""<div class="hero">
        <h1>🛒 {html.escape(APP_NAME)}</h1>
        <p>Tell me what you need. I'll search, compare ratings and reviews, and check you out.</p>
        <div class="chips">
          <span class="chip"><b>{k['products']:,}</b> products</span>
          <span class="chip"><b>{k['categories']}</b> categories</span>
          <span class="chip"><b>{k['reviews']:,}</b> reviews</span>
          <span class="chip">⭐ <b>{k['avg_rating']:.2f}</b> avg rating</span>
          {cart_chip}
        </div></div>""",
        unsafe_allow_html=True,
    )


@st.cache_data(show_spinner=False)
def _kpis(version: int) -> dict:
    return db.kpis()


def product_card(p: dict, key: str) -> None:
    e = html.escape
    badges = ""
    if p.get("is_organic"):
        badges += '<span class="badge org">🌿 Organic</span>'
    if p.get("rating", 0) >= 4.7 and p.get("review_count", 0) >= 3:
        badges += '<span class="badge top">Top rated</span>'
    meta = " · ".join(x for x in (p.get("brand"), p.get("category")) if x)
    with st.container(border=True, key=f"pc_{key}"):
        st.markdown(
            f"""<div class="pc-top"><div class="pc-emoji">{emoji_for(p)}</div><div>{badges}</div></div>
            <div class="pc-name" title="{e(p['name'])}">{e(p['name'])}</div>
            {stars_html(p.get('rating', 0), p.get('review_count', 0))}
            <div class="pc-meta">{e(meta)}</div>
            <div class="pc-desc">{e(p.get('description') or '')}</div>""",
            unsafe_allow_html=True)
        a, b = st.columns([1, 1.25], vertical_alignment="center")
        a.markdown(f'<div class="price">${p["price"]:,.2f}</div>', unsafe_allow_html=True)
        b.button("Add to cart", key=f"add_{key}", on_click=cart_add, args=(p,),
                 width="stretch", icon="🛒", type="primary")


def product_grid(products: list[dict], key: str, cols: int = 3) -> None:
    for start in range(0, len(products), cols):
        row = st.columns(cols)
        for col, p in zip(row, products[start:start + cols]):
            with col:
                product_card(p, f"{key}_{p['id']}")


def comparison_table(products: list[dict]) -> None:
    df = pd.DataFrame({
        p["name"]: {
            "Price": f"${p['price']:.2f}",
            "Rating": f"{p['rating']:.2f} ★" if p["review_count"] else "–",
            "Reviews": f"{p['review_count']:,}",
            "Organic": "Yes" if p["is_organic"] else "No",
            "Category": p["category"],
            "Brand": p.get("brand") or "–",
        } for p in products})
    st.dataframe(df, width="stretch")


def order_summary(order: dict, key: str) -> None:
    st.success(f"**Order #{order['order_id']} confirmed**: {order['item_count']} item(s), "
               f"total **${order['total']:.2f}**. Arrives in 3–5 business days.", icon="✅")
    df = pd.DataFrame(order["items"])[["name", "quantity", "unit_price", "line_total"]]
    df.columns = ["Item", "Qty", "Unit price", "Total"]
    st.dataframe(df, hide_index=True, width="stretch", column_config={
        "Unit price": st.column_config.NumberColumn(format="$%.2f"),
        "Total": st.column_config.NumberColumn(format="$%.2f")})
    st.download_button("Download receipt", exports.receipt_html(order),
                       file_name=f"receipt_{order['order_id']}.html", mime="text/html",
                       key=f"rcpt_{key}", icon="🧾")


@st.dialog("🎉 Order confirmed")
def order_dialog(order: dict) -> None:
    order_summary(order, f"dlg_{order['order_id']}")
    st.caption("Track all your orders in the **Orders** tab.")


def render_cart_sidebar() -> None:
    c = cart()
    st.markdown(f"### 🛒 Your cart{f' ({cart_count()})' if c else ''}")
    if not c:
        st.caption("Empty for now. Ask the assistant, or tap **Add to cart** on any product.")
        return
    for pid, it in list(c.items()):
        with st.container(border=True):
            st.markdown(
                f'<div class="cart-line">{it.get("emoji", "🛍️")} <b>{html.escape(it["name"])}</b><br>'
                f'${it["price"]:.2f} × {it["qty"]} = <b>${it["price"] * it["qty"]:.2f}</b></div>',
                unsafe_allow_html=True)
            a, b, d = st.columns(3)
            a.button("➖", key=f"dec_{pid}", on_click=cart_change, args=(pid, -1), width="stretch")
            b.button("➕", key=f"inc_{pid}", on_click=cart_change, args=(pid, 1), width="stretch")
            d.button("🗑️", key=f"del_{pid}", on_click=cart_remove, args=(pid,), width="stretch")
    st.markdown(f"**Subtotal: ${cart_total():,.2f}**")
    st.text_input("Name on order (optional)", key="cust_name", placeholder="e.g. Alex")
    if st.button("Place order", type="primary", width="stretch", icon="✅"):
        try:
            order = db.create_order(
                [{"product_id": pid, "quantity": it["qty"]} for pid, it in c.items()],
                st.session_state.get("cust_name"), source="cart")
        except ValueError as e:
            st.error(str(e))
        else:
            c.clear()
            st.session_state.last_order = order
            order_dialog(order)
    st.button("Clear cart", on_click=cart_clear, width="stretch", type="tertiary")
