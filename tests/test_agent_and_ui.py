"""Agent loop + headless UI flows, using a scripted model (no network, no API key)."""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from scripted_model import ScriptedModel, ai, call

APP = str(Path(__file__).resolve().parents[1] / "app.py")


# ----------------------------------------------------------------------------- agent
def test_run_agent_collects_cards_and_strips_reasoning(store):
    import shopping_agent as sa
    model = ScriptedModel(script=[
        ai("", [call("search_products", query="honey", is_organic=True, max_price=20, min_rating=4.5, sort_by="rating")]),
        ai("<think>secret</think>Best pick: Organic Acacia Honey (ID:7)."),
    ])
    seen = []
    r = sa.run_agent([{"role": "user", "content": "x"}], on_status=seen.append,
                     agent=sa.build_agent(model))
    assert r.text == "Best pick: Organic Acacia Honey (ID:7)."
    assert r.products[0]["id"] == 7 and seen == ["🔎 Searching the catalog…"]


def test_checkout_tool_creates_order_with_quantity(store):
    import shopping_agent as sa
    model = ScriptedModel(script=[ai("", [call("checkout", items=[{"product_id": 7, "quantity": 2}])]),
                                  ai("Done")])
    r = sa.run_agent([{"role": "user", "content": "yes"}], agent=sa.build_agent(model))
    assert r.order["total"] == pytest.approx(35.98) and store.kpis()["orders"] == 1


def test_checkout_tool_reports_unknown_product(store):
    import shopping_agent as sa
    out = json.loads(sa.checkout.invoke({"items": [{"product_id": 12345, "quantity": 1}]}))
    assert "error" in out and store.kpis()["orders"] == 0


def test_image_tool_refuses_paths_outside_upload_dir(store):
    import shopping_agent as sa
    out = json.loads(sa.describe_product_image.invoke({"image_path": "/etc/passwd"}))
    assert "error" in out


# ----------------------------------------------------------------------------- UI
@pytest.fixture()
def app(store, monkeypatch):
    monkeypatch.setenv("STORE_DB_PATH", store.DB_PATH)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return AppTest.from_file(APP, default_timeout=60)


def test_every_view_renders_without_errors(app):
    at = app.run()
    assert not at.exception
    for view in ["🛍️ Browse", "📝 Bulk list", "📦 Orders", "📊 Insights", "🗄️ Data", "💬 Assistant"]:
        at.session_state["view"] = view
        at.run()
        assert not at.exception, view


def test_basic_mode_chat_cart_checkout(app):
    at = app.run()
    assert any("Basic mode" in i.value for i in at.info)
    at.chat_input[0].set_value("organic honey under $20 rated 4.5+").run()
    cards = [b for b in at.button if b.label == "Add to cart"]
    assert len(cards) == 4
    cards[0].click().run()
    assert at.session_state["cart"]
    [b for b in at.button if b.label == "Place order"][0].click().run()
    assert not at.session_state["cart"] and at.session_state["last_order"]["order_id"] == 1


def test_browse_filters_and_table_mode(app):
    at = app.run()
    at.session_state["view"] = "🛍️ Browse"
    at.run()
    at.text_input(key="b_q").set_value("coffee").run()
    assert not at.exception
    assert any("**3** products" in m.value for m in at.markdown)
    at.session_state["b_mode"] = "Table"
    at.run()
    assert not at.exception and len(at.dataframe) >= 1


def test_ai_mode_end_to_end_and_failure_fallback(app, monkeypatch):
    import shopping_agent as sa
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setattr(sa, "_agent", sa.build_agent(ScriptedModel(script=[
        ai("", [call("compare_products", product_ids=[9, 12])]), ai("Olive oil wins on price.")])))
    at = app.run()
    assert not any("Basic mode" in i.value for i in at.info)
    at.chat_input[0].set_value("compare olive and avocado oil").run()
    last = at.session_state["messages"][-1]
    assert last["content"] == "Olive oil wins on price." and len(last["comparison"]) == 2

    class Boom(ScriptedModel):
        def _generate(self, *a, **k):
            raise RuntimeError("429 rate limit")
    monkeypatch.setattr(sa, "_agent", sa.build_agent(Boom(script=[ai("x")])))
    at.chat_input[0].set_value("honey").run()
    last = at.session_state["messages"][-1]
    assert "busy" in last["notice"] and last["products"] and not at.exception
