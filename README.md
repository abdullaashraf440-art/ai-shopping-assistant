# 🛒 FreshCart AI: Shopping Assistant

A conversational shopping assistant built with **Streamlit**, **LangChain** and **Groq**. Chat to find products,
compare ratings and reviews, shop by photo, bulk-match a whole shopping list, and check out, then import
your own catalog and export the results.

## What's new in this version
**Experience**
- Polished UI: hero header, product **cards** (emoji tile, stars, organic / top-rated badges, Add to cart),
  live **cart** in the sidebar with quantities, order confirmation dialog and downloadable **receipts**
- Six views: 💬 Assistant · 🛍️ Browse · 📝 Bulk list · 📦 Orders · 📊 Insights · 🗄️ Data
- Live progress while the AI works ("Searching the catalog…", "Reading reviews…"), one-tap example prompts
- Graceful degradation: no API key or rate-limited → **Basic mode** (deterministic search with price / rating /
  organic filters) instead of a crash

**Data processing**
- **Import** products and reviews from CSV / TSV / Excel / JSON / JSONL, with auto column mapping, per-row
  validation, an error report and idempotent upserts (up to 500k rows)
- **Fast search at scale**: SQLite FTS5 (ranking, stemming, prefix) + cached ratings → ~4 ms over 20,000 products
- **Bulk shopping list**: paste or upload dozens of lines (`2 x organic honey under $15`) and get the best match per line
- **Exports**: CSV / styled Excel for products, reviews, orders, shopping lists; HTML receipts
- **Insights** dashboard: ratings and prices by category, rating distribution, best value, revenue
- **Demo generator**: add up to 50,000 synthetic products and reviews to see it at scale

**Agent upgrades**
- Search returns ratings in the same call (previously one tool call per product), with price range, category,
  min-rating and sort filters; new tools: reviews, side-by-side compare, categories, multi-item checkout
- The old search only matched the exact phrase, so "organic honey" missed "Organic Raw Honey"; fixed
- Image tool restricted to the upload folder; `setup_db.py` no longer wipes reviews on re-run

## Quick start
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # add GROQ_API_KEY (free at https://console.groq.com)
streamlit run app.py
```
The database is created and seeded on first launch. Without a key the app runs in Basic mode.

Try: `sample_data/products_sample.csv` and `reviews_sample.csv` in **Data → Import**, then paste
`sample_data/shopping_list.txt` into **Bulk list**.

## Deploy
**Streamlit Community Cloud**: push to GitHub, create the app from `app.py`, and add `GROQ_API_KEY` under
*Settings → Secrets*. Its filesystem is ephemeral, so data resets on restart; use Docker/a VM for persistence.

**Docker** (persistent data volume):
```bash
cp .env.example .env && docker compose up -d --build     # http://localhost:8501
```
Set `STORE_DB_PATH` to a mounted volume path. SQLite suits a single instance; for multi-instance
deployments move the data layer (`db.py`) to Postgres.

## Project structure
| File | Purpose |
|---|---|
| `app.py` | Entry point: bootstrap, sidebar (cart, image search), navigation |
| `pages.py` | The six views |
| `ui.py` | Theme CSS, hero, product cards, cart, receipts |
| `shopping_agent.py` | LangChain agent, tools, streaming runner, structured results |
| `db.py` | Schema + migrations, FTS5 search, orders, analytics queries |
| `data_import.py` | File parsing, column detection, validation, bulk import |
| `shopping_list.py` | Line parser + matcher (also powers Basic mode) |
| `demo_data.py` · `exports.py` | Demo catalog generator · CSV/Excel/receipt output |
| `setup_db.py` · `reviews_api.py` | Seed data (`--reset` to wipe) · rating lookups |
| `tests/` | 37 tests (pytest): search, imports, migration, agent loop, UI flows |

## Tests
```bash
pip install -r requirements-dev.txt && pytest -q
```
Tests use a scripted model, so they need no API key or network.

## Example prompts
- "I want organic honey under $15 with 4+ rating"
- "Compare the organic olive oil and avocado oil"
- "What do customers say about the Manuka honey?"
- "Order two of the first one"
- Upload `sample_images/honey.png` (match) or `elephant.png` (no match) in the sidebar
