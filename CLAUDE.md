# Deal Scanner

Personal scanner for underpriced used electronics (iPhone, Apple Watch, AirPods, iPad, MacBook,
consoles, GPUs, gaming PCs) in Czechia, for resale. Region: Ústí nad Orlicí (PSČ 56201), 60 km radius.

Pipeline (built step by step, see PROMPTS.md):
`sources (Bazoš, later FB) → SQLite → pricing rules (prices.json) → Gemini second opinion → Telegram`

## Rules
- Code, identifiers, comments, commit messages: **English**.
- Talk to the user: **Russian**.
- Texts for sellers (message templates, Gemini `message_cs`): **Czech**, formal "vy", short.
- `knowledge/KNOWLEDGE.md` is the domain knowledge base (buy rules, condition adjustments, scam patterns,
  page structure). When parser selectors change, update its "Техника парсинга" section.
- `knowledge/prices.json` is the price list (`market_price`, `liquidity`, `confidence`). Never change prices
  silently; price changes go through the user.
- Be polite to sources: sequential requests only, random 2–5 s pause, retries with backoff.
  Never parallelize requests to Bazoš. Never bypass FB login walls/captchas.
- Secrets only in `.env` (see `.env.example`); search config in `config.yaml`.
- Git: commit and push straight to `main` (the owner's standing choice); no feature branches or PRs unless asked.
  The host deploys from `main`, so run `pytest` before every push.

## Layout
- `scanner/config.py`: `Settings` (.env via pydantic-settings) + `load_config()` (config.yaml)
- `scanner/models.py`: `Listing`, `ListingDetail` (source-independent)
- `scanner/sources/bazos.py`: URL builder, pure parsers (`parse_search_page`, `parse_detail`,
  `parse_price`), `BazosClient` (polite async httpx client: `scan()`, `fetch_detail()`)
- `scanner/db.py`: SQLite `listings` (PK source+id, `status` new/alerted/hidden/contacted) and
  `price_history`; `Database.upsert()` returns NEW / PRICE_CHANGED / UNCHANGED
- `scanner/pricing.py`: rule-based evaluation, no LLM. `normalize()` titles, `identify()` category +
  model + attrs, `PriceList.match()` (price list items are parsed by the same `identify()`; unreadable
  model names go to `_ITEM_ALIASES`), `evaluate()` / `evaluate_many()` (metrics, stop words, accessory
  incl. foreign brands/words, too_cheap, scam_suspect, shop, stale, low_profit = below MIN_PROFIT is no
  candidate, `bundle_bonus()` for extra controllers, score 0–100 with a discount cap), `rank()`.
  Buy ceiling `max_buy_price()` is tiered by market price (`pricing.max_price_tiers`), first offer
  `offer_price()`; `max_price_rule_lines()` renders the rule for the Gemini prompt and must match
  KNOWLEDGE.md (a test checks it). Thresholds and distance bonus: `pricing:` in config.yaml
- `scanner/gemini.py`: Gemini second opinion (google-genai). `Verdict` = response schema,
  `GeminiReviewer.review()` (cache in `gemini_reviews` by source+id+price, per-model `RateLimiter` for
  RPM/TPM, RPD from `gemini_usage`, 429 backoff, daily quota → fallback model → "без ИИ"),
  `review_candidates()` (fetch description, re-check rules, then Gemini). After each answer
  `enforce_rules()` (max_price and offer_price recomputed by the tiered rule from Gemini's adjusted
  market price, buy above max → negotiate, stale + big discount → negotiate) and, when Gemini
  says the price list item is wrong, `_rematch()` by its model (one more request at most); such cases go
  to the `pricing_mismatches` table (`reviews` shows them). KNOWLEDGE.md is the system
  instruction. Models and limits: `GEMINI_*` in .env (explicit ids only, no `*-latest`); quota day is
  counted in `GEMINI_QUOTA_TZ` (Pacific Time, resets ~09:00 Prague). Other options: `gemini:` in config.yaml
- `scanner/pipeline.py`: one scan (`run_scan()`): Bazoš → DB → rules → Gemini → rows in `alerts`.
  Telegram gets only listings posted ≤ `notify.max_age_days` ago or with a price drop in the last
  `notify.price_drop_days`; stale ones never (only /top). Also watch alerts ("🔁", drop ≥ 5 %) and
  "gone" for contacted listings (detail page 404/410). Options: `notify:` in config.yaml
- `scanner/pc.py`: ready-built PCs. `is_pc_build()` (title), `PcSpecs` (Gemini response schema: hardware only,
  Gemini does not price), `value_pc()` = used part prices from `knowledge/components.json` (GPU from
  prices.json) × `pc_factor()` (1.0 modern / 0.9 rest / 0.8 old hardware), OEM −15 %, bundled monitor at full price, nearest CPU/size → low confidence, no CPU or GPU
  → not valued. The pipeline reads at most `notify.pc_specs_per_run` new PCs per run (cached in `pc_specs`),
  then `evaluate(..., match=pc_match())` applies the usual ceiling, offer and score
- `scanner/notify.py`: message texts (HTML), digest splitting
- `scanner/bot.py`: aiogram 3 bot + APScheduler in one process (`ScannerApp`): scan every 10 min +
  jitter, quiet hours (scan every 30 min, alerts → morning digest), delivery, handler wiring.
  Answers only TELEGRAM_CHAT_ID. `make_bot()` trusts SSL_CERT_FILE
- `scanner/menu.py`: what the bot shows. Bottom reply keyboard (`MAIN_KEYBOARD`), "/" command list
  (`BOT_COMMANDS`), inline screens edited in place: `render("<screen>")` for callbacks `m:<screen>:<args>`
  (home, status, top, card, skipped, logs, settings, tiers, radius, searches, rmsearch, prices, preport,
  pselect) and `act()` for `a:<action>:<args>` (pause/resume, tier ±1, radius, rmsearch, ptoggle, papply). Plain functions returning (HTML, keyboard)
- `scanner/config_edit.py`: line-level edits of config.yaml (radius, tier pct, searches; validated
  before writing) and market_price edits in prices.json (logged in `price_changes`)
- `scanner/overrides.py`: every Telegram edit goes through here: it edits the file and records it in
  `overrides` (with the repo value it replaced); `replay()` on bot start re-applies them after a redeploy,
  unless the repo changed that value since (the repo wins, the record is dropped)
- `scanner/learn.py`: self-learning price list. `record()` stores observations (high-confidence matches,
  last asking price per listing) and `unknown_models`; `check_sold()` checks detail pages of listings that
  left the results (404/410 = probable sale); `build_report()` proposes market prices (median of sales,
  else median asking × 95 %, capped ±8 %, rounded towards the current price; estimate items first);
  proposals are applied only by the owner's buttons (`apply()`, through overrides). Weekly job: `learn:`
  in config.yaml (Sunday 19:00), on demand: /prices, CLI `price-report`
- `scanner/logs.py`: console + rotating files `data/logs/scanner.log` (INFO) and `errors.log` (WARNING+);
  the bot shows the end of errors.log with /logs. The bot also sends "🤖 Бот запущен" with the version on start
  and a health alert when no scan succeeded for `notify.health_after_min` (twice the interval in quiet hours)
- `scanner/__main__.py`: CLI
- `tests/`: pytest; `tests/fixtures/` holds real Bazoš HTML

## Commands
```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
python -m scanner bot                   # Telegram bot + scheduler (the normal way to run it; = python main.py)
python -m scanner run-once --max-alerts 1  # one scan, alerts go to Telegram right away
python -m scanner run-once --dry        # scan all Bazoš searches, print ranked candidates and would-be alerts
python -m scanner run-once --dry --all  # also print matched listings that are not candidates (and why)
python -m scanner run-once --dry --no-ai # same, without Gemini (--ai-limit N: only top N candidates)
python -m scanner review --limit 5 --json  # Gemini for top DB candidates, print the JSON verdicts
python -m scanner review --ids 123 456 --refresh  # these listings first, ignoring cached reviews
python -m scanner reviews --skipped     # what Gemini rejected in 24 h, pricing mismatches, Gemini usage
python -m scanner evaluate --all        # re-evaluate DB listings offline (e.g. after editing prices.json)
python -m scanner save-fixtures         # refresh tests/fixtures from live Bazoš (when the layout changes)
python -m scanner price-report          # price proposals from the collected observations (= /prices)
pytest
```
README.md (Russian) is the user-facing guide: setup, hosting, bot commands, troubleshooting.
`knowledge/components.json` holds used component prices for PC valuation (not prices.json).
`data/` is runtime only (the DB; on the host a persistent volume is mounted there and hides its contents), so
reference files live in `knowledge/`. The DB lives in `data/scanner.db` (gitignored). `--dry` still writes listings and Gemini reviews to the
DB; it stores no alerts and sends nothing.
