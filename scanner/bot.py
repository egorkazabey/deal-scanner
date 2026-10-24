"""Telegram bot (aiogram 3) and the scan scheduler (APScheduler) in one asyncio process.

`python -m scanner bot`. The bot answers only TELEGRAM_CHAT_ID. Screens and command texts live in
scanner/menu.py as plain functions, so they are testable without Telegram; this module wires them up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from datetime import datetime, time, timedelta
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommandScopeChat, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from scanner import overrides
from scanner.browser_setup import BrowserSetupError, ensure_chromium
from scanner.config import AppConfig, NotifyConfig, Settings, load_config
from scanner.db import Database, Status
from scanner.gemini import PRAGUE
from scanner.menu import (
    BOT_COMMANDS, BTN_LOGS, BTN_PAUSE, BTN_SETTINGS, BTN_SKIPPED, BTN_STATUS, BTN_TOP, HELP, MAIN_KEYBOARD, Screen,
    _fmt_ts, act, alert_keyboard, apply_price, cmd_pct, cmd_price, cmd_radius, cmd_search, code_version, render,
    screen_prices,
)
from scanner.notify import kc, split_messages
from scanner.pipeline import run_scan
from scanner.pricing import PriceList
from scanner.sources.bazos import BazosClient
from scanner.sources.sbazar import SbazarClient
from scanner.sources.facebook import FacebookClient, FbLoginRequired, valid_state

log = logging.getLogger(__name__)

# --- schedule helpers --------------------------------------------------------------------------


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime, cfg: NotifyConfig) -> bool:
    """`now` in Prague time; the window may cross midnight."""
    start, end, t = _hm(cfg.quiet_start), _hm(cfg.quiet_end), now.astimezone(PRAGUE).time()
    return start <= t < end if start <= end else t >= start or t < end


def health_problem(
    last_success: datetime | None, started: datetime, now: datetime, quiet: bool, cfg: NotifyConfig,
) -> str | None:
    """Text of the alert when no scan succeeded for too long (naive local times, like the DB)."""
    interval = cfg.quiet_interval_min if quiet else cfg.interval_min
    limit = timedelta(minutes=max(cfg.health_after_min, 2 * interval))
    since = max(t for t in (last_success, started) if t)  # a fresh start gets a full grace period
    if now - since <= limit:
        return None
    minutes = int((now - since).total_seconds() // 60)
    last = _fmt_ts(last_success.isoformat()) if last_success else "ещё не было"
    return (f"⚠️ Нет успешного прогона Bazoš уже {minutes} мин (последний удачный: {last}).\n"
            "Последние ошибки: /logs, состояние: /status")


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


async def import_fb_state(bot: Bot, file_id: str, settings: Settings) -> str:
    """Save a session file sent by the owner (from fb-login on a PC); the next FB run imports it."""
    buf = await bot.download(file_id)
    try:
        data = json.loads(buf.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "Это не JSON. Нужен файл <code>fb_state.json</code>, который создаёт <code>fb-login</code>."
    if not valid_state(data):
        return "В файле нет входа в Facebook (cookie c_user). Запусти <code>fb-login</code> ещё раз."
    settings.fb_state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.fb_state_path.write_text(json.dumps(data), encoding="utf-8")
    if not settings.fb_enabled:
        return "✅ Сессия сохранена, но Facebook выключен: задай FB_ENABLED=true на хостинге и передеплой."
    return "✅ Сессия Facebook принята, файл удалён из чата. Проверяю FB в ближайшие минуты."


async def _edit(q: CallbackQuery, screen: Screen) -> None:
    """Replace the menu message in place; an unchanged screen is fine."""
    try:
        await q.message.edit_text(screen[0], reply_markup=screen[1])
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


async def _send(bot: Bot, chat_id: int, text: str, markup=None) -> int:
    for attempt in range(2):
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=markup)
            return msg.message_id
        except TelegramRetryAfter as exc:
            if attempt:
                raise
            await asyncio.sleep(exc.retry_after + 1)
    raise AssertionError("unreachable")


async def deliver(bot: Bot, db: Database, chat_id: int, quiet: bool, buttons: bool = True) -> int:
    """Send waiting alerts: quiet-hours ones as one digest (after the quiet hours), others one by one.

    buttons=False: fb-worker on another machine; the bot that handles the buttons has its own DB.
    """
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
        markup = None if r["kind"] == "gone" or not buttons else alert_keyboard(r["source"], r["listing_id"])
        try:
            mid = await _send(bot, chat_id, r["text"], markup)
        except TelegramAPIError as exc:
            log.error("Telegram alert %s failed: %s", r["id"], exc)  # stays pending, next run retries
            continue
        db.mark_sent([r["id"]], mid)
        sent += 1
    return sent


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
        self._fb_lock = asyncio.Lock()
        self._fb_task: asyncio.Task | None = None
        self._fb_client: FacebookClient | None = None
        self._fb_stage = ""
        self._deliver_lock = asyncio.Lock()  # the Bazoš and FB ticks both deliver: never at the same time
        self.started_at = datetime.now()  # naive local, like the DB timestamps
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
        clients = [lambda: BazosClient(cfg.bazos)] if cfg.bazos.enabled else []
        if cfg.sbazar.enabled:
            clients.append(lambda: SbazarClient(cfg.sbazar, cfg.bazos.searches))
        for make in clients:  # one source after the other, never in parallel
            client = make()
            try:
                async with client:
                    report = await run_scan(self.settings, cfg, self.db, client, quiet=quiet)
                log.info("run %s: %d listings, %d candidates, %d alerts", client.source, report.listings,
                         report.candidates, len(report.alerts))
            except Exception:  # noqa: BLE001 - one failed run must not stop the scheduler
                log.exception("scan %s failed", getattr(client, "source", "?"))
        await self.deliver(quiet)

    async def tick_fb(self) -> None:
        """Facebook Marketplace run: rare, never in quiet hours, stops at a login wall until fb-login."""
        if self._fb_lock.locked() or self.db.get_state("paused") == "1" or self.db.get_state("fb_blocked") == "1":
            return
        cfg = self.cfg()
        if in_quiet_hours(datetime.now(PRAGUE), cfg.notify):
            return
        async with self._fb_lock:
            self._fb_task = asyncio.current_task()
            self._fb_stage = "старт"
            try:
                # A hung browser must not block FB forever (APScheduler then skips every next run).
                await asyncio.wait_for(self._fb_run(cfg), timeout=cfg.facebook.run_timeout_min * 60)
            except asyncio.TimeoutError:
                stage = self._fb_client.stage if self._fb_client else self._fb_stage
                log.error("fb run timed out after %d min at: %s", cfg.facebook.run_timeout_min, stage)
                if self.db.get_state("fb_timeout_stage") != stage:  # tell once per stage
                    self.db.set_state("fb_timeout_stage", stage)
                    await self._notify(f"⚠️ FB-прогон завис на шаге «{escape(stage)}» и прерван через "
                                       f"{cfg.facebook.run_timeout_min:g} мин. Следующий по расписанию. "
                                       "Подробности: /logs")
            except FbLoginRequired as exc:
                log.error("fb: %s", exc)
                self.db.set_state("fb_blocked", "1")
                await self._notify(
                    "⚠️ Facebook требует входа (стена входа / проверка / капча). FB-источник остановлен, "
                    "ничего не обхожу.\nНа своём ПК: <code>python -m scanner fb-login</code>, войди руками, "
                    "потом пришли сюда файл <code>fb_state.json</code>: подхвачу его и продолжу сам.",
                    InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🔁 Проверить FB", callback_data="a:fbretry")]]))
            except asyncio.CancelledError:
                log.info("fb run cancelled: shutting down")
            except Exception:  # noqa: BLE001 - one failed run must not stop the scheduler
                log.exception("fb scan failed at: %s", self._fb_client.stage if self._fb_client else self._fb_stage)
            finally:
                self._fb_task = self._fb_client = None
        await self.deliver(quiet=False)

    async def deliver(self, quiet: bool) -> int:
        # Two deliveries at once would both read the same pending alerts and send them twice.
        async with self._deliver_lock:
            return await deliver(self.bot, self.db, self.chat_id, quiet)

    async def _fb_run(self, cfg: AppConfig) -> None:
        self._fb_stage = "установка / проверка Chromium"
        log.info("fb: checking Chromium")
        try:
            if done := await ensure_chromium(self.settings):
                await self._notify("📘 Для Facebook: " + ", ".join(done))
        except BrowserSetupError as exc:
            log.error("fb browser: %s", exc)
            if self.db.get_state("fb_setup_error") != str(exc):  # tell once per distinct problem
                self.db.set_state("fb_setup_error", str(exc))
                await self._notify(f"⚠️ Facebook: {escape(str(exc))}. FB пропущен, Bazoš работает.")
            return
        self.db.set_state("fb_setup_error", "")
        self._fb_stage = "запуск браузера"
        log.info("fb: starting the browser")
        self._fb_client = FacebookClient(cfg, self.settings.fb_profile_dir, self.settings.fb_browser_path,
                                         state_path=self.settings.fb_state_path)
        async with self._fb_client as fb:
            report = await run_scan(self.settings, cfg, self.db, fb)
        self.db.set_state("fb_timeout_stage", "")
        log.info("fb run: %d listings, %d candidates, %d alerts", report.listings, report.candidates,
                 len(report.alerts))

    async def _on_startup(self) -> None:
        restored = overrides.replay(self.settings, self.db)  # Telegram edits survive a redeploy
        cfg = self.cfg()
        self.scheduler.add_job(
            self.tick, IntervalTrigger(minutes=cfg.notify.interval_min, jitter=cfg.notify.jitter_s),
            id="bazos", max_instances=1, coalesce=True, next_run_time=datetime.now(PRAGUE) + timedelta(seconds=5),
        )
        self.scheduler.add_job(self.health_check, IntervalTrigger(minutes=5), id="health", max_instances=1,
                               coalesce=True)
        if self.settings.fb_enabled:
            self.scheduler.add_job(
                self.tick_fb, IntervalTrigger(minutes=cfg.facebook.interval_min, jitter=cfg.facebook.jitter_min * 60),
                id="fb", max_instances=1, coalesce=True, next_run_time=datetime.now(PRAGUE) + timedelta(minutes=2),
            )
            log.info("Facebook source on: every %d min", cfg.facebook.interval_min)
        hour, minute = map(int, cfg.learn.report_time.split(":"))
        self.scheduler.add_job(self.weekly_report, CronTrigger(day_of_week=cfg.learn.report_day, hour=hour,
                                                               minute=minute, timezone=PRAGUE), id="prices")
        self.scheduler.start()
        log.info("scheduler started: Bazoš every %d min", cfg.notify.interval_min)
        try:
            await self.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeChat(chat_id=self.chat_id))
            await self.bot.set_chat_menu_button(chat_id=self.chat_id, menu_button=MenuButtonCommands())
        except TelegramAPIError as exc:
            log.warning("Telegram command menu not set: %s", exc)
        if cfg.notify.startup_message:
            prices = PriceList.load(cfg.pricing.prices_path)
            extra = f"\n♻️ Восстановлены настройки из меню: {escape('; '.join(restored))}" if restored else ""
            await self._notify(f"🤖 Бот запущен. Версия {code_version()}, прайс {len(prices.items)} позиций. "
                               f"Первый прогон через несколько секунд.{extra}", MAIN_KEYBOARD)

    async def _notify(self, text: str, markup=None) -> None:
        try:
            await _send(self.bot, self.chat_id, text, markup)
        except TelegramAPIError as exc:
            log.error("Telegram message failed: %s", exc)

    async def weekly_report(self) -> None:
        """Sunday 19:00: price proposals from the observations, applied only by the owner's button."""
        text, markup = screen_prices(self.settings, self.db)
        try:
            await _send(self.bot, self.chat_id, text, markup)
        except TelegramAPIError as exc:
            log.error("price report failed: %s", exc)

    async def health_check(self) -> None:
        """Telegram once when scans stop succeeding, and once when they work again."""
        if self.db.get_state("paused") == "1":
            return
        cfg = self.cfg()
        quiet = in_quiet_hours(datetime.now(PRAGUE), cfg.notify)
        problem = health_problem(self.db.last_success(), self.started_at, datetime.now(), quiet, cfg.notify)
        alerted = self.db.get_state("health_alerted") == "1"
        if problem and not alerted:
            log.error("health: %s", problem.split("\n")[0])
            await self._notify(problem)
            self.db.set_state("health_alerted", "1")
        elif not problem and alerted:
            await self._notify("✅ Сканирование снова работает")
            self.db.set_state("health_alerted", "0")

    async def _on_shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        if self._fb_task:
            self._fb_task.cancel()
            try:
                await self._fb_task
            except asyncio.CancelledError:
                pass
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

        async def show(m: Message, screen: Screen) -> None:
            await m.answer(screen[0], reply_markup=screen[1])

        def screen(name: str, *args: str) -> Screen:
            return render(name, list(args), self.settings, self.db, self._next_run())

        @r.message(Command("start", "help"))
        async def _help(m: Message) -> None:
            await m.answer(HELP, reply_markup=MAIN_KEYBOARD)
            await show(m, screen("home"))

        @r.message(Command("menu"))
        async def _menu(m: Message) -> None:
            await show(m, screen("home"))

        @r.message(Command("status"))
        @r.message(F.text == BTN_STATUS)
        async def _status(m: Message) -> None:
            await show(m, screen("status"))

        @r.message(Command("top"))
        @r.message(F.text == BTN_TOP)
        async def _top(m: Message) -> None:
            await show(m, screen("top"))

        @r.message(Command("skipped"))
        @r.message(F.text == BTN_SKIPPED)
        async def _skipped(m: Message) -> None:
            await show(m, screen("skipped"))

        @r.message(Command("settings"))
        @r.message(F.text == BTN_SETTINGS)
        async def _settings(m: Message) -> None:
            await show(m, screen("settings"))

        @r.message(Command("prices"))
        async def _prices(m: Message) -> None:
            await show(m, screen("prices"))

        @r.message(Command("logs"))
        async def _logs_cmd(m: Message, command: CommandObject) -> None:
            await show(m, screen("fblogs" if (command.args or "").strip().lower() == "fb" else "logs"))

        @r.message(F.text == BTN_LOGS)
        async def _logs(m: Message) -> None:
            await show(m, screen("logs"))

        @r.message(F.text == BTN_PAUSE)
        async def _toggle(m: Message) -> None:
            await (_resume(m) if self.db.get_state("paused") == "1" else _pause(m))

        @r.message(Command("pause"))
        async def _pause(m: Message) -> None:
            self.db.set_state("paused", "1")
            await m.answer("⏸ Сканирование на паузе. Продолжить: кнопка «⏯» или /resume")

        @r.message(Command("resume"))
        async def _resume(m: Message) -> None:
            self.db.set_state("paused", "0")
            self._kick()
            await m.answer("▶️ Продолжаю, прогон в ближайшие секунды")

        @r.message(Command("pct"))
        async def _pct(m: Message, command: CommandObject) -> None:
            await m.answer(cmd_pct(self.settings, self.db, command.args))

        @r.message(Command("radius"))
        async def _radius(m: Message, command: CommandObject) -> None:
            await m.answer(cmd_radius(self.settings, self.db, command.args))

        @r.message(Command("search"))
        async def _search(m: Message, command: CommandObject) -> None:
            await m.answer(cmd_search(self.settings, self.db, command.args))

        @r.message(Command("price"))
        async def _price(m: Message, command: CommandObject) -> None:
            text, markup = cmd_price(self.settings, self.db, command.args)
            await m.answer(text, reply_markup=markup)

        @r.message(F.document)
        async def _document(m: Message) -> None:
            name = m.document.file_name or ""
            if not name.endswith(".json") or (m.document.file_size or 0) > 1_000_000:
                await m.answer("Файлы я принимаю только один: <code>fb_state.json</code> из fb-login.")
                return
            text = await import_fb_state(self.bot, m.document.file_id, self.settings)
            try:
                await m.delete()  # the session file should not stay in the chat
            except TelegramAPIError:
                text += "\nУдали сообщение с файлом сам: это доступ к твоему FB."
            if text.startswith("✅"):
                self.db.set_state("fb_blocked", "0")
                self._kick("fb")
            await m.answer(text)

        @r.callback_query(F.data.startswith("m:"))
        async def _screen(q: CallbackQuery) -> None:
            name, *args = q.data.split(":")[1:]
            await _edit(q, screen(name, *args))
            await q.answer()

        @r.callback_query(F.data.startswith("a:"))
        async def _action(q: CallbackQuery) -> None:
            action, *args = q.data.split(":")[1:]
            if action == "noop":
                await q.answer()
                return
            toast, name, screen_args = act(action, args, self.settings, self.db)
            if action == "resume":
                self._kick()
            if action == "fbretry":
                self._kick("fb")
            await _edit(q, screen(name, *screen_args))
            await q.answer(toast)

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
            # Keep the other rows (e.g. "К топу" on a card), mark the pressed button.
            rows = q.message.reply_markup.inline_keyboard if q.message.reply_markup else []
            marked = alert_keyboard(source, listing_id, pressed=key).inline_keyboard
            rest = [row_ for row_ in rows if not any(b.callback_data and b.callback_data[:2] in ("c:", "h:", "w:")
                                                     for b in row_)]
            try:
                await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=marked + rest))
            except TelegramAPIError:
                pass  # e.g. "message is not modified"
            await q.answer(note, show_alert=False)

        return r

    def _kick(self, job_id: str = "bazos") -> None:
        job = self.scheduler.get_job(job_id)
        if job:
            job.modify(next_run_time=datetime.now(PRAGUE) + timedelta(seconds=5))

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
