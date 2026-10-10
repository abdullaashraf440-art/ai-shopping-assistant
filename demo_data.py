"""Generate a realistic large demo catalog (up to ~50k products) for scale testing and demos."""
from __future__ import annotations

import random

import db

BRANDS = ["Green Valley", "Nature's Pantry", "Sunrise Farms", "Golden Acre", "Wild Harvest",
          "Maple & Oak", "Blue Meadow", "Terra Foods", "Harvest Moon", "Northwoods",
          "Olive Grove", "Stone Mill", "Fieldstone", "Pure Roots", "Coastal Kitchen"]
SIZES = ["250g", "500g", "1kg", "12 oz", "16 oz", "2 lb", "family size", "value pack"]

# category -> (base price, [product kinds], [flavour/variety adjectives])
CATALOG = {
    "honey": (11, ["Honey"], ["Raw", "Wildflower", "Clover", "Manuka", "Acacia", "Buckwheat", "Creamed"]),
    "oil": (13, ["Olive Oil", "Avocado Oil", "Sesame Oil", "Walnut Oil"], ["Extra Virgin", "Cold-Pressed", "Toasted", "Light"]),
    "nuts": (10, ["Almonds", "Cashews", "Pistachios", "Walnuts", "Pecans", "Hazelnuts"], ["Roasted", "Raw", "Salted", "Unsalted", "Smoked"]),
    "grains": (7, ["Quinoa", "Brown Rice", "Rolled Oats", "Barley", "Millet", "Farro"], ["Whole Grain", "Gluten-Free", "Sprouted", "Steel-Cut"]),
    "tea": (9, ["Green Tea", "Black Tea", "Chamomile Tea", "Peppermint Tea", "Rooibos"], ["Loose Leaf", "Ceremonial", "Decaf", "Blend"]),
    "coffee": (14, ["Coffee Beans", "Ground Coffee", "Espresso Blend", "Cold Brew Coffee"], ["Ethiopian", "Colombian", "Dark Roast", "Medium Roast", "Single-Origin"]),
    "snacks": (6, ["Granola", "Trail Mix", "Rice Cakes", "Energy Bars", "Dried Mango", "Seaweed Snack"], ["Crunchy", "Sweet", "Spicy", "Low-Sugar"]),
    "dairy-alt": (4, ["Almond Milk", "Oat Milk", "Soy Milk", "Coconut Milk", "Cashew Milk"], ["Unsweetened", "Barista", "Vanilla", "Original"]),
    "pasta": (4, ["Spaghetti", "Penne", "Fusilli", "Lasagna Sheets"], ["Whole Wheat", "Durum", "Chickpea", "Bronze-Cut"]),
    "spices": (5, ["Turmeric", "Paprika", "Black Pepper", "Cumin", "Cinnamon", "Chili Flakes"], ["Ground", "Whole", "Smoked", "Fair Trade"]),
    "sauces": (6, ["Tomato Sauce", "Hot Sauce", "Pesto", "Soy Sauce", "Salsa"], ["Classic", "Spicy", "Garlic", "Low-Sodium"]),
    "bakery": (5, ["Sourdough Loaf", "Whole Wheat Bread", "Bagels", "Tortillas"], ["Seeded", "Rustic", "Multigrain"]),
    "fruit": (6, ["Dried Apricots", "Dried Figs", "Raisins", "Dates", "Cranberries"], ["Unsweetened", "Sun-Dried", "Premium"]),
    "protein": (18, ["Pea Protein", "Whey Protein", "Hemp Protein", "Collagen Peptides"], ["Vanilla", "Chocolate", "Unflavored"]),
    "spreads": (7, ["Peanut Butter", "Almond Butter", "Tahini", "Hazelnut Spread", "Fruit Jam"], ["Crunchy", "Smooth", "No-Sugar-Added"]),
}
POSITIVE = ["Excellent quality, will buy again.", "Fresh and tasty.", "Great value for the price.",
            "Exactly as described.", "My whole family loves it.", "Best I've tried.",
            "Arrived quickly and well packed.", "Perfect for everyday use."]
NEUTRAL = ["Decent, nothing special.", "Okay for the price.", "Average quality.", "Fine but a bit pricey."]
NEGATIVE = ["Not as fresh as I hoped.", "Flavor was underwhelming.", "Packaging was damaged."]
FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Casey", "Morgan", "Riley", "Jamie", "Avery", "Quinn",
         "Drew", "Blake", "Rowan", "Skyler", "Parker", "Reese"]


def generate_demo_catalog(n_products: int = 1000, max_reviews: int = 12, seed: int = 42) -> dict:
    """Append n_products synthetic products (+ reviews). Returns counts."""
    rng = random.Random(seed)
    cats = list(CATALOG)
    with db.get_conn() as conn:
        start = (conn.execute("SELECT COALESCE(MAX(id),0) FROM products").fetchone()[0]) + 1
        products, reviews, seen = [], [], set()
        pid = start
        attempts = 0
        while len(products) < n_products and attempts < n_products * 20:
            attempts += 1
            cat = cats[attempts % len(cats)] if attempts <= len(cats) * 2 else rng.choice(cats)
            base, kinds, adjs = CATALOG[cat]
            kind, adj, brand = rng.choice(kinds), rng.choice(adjs), rng.choice(BRANDS)
            organic = rng.random() < 0.4
            size = rng.choice(SIZES)
            name = f"{brand} {'Organic ' if organic else ''}{adj} {kind} {size}"
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            price = round(max(1.49, base * rng.uniform(0.55, 2.4) * (1.25 if organic else 1)) - 0.01, 2)
            desc = (f"{'Certified organic ' if organic else ''}{adj.lower()} {kind.lower()} from "
                    f"{brand}. {size.capitalize()}.")
            products.append((pid, name, cat, price, desc, int(organic), brand))
            quality = rng.gauss(4.1, 0.55) + (0.15 if organic else 0)
            for _ in range(rng.randint(0, max_reviews)):
                rating = min(5.0, max(1.0, round(rng.gauss(quality, 0.8) * 2) / 2))
                pool = POSITIVE if rating >= 4 else NEUTRAL if rating >= 3 else NEGATIVE
                reviews.append((pid, rating, rng.choice(FIRST), rng.choice(pool)))
            pid += 1
        conn.executemany(
            "INSERT INTO products (id, name, category, price, description, is_organic, brand) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", products)
        db.insert_reviews(conn, reviews)
        db.finalize(conn)
    return {"products": len(products), "reviews": len(reviews)}
