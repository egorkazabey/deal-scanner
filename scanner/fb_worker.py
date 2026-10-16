"""`python -m scanner fb-worker`: Facebook Marketplace only, for a home PC next to the hosted bot.

The hosted bot keeps Bazoš and the Telegram menu. This worker does not poll Telegram (two pollers with
one token conflict); it only scans FB on its schedule and sends alerts (without buttons: the hosted bot
has its own DB and would not know these listings). A login wall stops it until `fb-login` is run on
this machine (which clears the flag in this machine's DB); it then continues by itself.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime

from aiogram.exceptions import TelegramAPIError

from scanner.browser_setup import ensure_chromium
from scanner.bot import _send, deliver, in_quiet_hours, make_bot
from scanner.config import Settings, load_config
from scanner.db import Database
from scanner.gemini import PRAGUE
from scanner.pipeline import run_scan
from scanner.sources.facebook import FacebookClient, FbLoginRequired

log = logging.getLogger(__name__)


async def run_worker(settings: Settings) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
    db = Database(settings.db_path)
    bot = make_bot(settings.telegram_bot_token)
    told_blocked = False
    try:
        await _send(bot, settings.telegram_chat_id, "📘 FB-воркер запущен (домашний ПК)")
        while True:
            cfg = load_config(settings.config_path)
            blocked = db.get_state("fb_blocked") == "1"
            if blocked and not told_blocked:
                await _send(bot, settings.telegram_chat_id,
                            "⚠️ Facebook требует входа. FB-воркер ждёт: запусти на этом ПК "
                            "<code>python -m scanner fb-login</code>, дальше он продолжит сам.")
                told_blocked = True
            if not blocked:
                told_blocked = False
                if not in_quiet_hours(datetime.now(PRAGUE), cfg.notify):
                    try:
                        await ensure_chromium(settings)
                        async with FacebookClient(cfg, settings.fb_profile_dir, settings.fb_browser_path,
                                                  state_path=settings.fb_state_path) as fb:
                            report = await run_scan(settings, cfg, db, fb)
                        log.info("fb run: %d listings, %d candidates, %d alerts", report.listings,
                                 report.candidates, len(report.alerts))
                    except FbLoginRequired as exc:
                        log.error("fb: %s", exc)
                        db.set_state("fb_blocked", "1")
                        continue
                    except Exception:  # noqa: BLE001 - keep the worker alive
                        log.exception("fb scan failed")
                    try:
                        await deliver(bot, db, settings.telegram_chat_id, quiet=False, buttons=False)
                    except TelegramAPIError as exc:
                        log.error("Telegram: %s", exc)
            minutes = cfg.facebook.interval_min + random.uniform(-cfg.facebook.jitter_min, cfg.facebook.jitter_min)
            await asyncio.sleep(60 if blocked else max(5, minutes) * 60)
    finally:
        await bot.session.close()
        db.close()
