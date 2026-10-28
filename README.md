# Deal Scanner

A personal scanner for underpriced used electronics (iPhone, Apple Watch, AirPods, iPad, MacBook, consoles,
graphics cards, ready-built PCs) on Bazoš and Sbazar, within 60 km of Ústí nad Orlicí, Czechia, and on Vinted
(shipped, the whole country and foreign sellers). It finds listings
below market price, gets a second opinion from Gemini and sends them to Telegram with a ready-made
message for the seller (in Czech).

```
Bazoš, Sbazar, Vinted (FB optional) → SQLite → rules (knowledge/prices.json) → Gemini → Telegram
```

The bot's interface is in Russian; button and command names below are quoted as they appear in the bot.

## Setup (local)

Python 3.12+ is required.

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip install -e ".[dev]"
cp .env.example .env                   # then fill in .env
python -m scanner run-once --dry       # one scan without sending anything, to check that it all works
python main.py                         # bot + scheduler (same as python -m scanner bot)
```

### .env

| Variable | What it is |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from @BotFather |
| `TELEGRAM_CHAT_ID` | your chat id: send the bot `/start`, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, take `chat.id` |
| `GEMINI_API_KEY` | key from aistudio.google.com |
| `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` | exact model ids (no `*-latest`) |
| `GEMINI_RPM`, `GEMINI_RPD`, `GEMINI_TPM` | our own limits, about half of the free tier |

The bot answers only `TELEGRAM_CHAT_ID` and silently ignores everyone else.

## Hosting

- Entry file: `main.py` (or the command `python main.py`). Build: `pip install .`.
- Service type: **worker / background**, not web: the bot opens no HTTP port.
- Set the variables above in the host's panel (there is no `.env` in the repository).
- Mount the persistent disk at `/app/data`: it holds the database `scanner.db` and the logs in `logs/`.
  Reference files live in `knowledge/`, not in `data/`, because the mounted disk hides whatever the
  repository has in `data/`.
- **Redeploy after every push to `main`**: the host may not rebuild on its own. To check: `/status` shows
  `версия <commit> · прайс N позиций`, and the bot sends «🤖 Бот запущен» after each start.
- Run **one** instance. Two bots with the same token get in each other's way (`Conflict: terminated by
  other getUpdates request` in the log).

A home PC works too: `python main.py` on autostart (Windows: Task Scheduler, trigger "At log on", start
in = the project folder).

## How it works

- A scan every 10 minutes (plus a random 0 to 90 s). Quiet hours 00:30 to 06:30: a scan every 30 minutes,
  alerts are collected into one morning digest.
- Candidate: the model is in the price list, the price is 40 to 90 % of market, the profit is at least
  1 000 Kč (2 000 for PCs, graphics cards and MacBooks).
- Alerts only for listings posted in the last 3 days or with a fresh price drop. Stale listings (older
  than 14 days) show up only in `/top`.
- The buy ceiling (`максимум`) is tiered by market price; the first offer is min(92 % of the asking
  price, ceiling − 5 %). The rule lives in `config.yaml` (`pricing.max_price_tiers`) and in
  `knowledge/KNOWLEDGE.md`.
- Ready-built PCs: Gemini reads the hardware, the code prices it from `knowledge/components.json`
  (graphics cards from the price list).
- Buttons under an alert: «✍️ Написал» (contacted: the bot tells you if the listing disappears),
  «🙈 Скрыть» (hide), «🔁 Снизится, сообщи» (tell me if the price drops by 5 % or more).
- If no scan succeeded for 30 minutes (60 in quiet hours), the bot says so, and again once it recovers.

## Bot

A persistent keyboard at the bottom: **📊 Статус** (status), **🏆 Топ** (top), **🚫 Отбраковано** (rejected
by Gemini), **⚙️ Настройки** (settings), **⏯ Пауза / старт** (pause / resume), **📜 Логи** (logs). Each
section opens as one message and the buttons under it switch screens in place (🔄 refresh, ⬅️ back,
🏠 menu). In the top list, a number opens the listing's card with the alert buttons. In the settings:
tier percentages with ➖ / ➕, radius choices, removing a search with a confirmation, and the price
report. The command list is behind the «Меню» button left of the input field.

| Command | What it does |
|---|---|
| `/menu` | main menu |
| `/status` | version, last scan, errors, Gemini limits |
| `/top` | the 10 best candidates of the last 24 h + stale ones |
| `/skipped` | what Gemini rejected in the last 24 h, and why |
| `/settings` | buy ceiling, radius, searches, price report |
| `/prices` | price report: what to adjust in the price list (also sent every Sunday at 19:00) |
| `/logs` | latest warnings and errors from the log |
| `/pause`, `/resume` | stop / resume scanning |
| `/pct 2 80` | tier 2 of the buy ceiling → 80 % |
| `/radius 70` | Bazoš radius in km (any value; the menu offers presets) |
| `/price iphone 15 128 9500` | set a market_price in the price list |
| `/search add mobil galaxy s24 3000 20000`, `/search remove galaxy s24` | searches |

Changes made in the bot (`/pct`, `/radius`, `/price`, `/search`, settings buttons, an applied price
report) are stored in the database and restored after a redeploy; the «🤖 Бот запущен» message lists
what was restored. If the same value was changed in the repository in the meantime, the repository wins.

## Keeping the price list up to date

**Automatically (price report).** The bot records the price of every confidently matched listing. When a
listing leaves the search results, it occasionally opens its page: "deleted" counts as a probable sale
at the last price. Every Sunday at 19:00 it sends a report for items with 5+ listings in 30 days: median
asking price, median sale price, current price and a proposal (from sales when there are 3+, otherwise
asking prices × 95 %; at most ±8 % at a time). Items marked `estimate` come first; models missing from
the price list are listed at the end. Buttons «✅ Применить всё» (apply all) and «☑️ Выборочно»
(choose); nothing changes without a tap. Options: `learn:` in `config.yaml`.

**By hand.**

- `knowledge/prices.json`: `market_price` (what the item actually sells for), `liquidity` 1 to 3,
  `confidence` (`checked` = verified against listings, `estimate` = rough estimate).
- `knowledge/components.json`: used prices of CPUs, RAM, drives, power supplies, monitors and the PC
  factors.
- After editing: `python -m scanner evaluate --all` re-evaluates the stored listings offline, `pytest`
  checks that everything is still recognized. Then commit, push to `main`, redeploy.

## Sbazar

Sbazar.cz runs the same searches as Bazoš, right after every Bazoš run (`sbazar:` in config.yaml).
Sbazar counts the radius from the Ústí nad Orlicí district, so `radius_km: 30` covers about what Bazoš
covers with 60 km. Bazoš category searches (graficka) have no Sbazar category: `sbazar.queries` maps them
to a text query. `sbazar.enabled: false` switches it off; `/status` shows the last Sbazar run.

## Vinted

Vinted.cz runs the same searches as Bazoš, at most every `vinted.interval_min` (20) minutes, in the Bazoš
run right after Sbazar (`vinted:` in config.yaml). No login is needed. Vinted has no radius: it ships, and
about 90 % of what vinted.cz shows comes from sellers in Poland, Slovakia, Hungary, ... with prices converted
to CZK (`vinted.foreign: false` keeps Czech sellers only). So the rules count what you would pay:
price + Vinted's buyer protection (5 % + 18 Kč, given by Vinted) + shipping (`shipping_czk` /
`shipping_abroad_czk`). The ceiling ("максимум") is for the asking price with that already taken off, and the
alert shows the sum ("📦 … = … Kč") and "🌍 из-за рубежа" for foreign sellers. A foreign listing below 55 % of
the market is "too cheap" (`pricing.foreign_min_price_pct`): those are mostly scams or iCloud-locked phones.
`vinted.catalogs` limits each search to a Vinted category (phones, consoles, ...), so "iphone" does not
bring phone cases. Buy only through Vinted's own button (that is where the buyer protection is).

If Vinted answers with an anti-bot page or captcha, the bot does not try to get around it: it stops Vinted,
says so in Telegram and waits for the "🔁 Проверить Vinted" button (also on `/status`). Vinted's terms do not
allow automated access, so keep the interval long and the pages few.

## Facebook Marketplace (optional)

Facebook has no public Marketplace API and automating it is against its rules: the account may get
restricted. So the source is off by default, scans rarely (every ~25 min with jitter, never in quiet
hours), opens at most 10 detail pages per run and never works around a login wall, checkpoint or
captcha: it stops and asks you to log in again. There are no passwords anywhere.

### On the server (with the hosted bot)

1. Set `FB_ENABLED=true` in the host's panel and redeploy. On the first FB run the bot sets up Chromium
   by itself: it downloads it into `/app/data/ms-playwright` (persistent disk) and, if system libraries are
   missing, installs them with apt (falls back to Debian's `chromium` package if the download is blocked).
   It reports in Telegram what it did, or why it could not.
2. On your PC (once, and again whenever Facebook logs you out):
   ```
   pip install -e .
   python -m playwright install chromium
   python -m scanner fb-login        # a browser opens: log in by hand, it closes by itself
   ```
   This writes `fb_state.json` (your Facebook session). Send that file to the bot in Telegram: the bot
   imports it, deletes your message and continues. Treat the file like a password.
3. Set the location in the Facebook account to Ústí nad Orlicí, 60 km. Listings from cities outside
   `pricing.home_cities` / `near_cities` (+ `facebook.extra_cities`) are dropped, because Facebook mixes
   in the whole country.

From a data-center IP Facebook asks for confirmations more often. When it does, the bot stops FB, says so
and waits for a new `fb_state.json`; Bazoš keeps running. `/status` shows the FB state, and Chromium
needs a few hundred MB of RAM on the host.

### On a home PC instead

- **Hosted bot for Bazoš + `python -m scanner fb-worker` at home**: the worker only scans FB and sends
  alerts, without buttons (the hosted bot has its own database).
- **Everything at home**: stop the hosted bot, set `FB_ENABLED=true`, run `python main.py`.

Check what gets parsed: `python -m scanner run-once --source fb --dry`.

## CLI

```
python -m scanner run-once --dry        # scan without sending: candidates and what would go to Telegram
python -m scanner run-once --dry --all  # plus matched listings that are not candidates (and why)
python -m scanner run-once --max-alerts 1  # scan and send, at most 1 alert
python -m scanner review --ids 123 --refresh --json  # Gemini for a specific listing
python -m scanner reviews --skipped     # what Gemini rejected, Gemini usage
python -m scanner evaluate --all        # re-evaluate the database after a price list change
python -m scanner price-report          # the price report (= /prices)
python -m scanner fb-login              # log in to Facebook by hand (profile in fb_profile/)
python -m scanner run-once --source sbazar --dry  # one Sbazar scan without sending
python -m scanner run-once --source vinted --dry  # one Vinted scan without sending
python -m scanner run-once --source fb --dry  # one Facebook scan without sending
python -m scanner fb-worker             # Facebook only, for a home PC next to the hosted bot
python -m scanner save-fixtures         # refresh the test HTML when Bazoš changes its layout
python -m scanner save-fixtures --source vinted  # the same for Vinted (pages trimmed to ~30 KB)
pytest
```

## Troubleshooting

| Symptom | Where to look |
|---|---|
| the bot ignores `/start` | the bot is not running (host log), or `TELEGRAM_CHAT_ID` is wrong |
| "chat not found" in the log | you never sent `/start` to the bot from this account |
| no alerts | `/status`: any candidates? `/top`: everything older than 3 days? `/logs`: errors? |
| a model is not found | it is not in the price list or the parser does not know it (`pytest`, `evaluate --all`) |
| `FileNotFoundError ... /app/data/...` | the file is in `data/`, which the host's disk hides: reference files go to `knowledge/` |
| empty Bazoš results | the layout changed: `save-fixtures`, parser tests, "Техника парсинга" in KNOWLEDGE.md |
