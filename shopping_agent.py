"""
LangChain shopping agent.

What changed vs. the original
- The model is created lazily, so the app starts (and degrades gracefully) without an API key.
- search_products now takes price range / category / min rating / sort and returns ratings in the
  same call (no more one get_rating call per product), backed by the FTS5 search in db.py.
- New tools: get_reviews, compare_products, list_categories; checkout supports several items
  and quantities.
- run_agent() streams progress to the UI and returns structured results (product cards,
  comparison, order) in addition to the model's text.
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field

import config
import db
from reviews_api import get_product_rating

SYSTEM_PROMPT = """You are FreshCart AI, a friendly, precise shopping assistant for an online grocery store.

The app automatically shows product cards (photo tile, price, rating, Add-to-cart button) under your
reply for every product you retrieve. So DO NOT re-list product details in a table or long list.
Write a short, warm answer (2-5 sentences): name your top pick(s) as "Name (ID:X)", say why
(rating, price, organic, value), and mention trade-offs or an alternative when useful.

SEARCHING
- Use search_products. Put only the product keywords in `query` (e.g. "honey", "olive oil"); express
  price limits, organic, rating and category with the dedicated arguments, not in the query text.
- Use sort_by="rating" for "best"/"top rated", "price_low" for "cheapest". Default limit is fine.
- If a search returns nothing, retry ONCE with fewer filters (drop min_rating, category, or organic)
  and tell the user what you relaxed. If nothing relevant exists, say so honestly. Never suggest
  unrelated products just to fill the answer.
- Use get_reviews when asked what customers say, compare_products for side-by-side questions, and
  list_categories when the user wants to browse or you are unsure what the store sells.

IMAGE SEARCH - when the user message contains an image path:
1. Call describe_product_image with the path.
2. Use its search_query / is_organic with search_products and continue as above.
3. If the image is not a product we could sell (e.g. an animal), say no matching products exist.

ORDERING
- NEVER call checkout unless the user explicitly confirms (e.g. "yes", "order it", "buy #2",
  "get me two of the first one"). Never guess a product ID; use the (ID:X) from the conversation,
  including the "[Products shown: ...]" notes. Confirm quantities when ambiguous.
- After checkout succeeds, confirm the order number and total in one or two sentences.

