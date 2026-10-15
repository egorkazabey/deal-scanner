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
- `scanner/pricing.py`: rule-based evaluation, no LLM. `normalize()` titles, `identify()` category +
  model + attrs, `PriceList.match()` (price list items are parsed by the same `identify()`; unreadable
  model names go to `_ITEM_ALIASES`), `evaluate()` / `evaluate_many()` (metrics, stop words, accessory,
  too_cheap, scam_suspect, shop, score 0–100), `rank()`. Thresholds and distance bonus: `pricing:` in config.yaml
- `scanner/gemini.py`: Gemini second opinion (google-genai). `Verdict` = response schema,
  `GeminiReviewer.review()` (cache in `gemini_reviews` by source+id+price, per-model `RateLimiter` for
  RPM/TPM, RPD from `gemini_usage`, 429 backoff, daily quota → fallback model → "без ИИ"),
  `review_candidates()` (fetch description, re-check rules, then Gemini). KNOWLEDGE.md is the system
  instruction. Models and limits: `GEMINI_*` in .env (explicit ids only, no `*-latest`); quota day is
  counted in `GEMINI_QUOTA_TZ` (Pacific Time, resets ~09:00 Prague). Other options: `gemini:` in config.yaml
- `scanner/__main__.py`: CLI
- `tests/`: pytest; `tests/fixtures/` holds real Bazoš HTML

## Commands
```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
python -m scanner run-once --dry        # scan all Bazoš searches, print ranked candidates
python -m scanner run-once --dry --all  # also print matched listings that are not candidates (and why)
python -m scanner run-once --dry --no-ai # same, without Gemini (--ai-limit N: only top N candidates)
python -m scanner review --limit 5 --json  # Gemini for top DB candidates, print the JSON verdicts
python -m scanner reviews --skipped     # what Gemini rejected in the last 24 h (+ today's Gemini usage)
python -m scanner evaluate --all        # re-evaluate DB listings offline (e.g. after editing prices.json)
python -m scanner save-fixtures         # refresh tests/fixtures from live Bazoš (when the layout changes)
pytest
```
The DB lives in `data/scanner.db` (gitignored). `--dry` still writes to the DB; it only disables notifications.
