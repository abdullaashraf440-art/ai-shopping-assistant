"""
Natural-language line parsing + catalog matching, with no LLM involved.

Used for (a) the bulk shopping-list tool and (b) the offline "basic mode" chat that keeps the
app useful when no API key is configured or the model is rate limited.

Understands lines such as:
    "2 x organic honey under $15"     "coffee 4.5+"      "almond milk, max 5"
    "best rated olive oil"            "non-organic oats"
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import db

_NUM = r"(\d+(?:\.\d+)?)"
_PRICE_PATTERNS = [
    rf"(?:under|below|less than|cheaper than|up to|at most|max(?:imum)?(?: price)?|<=?)\s*\$?\s*{_NUM}",
    rf"\$\s*{_NUM}\s*(?:or less|or under|max)",
]
_RATING_PATTERNS = [
    rf"(?:rated?|rating|stars?)\s*(?:of\s*)?(?:at least|over|above|>=?|≥)?\s*{_NUM}\s*\+?",
    rf"{_NUM}\s*\+?\s*(?:stars?|★)",
    rf"{_NUM}\s*\+(?!\w)",
    rf"(?:at least|over|above|>=?|≥)\s*{_NUM}\s*(?:stars?|rating)?",
]


@dataclass
class ParsedLine:
    raw: str
    query: str
    quantity: int = 1
    max_price: Optional[float] = None
    min_rating: Optional[float] = None
    organic: Optional[bool] = None
    best: bool = False

    def describe(self) -> str:
        bits = []
        if self.organic is True:
            bits.append("organic")
        if self.organic is False:
            bits.append("non-organic")
        if self.max_price is not None:
            bits.append(f"under ${self.max_price:g}")
        if self.min_rating is not None:
            bits.append(f"{self.min_rating:g}★+")
        return ", ".join(bits)


def parse_line(line: str) -> Optional[ParsedLine]:
    text = (line or "").strip()
    text = re.sub(r"^[\s\-\*\u2022\u2610\u2611\[\]x]*(?=\S)", "", text, count=0) if text[:1] in "-*•☐☑[" else text
    if not text or text.startswith("#"):
        return None
    raw = text
    qty = 1

    m = re.match(r"^(\d{1,2})\s*(?:x|×|\*)\s*", text, re.I) or re.match(r"^(\d{1,2})\s+(?=[a-z])", text, re.I)
    if m:
        qty = max(1, int(m.group(1)))
        text = text[m.end():]
    m = re.search(r"(?<![a-z])\s*[x×]\s*(\d{1,2})\s*$", text, re.I)
    if m:
        qty = max(1, int(m.group(1)))
        text = text[:m.start()]

    low = text.lower()
    organic = None
    if re.search(r"\b(non[- ]?organic|not organic|conventional)\b", low):
        organic = False
        low = re.sub(r"\b(non[- ]?organic|not organic|conventional)\b", " ", low)
    elif re.search(r"\borganic\b", low):
        organic = True
        low = re.sub(r"\borganic\b", " ", low)

    max_price = None
    for pat in _PRICE_PATTERNS:
        m = re.search(pat, low)
        if m:
            max_price = float(m.group(1))
            low = low[:m.start()] + " " + low[m.end():]
            break

    min_rating = None
    for pat in _RATING_PATTERNS:
        m = re.search(pat, low)
        if m and 0 < float(m.group(1)) <= 5:
            min_rating = float(m.group(1))
            low = low[:m.start()] + " " + low[m.end():]
            break

    low = re.sub(r"\b(dollars?|bucks?|usd)\b", " ", low)
    best = bool(re.search(r"\b(best|top|highest)\b", low))
    query = re.sub(r"[^a-z0-9'\- ]+", " ", low)
    query = " ".join(db.tokenize(query))
    return ParsedLine(raw=raw, query=query, quantity=qty, max_price=max_price,
                      min_rating=min_rating, organic=organic, best=best)


def search_for(p: ParsedLine, limit: int = 10, sort: Optional[str] = None, relax: bool = True) -> dict:
    return db.search_products(
        p.query, organic=p.organic, max_price=p.max_price, min_rating=p.min_rating,
        sort=sort or ("rating" if (p.best or p.min_rating) else "relevance"), limit=limit,
        relax=relax)


def match_lines(text: str) -> list[dict]:
    """Match each non-empty line to the best-rated relevant product."""
    out = []
    for line in text.splitlines():
        p = parse_line(line)
        if p is None:
            continue
        if not p.query and p.organic is None:
            out.append({"request": p.raw, "status": "not understood"})
            continue
        res = search_for(p, limit=10, sort="relevance", relax=False)
        cands = res["products"]
        row = {"request": p.raw, "quantity": p.quantity, "filters": p.describe(),
               "matches": res["total"]}
        if not cands:
            row.update(status="no match")
        else:
            ranked = sorted(cands, key=lambda c: (-c["rating"], -c["review_count"], c["price"]))
            best, alts = ranked[0], ranked[1:3]
            row.update(status="matched", product=best,
                       line_total=round(best["price"] * p.quantity, 2),
                       alternatives=[a["name"] for a in alts])
        out.append(row)
    return out


def basic_reply(prompt: str) -> dict:
    """Deterministic chat fallback. Returns {'text', 'products'}."""
    p = parse_line(prompt)
    if p is None or (not p.query and p.organic is None and p.max_price is None):
        return {"text": "I can search by product, price, rating and organic status. "
                        "Try: *organic honey under $15 rated 4.5+*.", "products": []}
    res = search_for(p, limit=9)
    total = res["total"]
    label = f" ({p.describe()})" if p.describe() else ""
    if total == 0:
        return {"text": f"I couldn't find anything for **{p.query or 'that'}**{label}. "
                        "Try loosening the price or rating.", "products": []}
    top = res["products"][0]
    text = (f"Found **{total}** match{'es' if total != 1 else ''} for **{p.query or 'your filters'}**"
            f"{label}. Top pick: **{top['name']}** at ${top['price']:.2f} "
            f"({top['rating']:.1f}★ from {top['review_count']} reviews).")
    if res["relaxed"]:
        text += " I matched any of your words since no product matched all of them."
    return {"text": text, "products": res["products"]}
