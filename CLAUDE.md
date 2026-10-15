# Deal Scanner

Personal scanner for underpriced used electronics (iPhone, Apple Watch, AirPods, iPad, MacBook,
consoles, GPUs, gaming PCs) in Czechia, for resale. Region: Ústí nad Orlicí (PSČ 56201), 60 km radius.

Pipeline (built step by step, see PROMPTS.md):
`sources (Bazoš, later FB) → SQLite → pricing rules (prices.json) → Gemini second opinion → Telegram`

## Rules
- Code, identifiers, comments, commit messages: **English**.
- Talk to the user: **Russian**.
- Texts for sellers (message templates, Gemini `message_cs`): **Czech**, formal "vy", short.
- `data/KNOWLEDGE.md` is the domain knowledge base (buy rules, condition adjustments, scam patterns,
  page structure). When parser selectors change, update its "Техника парсинга" section.
- `data/prices.json` is the price list (`market_price`, `liquidity`, `confidence`). Never change prices
  silently; price changes go through the user.
- Be polite to sources: sequential requests only, random 2–5 s pause, retries with backoff.
  Never parallelize requests to Bazoš. Never bypass FB login walls/captchas.
- Secrets only in `.env` (see `.env.example`); search config in `config.yaml`.

## Layout
- `scanner/config.py`: `Settings` (.env via pydantic-settings) + `load_config()` (config.yaml)
- `scanner/models.py`: `Listing`, `ListingDetail` (source-independent)
- `scanner/sources/bazos.py`: URL builder, pure parsers (`parse_search_page`, `parse_detail`,
  `parse_price`), `BazosClient` (polite async httpx client: `scan()`, `fetch_detail()`)
- `scanner/db.py`: SQLite `listings` (PK source+id, `status` new/alerted/hidden/contacted) and
  `price_history`; `Database.upsert()` returns NEW / PRICE_CHANGED / UNCHANGED
- `scanner/__main__.py`: CLI
- `tests/`: pytest; `tests/fixtures/` holds real Bazoš HTML

## Commands
```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
python -m scanner run-once --dry        # scan all Bazoš searches, print new / price-changed listings
python -m scanner run-once --dry --all  # also print already known listings
python -m scanner save-fixtures         # refresh tests/fixtures from live Bazoš (when the layout changes)
pytest
```
The DB lives in `data/scanner.db` (gitignored). `--dry` still writes to the DB; it only disables notifications.
