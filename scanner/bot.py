"""Telegram bot (aiogram 3) and the scan scheduler (APScheduler) in one asyncio process.

`python -m scanner bot`. The bot answers only TELEGRAM_CHAT_ID. Command logic lives in plain
`cmd_*` functions returning HTML, so it is testable without Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from datetime import datetime, time, timedelta
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scanner import config_edit
from scanner.config import PROJECT_ROOT, AppConfig, NotifyConfig, Settings, load_config
from scanner.db import Database, Status
from scanner.gemini import PRAGUE, GeminiReviewer, Verdict
from scanner.notify import NO_AI_EMOJI, VERDICT_EMOJI, age_words, kc, split_messages
from scanner.pipeline import cached_pc_results, run_scan
from scanner.pricing import PriceList, evaluate_many, max_price_rule_lines, rank
from scanner.sources.bazos import BazosClient

log = logging.getLogger(__name__)

HELP = """\
<b>Сканер Bazoš</b>
/status — последний прогон, ошибки, лимиты Gemini
/pause, /resume — остановить / продолжить сканирование
/top — 10 лучших активных кандидатов за 24 ч + «залежавшиеся»
/skipped — что Gemini отбраковал за 24 ч
/pct — ступени потолка покупки; <code>/pct 2 80</code> — ступень 2 → 80 %
/radius 60 — радиус Bazoš, км
/price &lt;позиция&gt; &lt;цена&gt; — поправить market_price в prices.json
/search list | add &lt;mobil|pc&gt; &lt;запрос&gt; [от] [до] | remove &lt;имя|номер&gt;"""


# --- schedule helpers --------------------------------------------------------------------------


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime, cfg: NotifyConfig) -> bool:
    """`now` in Prague time; the window may cross midnight."""
    start, end, t = _hm(cfg.quiet_start), _hm(cfg.quiet_end), now.astimezone(PRAGUE).time()
    return start <= t < end if start <= end else t >= start or t < end


# --- delivery ----------------------------------------------------------------------------------


def _session() -> AiohttpSession:
    """aiogram trusts only certifi; also trust SSL_CERT_FILE when set (TLS-inspecting proxies), like httpx."""
    session = AiohttpSession()
    if cafile := os.environ.get("SSL_CERT_FILE"):
        session._connector_init["ssl"] = ssl.create_default_context(cafile=cafile)
    return session


def make_bot(token: str, **kw) -> Bot:
    kw.setdefault("session", _session())
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True), **kw)


def alert_keyboard(source: str, listing_id: str, pressed: str | None = None) -> InlineKeyboardMarkup:
    labels = {"c": "✍️ Написал", "h": "🙈 Скрыть", "w": "🔁 Снизится — сообщи"}
    row = [
        InlineKeyboardButton(text=("✅ " if key == pressed else "") + text, callback_data=f"{key}:{source}:{listing_id}")
        for key, text in labels.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row[:2], row[2:]])


async def _send(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> int:
    for attempt in range(2):
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=markup)
            return msg.message_id
        except TelegramRetryAfter as exc:
            if attempt:
                raise
            await asyncio.sleep(exc.retry_after + 1)
    raise AssertionError("unreachable")


async def deliver(bot: Bot, db: Database, chat_id: int, quiet: bool) -> int:
    """Send waiting alerts: quiet-hours ones as one digest (after the quiet hours), others one by one."""
    if quiet:
        return 0
    pending = db.pending_alerts()
    sent = 0
    digest = [r for r in pending if r["quiet"]]
    if digest:
        header = f"☀️ <b>Сводка за ночь: {len(digest)}</b>"
        try:
            for part in split_messages(header, [r["summary"] for r in digest]):
                mid = await _send(bot, chat_id, part)
            db.mark_sent([r["id"] for r in digest], mid)
            sent += len(digest)
        except TelegramAPIError as exc:
            log.error("Telegram digest failed: %s", exc)
    for r in pending:
        if r["quiet"]:
            continue
        markup = None if r["kind"] == "gone" else alert_keyboard(r["source"], r["listing_id"])
        try:
            mid = await _send(bot, chat_id, r["text"], markup)
        except TelegramAPIError as exc:
            log.error("Telegram alert %s failed: %s", r["id"], exc)  # stays pending, next run retries
            continue
        db.mark_sent([r["id"]], mid)
        sent += 1
    return sent


# --- commands (plain functions) ---------------------------------------------------------------


def _fmt_ts(value: str | None) -> str:
    """DB timestamps are naive local server time (UTC on the host): show them in Prague time."""
    if not value:
        return "—"
    return datetime.fromisoformat(value).astimezone(PRAGUE).strftime("%d.%m. %H:%M")


def code_version() -> str:
    """Commit of the running code: env from the host (GIT_COMMIT, RENDER_GIT_COMMIT, ...) or .git, else '?'."""
    for name in ("GIT_COMMIT", "SOURCE_COMMIT", "RENDER_GIT_COMMIT", "RAILWAY_GIT_COMMIT_SHA", "COMMIT_SHA"):
        if value := os.environ.get(name):
            return value[:7]
    head = PROJECT_ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            ref_file = PROJECT_ROOT / ".git" / ref[5:]
            if ref_file.exists():
                return ref_file.read_text().strip()[:7]
            packed = (PROJECT_ROOT / ".git" / "packed-refs").read_text()
            return next(l.split()[0][:7] for l in packed.splitlines() if l.endswith(ref[5:]))
        return ref[:7]
    except (OSError, StopIteration):
        return "?"


def cmd_status(settings: Settings, cfg: AppConfig, db: Database, next_run: datetime | None = None) -> str:
    prices = PriceList.load(cfg.pricing.prices_path)
    lines = ["<b>Статус</b>", f"Версия: {code_version()}, прайс {len(prices.items)} позиций"]
    if db.get_state("paused") == "1":
        lines.append("⏸ на паузе (/resume)")
    runs = db.last_runs(1)
    if runs:
        r = runs[0]
        if r["finished_at"]:
            dur = (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).seconds
            lines.append(f"Последний прогон: {_fmt_ts(r['started_at'])} ({dur // 60} мин {dur % 60} с)")
        else:
            lines.append(f"Прогон идёт с {_fmt_ts(r['started_at'])}")
        lines.append(f"Объявлений {r['listings']}: новых {r['new']}, цена изменилась {r['price_changed']}, "
                     f"кандидатов {r['candidates']}, уведомлений {r['alerts']}")
        if r["errors"]:
            lines.append("❗ Ошибки: " + escape(r["errors"][:800]))
    else:
        lines.append("Прогонов ещё не было")
    if next_run:
        lines.append(f"Следующий: {next_run.astimezone(PRAGUE):%H:%M}")
    c = db.counts()
    lines.append(f"В базе {c['total']}: уведомлено {c.get('alerted', 0)}, написал {c.get('contacted', 0)}, "
                 f"скрыто {c.get('hidden', 0)}, слежу за ценой {c['watched']}")
    lines.append(f"Bazoš: {cfg.bazos.radius_km} км, {len(cfg.bazos.searches)} поисков, "
                 f"каждые {cfg.notify.interval_min} мин (тихие часы {cfg.notify.quiet_start}–{cfg.notify.quiet_end})")
    reviewer = GeminiReviewer(settings, cfg.gemini, cfg.pricing, PriceList([]), db)
    if reviewer.disabled_reason:
        lines.append(f"Gemini: {reviewer.disabled_reason}")
    for model, u in reviewer.usage().items():
        lines.append(f"Gemini {model}: {u['requests']} запросов, осталось {u['left']} из {settings.gemini_rpd}, "
                     f"ошибок {u['errors']}, токенов {kc(u['total_tokens'])}")
    return "\n".join(lines)


def _review_emoji(db: Database, source: str, listing_id: str, price: int | None) -> str:
    row = db.get_review(source, listing_id, price) if price is not None else None
    return VERDICT_EMOJI[row["verdict"]] if row else NO_AI_EMOJI


def cmd_top(cfg: AppConfig, db: Database, now: datetime | None = None, limit: int = 10, stale_limit: int = 5) -> str:
    now = now or datetime.now()
    prices = PriceList.load(cfg.pricing.prices_path)
    listings = db.listings(seen_since=now - timedelta(hours=24))
    hidden = set()
    for l in listings:
        row = db.get(l.source, l.id)
        if row and row["status"] == Status.HIDDEN:
            hidden.add((l.source, l.id))
    today = now.date()
    evaluations = evaluate_many(listings, prices, cfg.pricing, today)
    pcs = [r.evaluation for r in cached_pc_results(evaluations, db, prices, cfg, today) if not r.skipped]
    ranked = [e for e in sorted(rank(evaluations) + pcs, key=lambda e: e.score, reverse=True)
              if (e.listing.source, e.listing.id) not in hidden]
    active = [e for e in ranked if "stale" not in e.flags][:limit]
    stale = [e for e in ranked if "stale" in e.flags][:stale_limit]

    def line(i: int, e) -> str:
        l = e.listing
        return (f"{i}. {_review_emoji(db, l.source, l.id, l.price)} <a href=\"{escape(l.url)}\">"
                f"{escape(e.match.item.label)}</a> — {kc(l.price)} Kč · рынок {kc(e.market_price)} · "
                f"макс {kc(e.max_price)} · предложить {kc(e.offer_price)} · {e.score} · "
                f"{escape(l.location or '?')}, {age_words(l.posted_date, today)}")

    out = [f"<b>Топ-{len(active)} за 24 ч</b>"] + [line(i, e) for i, e in enumerate(active, 1)]
    if not active:
        out.append("кандидатов нет")
    if stale:
        out.append(f"\n<b>Залежавшиеся (> {cfg.pricing.stale_days} дн.)</b>")
        out += [line(i, e) for i, e in enumerate(stale, 1)]
    return "\n".join(out)


def cmd_skipped(db: Database, now: datetime | None = None, limit: int = 20) -> str:
    rows = db.reviews((now or datetime.now()) - timedelta(hours=24), only_hidden=True)[:limit]
    if not rows:
        return "Gemini за 24 ч ничего не отбраковал"
    out = [f"<b>Отбраковано Gemini за 24 ч: {len(rows)}</b>"]
    for r in rows:
        v = Verdict.model_validate_json(r["response"])
        out.append(f"{VERDICT_EMOJI[v.verdict]} <a href=\"{escape(r['url'])}\">{escape(r['title'])}</a> — "
                   f"{kc(r['price'])} Kč, scam {v.scam_risk}: {escape(v.reason_ru)}")
    return "\n".join(out)


def cmd_pct(settings: Settings, args: str | None) -> str:
    if args:
        parts = args.split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return "Формат: <code>/pct 2 80</code> (ступень, процент)"
        try:
            cfg = config_edit.set_tier_pct(settings.config_path, int(parts[0]), int(parts[1]))
        except config_edit.ConfigEditError as exc:
            return escape(str(exc))
        head = f"Ступень {parts[0]} → {parts[1]} %. Новые цифры со следующего прогона."
    else:
        cfg, head = load_config(settings.config_path), "Потолок покупки (config.yaml):"
    rule = "\n".join(f"{i}. {escape(l)}" for i, l in enumerate(max_price_rule_lines(cfg.pricing), 1))
    return f"{head}\n{rule}\n\nKNOWLEDGE.md обнови вручную, если поменял ступени."


def cmd_radius(settings: Settings, args: str | None) -> str:
    if not args or not args.strip().isdigit():
        return f"Радиус сейчас {load_config(settings.config_path).bazos.radius_km} км. Формат: <code>/radius 60</code>"
    try:
        config_edit.set_radius(settings.config_path, int(args))
    except config_edit.ConfigEditError as exc:
        return escape(str(exc))
    return f"Радиус Bazoš: {int(args)} км (со следующего прогона)"


def cmd_search(settings: Settings, args: str | None) -> str:
    parts = (args or "list").split()
    action, rest = parts[0], parts[1:]
    try:
        if action == "list":
            cfg = load_config(settings.config_path)
            lines = [f"{i}. {escape(s.name)} [{s.section}] {kc(s.price_min)}–{kc(s.price_max)} Kč"
                     for i, s in enumerate(cfg.bazos.searches, 1)]
            return "<b>Поиски Bazoš</b>\n" + "\n".join(lines)
        if action == "add":
            if len(rest) < 2 or rest[0] not in ("mobil", "pc"):
                return "Формат: <code>/search add mobil galaxy s24 3000 20000</code>"
            section, words = rest[0], rest[1:]
            nums = []
            while words and words[-1].isdigit() and len(nums) < 2:
                nums.insert(0, int(words.pop()))
            if not words:
                return "Нужен текст запроса"
            lo, hi = (nums + [None, None])[:2]
            config_edit.add_search(settings.config_path, section, " ".join(words), lo, hi)
            return f"Добавлен поиск «{escape(' '.join(words))}» [{section}]"
        if action == "remove" and rest:
            config_edit.remove_search(settings.config_path, " ".join(rest))
            return f"Удалён поиск «{escape(' '.join(rest))}»"
    except config_edit.ConfigEditError as exc:
        return escape(str(exc))
    return "Формат: /search list | add &lt;mobil|pc&gt; &lt;запрос&gt; [от] [до] | remove &lt;имя|номер&gt;"


def apply_price(settings: Settings, db: Database, index: int, price: int) -> str:
    cfg = load_config(settings.config_path)
    try:
        label, old = config_edit.set_market_price(cfg.pricing.prices_path, index, price)
    except config_edit.ConfigEditError as exc:
        return escape(str(exc))
    db.log_price_change(label, old, price)
    log.warning("market_price changed via Telegram: %s %d -> %d", label, old, price)
    return f"💰 {escape(label)}: {kc(old)} → {kc(price)} Kč (prices.json)"


def cmd_price(settings: Settings, db: Database, args: str | None) -> tuple[str, InlineKeyboardMarkup | None]:
    parts = (args or "").split()
    if len(parts) < 2 or not parts[-1].isdigit():
        return "Формат: <code>/price iphone 15 pro 128 15500</code>", None
    query, price = " ".join(parts[:-1]), int(parts[-1])
    cfg = load_config(settings.config_path)
    hits = config_edit.find_price_items(cfg.pricing.prices_path, query)
    if not hits:
        return f"В прайсе нет «{escape(query)}»", None
    if len(hits) == 1:
        return apply_price(settings, db, hits[0].index, price), None
    if len(hits) > 12:
        return f"Нашлось {len(hits)} позиций, уточни запрос", None
    rows = [[InlineKeyboardButton(text=f"{h.label} ({kc(h.market_price)})", callback_data=f"p:{h.index}:{price}")]
            for h in hits]
    return f"Какую позицию поставить на {kc(price)} Kč?", InlineKeyboardMarkup(inline_keyboard=rows)


# --- the app -----------------------------------------------------------------------------------


class ScannerApp:
    def __init__(self, settings: Settings, bot: Bot | None = None):
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        self.settings = settings
        self.chat_id = settings.telegram_chat_id
        self.db = Database(settings.db_path)
        self.bot = bot or make_bot(settings.telegram_bot_token)
        self.dp = Dispatcher()
        self.scheduler = AsyncIOScheduler(timezone=PRAGUE)
        self._scan_lock = asyncio.Lock()
        self._last_scan: datetime | None = None
        self._scan_task: asyncio.Task | None = None
        self.dp.include_router(self._router())
        self.dp.startup.register(self._on_startup)
        self.dp.shutdown.register(self._on_shutdown)

    def cfg(self) -> AppConfig:
        return load_config(self.settings.config_path)  # re-read: bot commands edit it

    async def tick(self) -> None:
        if self._scan_lock.locked():
            log.info("previous run still going, skipping this one")
            return
        async with self._scan_lock:
            self._scan_task = asyncio.current_task()
            try:
                await self._tick()
            except asyncio.CancelledError:
                log.info("scan cancelled: shutting down")  # the job ends quietly, the DB closes after it
            finally:
                self._scan_task = None

    async def _tick(self) -> None:
        if self.db.get_state("paused") == "1":
            return
        cfg = self.cfg()
        now = datetime.now(PRAGUE)
        quiet = in_quiet_hours(now, cfg.notify)
        if quiet and self._last_scan and now - self._last_scan < timedelta(minutes=cfg.notify.quiet_interval_min - 1):
            return
        self._last_scan = now
        try:
            async with BazosClient(cfg.bazos) as client:
                report = await run_scan(self.settings, cfg, self.db, client, quiet=quiet)
            log.info("run: %d listings, %d candidates, %d alerts", report.listings, report.candidates,
                     len(report.alerts))
        except Exception:  # noqa: BLE001 - one failed run must not stop the scheduler
            log.exception("scan failed")
        await deliver(self.bot, self.db, self.chat_id, quiet)

    async def _on_startup(self) -> None:
        cfg = self.cfg()
        self.scheduler.add_job(
            self.tick, IntervalTrigger(minutes=cfg.notify.interval_min, jitter=cfg.notify.jitter_s),
            id="bazos", max_instances=1, coalesce=True, next_run_time=datetime.now(PRAGUE) + timedelta(seconds=5),
        )
        self.scheduler.start()
        log.info("scheduler started: Bazoš every %d min", cfg.notify.interval_min)

    async def _on_shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        if self._scan_task:  # stop a running scan before the DB goes away
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        self.db.close()

    def _next_run(self) -> datetime | None:
        job = self.scheduler.get_job("bazos")
        return job.next_run_time if job else None

    def _router(self) -> Router:
        r = Router()
        r.message.filter(F.chat.id == self.chat_id)  # everyone else is ignored
        r.callback_query.filter(F.message.chat.id == self.chat_id)

        @r.message(Command("start", "help"))
        async def _help(m: Message) -> None:
            await m.answer(HELP)

        @r.message(Command("status"))
        async def _status(m: Message) -> None:
            await m.answer(cmd_status(self.settings, self.cfg(), self.db, self._next_run()))

        @r.message(Command("pause"))
        async def _pause(m: Message) -> None:
            self.db.set_state("paused", "1")
            await m.answer("⏸ Сканирование на паузе. /resume — продолжить")

        @r.message(Command("resume"))
        async def _resume(m: Message) -> None:
            self.db.set_state("paused", "0")
            await m.answer("▶️ Продолжаю, следующий прогон в течение пары минут")
            job = self.scheduler.get_job("bazos")
            if job:
                job.modify(next_run_time=datetime.now(PRAGUE) + timedelta(seconds=5))

        @r.message(Command("top"))
        async def _top(m: Message) -> None:
            for part in split_messages("", cmd_top(self.cfg(), self.db).split("\n")):
                await m.answer(part)

        @r.message(Command("skipped"))
        async def _skipped(m: Message) -> None:
            for part in split_messages("", cmd_skipped(self.db).split("\n")):
                await m.answer(part)

        @r.message(Command("pct"))
        async def _pct(m: Message, command: CommandObject) -> None:
            await m.answer(cmd_pct(self.settings, command.args))

        @r.message(Command("radius"))
        async def _radius(m: Message, command: CommandObject) -> None:
            await m.answer(cmd_radius(self.settings, command.args))

        @r.message(Command("search"))
        async def _search(m: Message, command: CommandObject) -> None:
            await m.answer(cmd_search(self.settings, command.args))

        @r.message(Command("price"))
        async def _price(m: Message, command: CommandObject) -> None:
            text, markup = cmd_price(self.settings, self.db, command.args)
            await m.answer(text, reply_markup=markup)

        @r.callback_query(F.data.startswith("p:"))
        async def _price_pick(q: CallbackQuery) -> None:
            _, index, price = q.data.split(":")
            text = apply_price(self.settings, self.db, int(index), int(price))
            await q.message.edit_text(text)
            await q.answer()

        @r.callback_query(F.data.regexp(r"^[chw]:"))
        async def _listing_button(q: CallbackQuery) -> None:
            key, source, listing_id = q.data.split(":", 2)
            row = self.db.get(source, listing_id)
            if row is None:
                await q.answer("Объявления нет в базе")
                return
            if key == "c":
                self.db.set_status(source, listing_id, Status.CONTACTED)
                note = "Отмечено: написал. Сообщу, если объявление пропадёт."
            elif key == "h":
                self.db.set_status(source, listing_id, Status.HIDDEN)
                note = "Скрыто, больше не пришлю"
            else:
                if row["price"] is None:
                    await q.answer("У объявления нет цены")
                    return
                self.db.set_watch(source, listing_id, row["price"])
                pct = self.cfg().notify.watch_drop_pct
                note = f"Слежу: сообщу, если цена упадёт на {pct} % и больше (сейчас {kc(row['price'])} Kč)"
            try:
                await q.message.edit_reply_markup(reply_markup=alert_keyboard(source, listing_id, pressed=key))
            except TelegramAPIError:
                pass  # e.g. "message is not modified"
            await q.answer(note)

        return r

    async def run(self) -> None:
        log.info("bot started, chat %s", self.chat_id)
        await self.dp.start_polling(self.bot, handle_signals=True)


async def send_pending(settings: Settings, db: Database) -> int:
    """One-shot delivery for `run-once` (no quiet hours: a manual run sends right away)."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: alerts stay in the DB")
        return 0
    bot = make_bot(settings.telegram_bot_token)
    try:
        return await deliver(bot, db, settings.telegram_chat_id, quiet=False)
    finally:
        await bot.session.close()