Be concise, never invent products, prices or ratings, and use plain text (no tables, no code blocks)."""


# --------------------------------------------------------------------------- models
_llm = None
_vision_llm = None


def get_llm():
    global _llm
    if _llm is None:
        from langchain_groq import ChatGroq
        _llm = ChatGroq(model=config.TEXT_MODEL, temperature=0)
    return _llm


def get_vision_llm():
    global _vision_llm
    if _vision_llm is None:
        from langchain_groq import ChatGroq
        _vision_llm = ChatGroq(model=config.VISION_MODEL, temperature=0)
    return _vision_llm


def llm_available() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


# --------------------------------------------------------------------------- tools
def _compact(p: dict) -> dict:
    d = dict(p)
    d["description"] = (d.get("description") or "")[:140]
    d["rating"] = round(d["rating"], 2)
    return d


@tool
def search_products(
    query: str = "",
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    is_organic: Optional[bool] = None,
    min_rating: Optional[float] = None,
    sort_by: Literal["relevance", "rating", "price_low", "price_high"] = "relevance",
    limit: int = 8,
) -> str:
    """
    Search the store catalog. `query` holds product keywords only (e.g. "honey"); use the other
    arguments for filters. Each result already includes its average rating and review count.
    Returns JSON: {total, showing, relaxed, products:[{id,name,category,brand,price,rating,
    review_count,is_organic,description}]}.
    """
    res = db.search_products(
        query, category=category, min_price=min_price, max_price=max_price, organic=is_organic,
        min_rating=min_rating, sort=sort_by, limit=max(1, min(limit, 12)))
    out = {"total": res["total"], "showing": len(res["products"]), "relaxed": res["relaxed"],
           "products": [_compact(p) for p in res["products"]]}
    if res["relaxed"]:
        out["note"] = "No product matched all keywords; these match some of them."
    return json.dumps(out)


@tool
def get_rating(product_id: int) -> str:
    """Get the average customer rating and total review count for one product ID."""
    return json.dumps(get_product_rating(product_id))


@tool
def get_reviews(product_id: int, limit: int = 5) -> str:
    """Get recent customer reviews, star distribution and rating summary for one product ID."""
    return json.dumps(db.get_reviews(product_id, limit))


@tool
def compare_products(product_ids: list[int]) -> str:
    """Compare 2-4 products side by side (price, rating, reviews, organic, category) by their IDs."""
    prods = db.get_products(product_ids[:4])
    if len(prods) < 2:
        return json.dumps({"error": "Need at least two valid product IDs to compare."})
    return json.dumps({"products": [_compact(p) for p in prods]})


@tool
def list_categories() -> str:
    """List store categories with product counts, average price and average rating."""
    return json.dumps(db.categories())


class OrderItem(BaseModel):
    product_id: int = Field(description="Product ID taken from a previous search result")
    quantity: int = Field(default=1, ge=1, le=99, description="How many units")


@tool
def checkout(items: list[OrderItem], customer_name: Optional[str] = None) -> str:
    """
    Place an order for one or more products. ONLY call after the user explicitly confirmed.
    Saves the order and returns JSON with order_id, items, total.
    """
    try:
        norm = []
        for it in items:
            if isinstance(it, dict):
                norm.append({"product_id": it.get("product_id"), "quantity": it.get("quantity", 1)})
            else:
                norm.append({"product_id": it.product_id, "quantity": it.quantity})
        order = db.create_order(norm, customer_name, source="chat")
        order["message"] = (f"Order #{order['order_id']} confirmed. Total ${order['total']:.2f}. "
                            "Arrives in 3-5 business days.")
        return json.dumps(order)
    except ValueError as e:
        return json.dumps({"error": str(e)})


def _safe_image_path(image_path: str) -> Path:
    p = Path(image_path).resolve()
    root = config.UPLOAD_DIR.resolve()
    if root not in p.parents or not p.is_file():
        raise ValueError("Image not found in the upload folder.")
    return p


@tool
def describe_product_image(image_path: str) -> str:
    """
    Analyze an uploaded product photo and return its attributes as JSON (product_type,
    search_query, is_organic, description). Use the result with search_products.
    """
    try:
        path = _safe_image_path(image_path)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    data = base64.b64encode(path.read_bytes()).decode()
    ext = path.suffix.lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext or 'jpeg'}"
    message = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
        {"type": "text", "text": (
            "Look at this product image and extract its key attributes. Return ONLY a JSON object "
            "with these fields:\n"
            "- product_type: what kind of product it is (e.g. honey, olive oil, almonds)\n"
            "- search_query: a short keyword to search for it (e.g. 'honey', 'olive oil')\n"
            "- is_organic: true if the label says organic, false if not, null if unclear\n"
            "- description: one sentence describing the product")},
    ])
    text = get_vision_llm().invoke([message]).content
    text = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.M).strip()
    return text


TOOLS = [search_products, get_rating, get_reviews, compare_products, list_categories,
         checkout, describe_product_image]


# --------------------------------------------------------------------------- agent
_agent = None


def build_agent(llm=None):
    return create_agent(tools=TOOLS, model=llm or get_llm(), system_prompt=SYSTEM_PROMPT)


def get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


STATUS_LABELS = {
    "search_products": "🔎 Searching the catalog…",
    "get_rating": "⭐ Checking ratings…",
    "get_reviews": "💬 Reading customer reviews…",
    "compare_products": "⚖️ Comparing products…",
    "list_categories": "🗂️ Browsing categories…",
    "checkout": "🧾 Placing your order…",
    "describe_product_image": "🖼️ Analyzing your image…",
}


@dataclass
class AgentResult:
    text: str = ""
    products: list = field(default_factory=list)
    comparison: list = field(default_factory=list)
    order: Optional[dict] = None
    steps: list = field(default_factory=list)


_THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


def clean_text(content) -> str:
    if isinstance(content, list):
        content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return _THINK.sub("", str(content or "")).replace("`", "").strip()


def _load(msg: ToolMessage):
    try:
        data = json.loads(msg.content if isinstance(msg.content, str) else str(msg.content))
        return data
    except (ValueError, TypeError):
        return None


def run_agent(messages: list[dict], on_status: Optional[Callable[[str], None]] = None,
              agent=None) -> AgentResult:
    """Run one agent turn over `messages` ([{'role','content'}]) and collect structured output."""
    agent = agent or get_agent()
    result, seen_ids, final = AgentResult(), set(), ""

    for update in agent.stream({"messages": messages[-config.MAX_CHAT_HISTORY:]},
                               config={"recursion_limit": 40}, stream_mode="updates"):
        for payload in update.values():
            if not isinstance(payload, dict):
                continue
            for m in payload.get("messages") or []:
                if isinstance(m, AIMessage):
                    for call in m.tool_calls or []:
                        label = STATUS_LABELS.get(call["name"], "⚙️ Working…")
                        result.steps.append(label)
                        if on_status:
                            on_status(label)
                    if not m.tool_calls:
                        final = clean_text(m.content) or final
                elif isinstance(m, ToolMessage):
                    data = _load(m)
                    if not isinstance(data, dict):
                        continue
                    if m.name == "search_products":
                        for p in data.get("products", []):
                            if p["id"] not in seen_ids:
                                seen_ids.add(p["id"])
                                result.products.append(p)
                    elif m.name == "compare_products" and data.get("products"):
                        result.comparison = data["products"]
                        for p in data["products"]:
                            if p["id"] not in seen_ids:
                                seen_ids.add(p["id"])
                                result.products.append(p)
                    elif m.name == "checkout" and data.get("order_id"):
                        result.order = data

    result.products = result.products[:12]
    result.text = final or ("Here's what I found." if result.products else
                            "Sorry, I couldn't complete that. Please try rephrasing.")
    return result


def history_note(products: list[dict]) -> str:
    """Hidden context appended to assistant turns so 'order the first one' resolves to real IDs."""
    if not products:
        return ""
    return "\n\n[Products shown: " + "; ".join(f"{p['name']} (ID:{p['id']}, ${p['price']:.2f})"
                                             for p in products[:8]) + "]"


if __name__ == "__main__":
    out = run_agent([{"role": "user", "content": "I want organic honey under $20 rated 4.5+"}])
    print(out.text)
    for p in out.products:
        print(f"  #{p['id']} {p['name']} ${p['price']:.2f} {p['rating']}★")
