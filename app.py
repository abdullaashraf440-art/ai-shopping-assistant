"""FreshCart AI: Streamlit entry point.   Run with:  streamlit run app.py"""
import os

import streamlit as st

import config

# --- page config must be the first Streamlit call -------------------------------------------
st.set_page_config(page_title=f"{config.APP_NAME} · AI Shopping Assistant", page_icon="🛒",
                   layout="wide", initial_sidebar_state="expanded")

# --- secrets: works with .env, real env vars, and Streamlit Cloud secrets --------------------
try:
    if not os.getenv("GROQ_API_KEY") and "GROQ_API_KEY" in st.secrets:
        os.environ["GROQ_API_KEY"] = st.secrets["GROQ_API_KEY"]
except Exception:  # no secrets file configured: that's fine
    pass

import db  # noqa: E402
import pages  # noqa: E402
import setup_db  # noqa: E402
import ui  # noqa: E402


@st.cache_resource(show_spinner="Setting up the store…")
def bootstrap() -> bool:
    """Create/migrate the database and seed demo data on first launch (once per process)."""
    config.ensure_dirs()
    setup_db.create_database()
    return True


bootstrap()
ui.inject_css()

VIEWS = {
    "💬 Assistant": pages.render_assistant,
    "🛍️ Browse": pages.render_browse,
    "📝 Bulk list": pages.render_bulk,
    "📦 Orders": pages.render_orders,
    "📊 Insights": pages.render_insights,
    "🗄️ Data": pages.render_data,
}

# --- sidebar: cart + shop by image --------------------------------------------------------------
with st.sidebar:
    ui.render_cart_sidebar()
    st.divider()
    st.markdown("### 📷 Shop by image")
    if pages.ai_enabled():
        st.caption("Upload a product photo and I'll find similar items.")
        up = st.file_uploader("Product photo", type=["jpg", "jpeg", "png", "webp"], key="img_up",
                              label_visibility="collapsed")
        if up:
            st.image(up, width="stretch")
            st.button("Find similar products", on_click=pages.queue_image, type="primary",
                      width="stretch", icon="🔎")
    else:
        st.caption("Image search needs a `GROQ_API_KEY`.")

# --- main -----------------------------------------------------------------------------------------
ui.render_hero()
view = st.segmented_control("Navigation", list(VIEWS), default="💬 Assistant", key="view",
                            label_visibility="collapsed") or "💬 Assistant"
VIEWS[view]()
