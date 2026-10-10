"""Reviews API: aggregated rating lookups (backed by the shared data layer)."""
import db


def get_product_rating(product_id: int) -> dict:
    """Return average rating and review count for a single product."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT AVG(rating), COUNT(*) FROM reviews WHERE product_id = ?", (product_id,)
        ).fetchone()
    avg = round(row[0], 2) if row and row[0] is not None else 0.0
    return {"product_id": product_id, "average_rating": avg, "review_count": row[1] if row else 0}


def get_ratings_for_products(product_ids: list[int]) -> list[dict]:
    """Return ratings for a list of product IDs in a single query."""
    if not product_ids:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT product_id, AVG(rating), COUNT(*) FROM reviews "
            f"WHERE product_id IN ({','.join('?' * len(product_ids))}) GROUP BY product_id",
            list(product_ids),
        ).fetchall()
    ratings = {r[0]: {"average_rating": round(r[1], 2), "review_count": r[2]} for r in rows}
    return [
        {
            "product_id": pid,
            "average_rating": ratings.get(pid, {}).get("average_rating", 0.0),
            "review_count": ratings.get(pid, {}).get("review_count", 0),
        }
        for pid in product_ids
    ]


if __name__ == "__main__":
    r = get_product_rating(1)
    print(f"Product {r['product_id']}: {r['average_rating']} stars ({r['review_count']} reviews)")
    for r in get_ratings_for_products([1, 3, 5, 7]):
        print(f"  Product {r['product_id']}: {r['average_rating']} stars ({r['review_count']} reviews)")
