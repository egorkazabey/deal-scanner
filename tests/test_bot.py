"""Bot: quiet hours, delivery, commands, config.yaml / prices.json edits, chat filter."""

import asyncio
import io
import json
import logging
import shutil
from datetime import datetime, timedelta

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from rich.console import Console

from scanner import config_edit
from scanner.bot import ScannerApp, deliver, health_problem, in_quiet_hours, make_bot
from scanner.menu import (
    act, cmd_logs, cmd_pct, cmd_price, cmd_radius, cmd_search, cmd_skipped, cmd_status, cmd_top, code_version, render,
)
from scanner.config import PROJECT_ROOT, AppConfig, NotifyConfig, Settings, load_config
from scanner.db import Database, Status
from scanner.gemini import PRAGUE
from scanner.logs import setup_logging
from scanner.models import Listing
from scanner.pricing import PriceList

CHAT = 4242


class FakeSession(BaseSession):
    """Records Bot API calls instead of sending them."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(), text=method.text,
                           chat=Chat(id=method.chat_id, type="private"))
        return True

    async def stream_content(self, *a, **kw):
        raise NotImplementedError

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Settings must not pick up the developer's real TELEGRAM_* / GEMINI_* variables."""
    for name in ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACK_MODEL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def files(tmp_path):
    """Copies of config.yaml and prices.json; settings pointing at them."""
    prices = tmp_path / "prices.json"
    shutil.copy(PROJECT_ROOT / "knowledge" / "prices.json", prices)
    text = (PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(text.replace("pricing:\n", f"pricing:\n  prices_path: {prices}\n", 1), encoding="utf-8")
    settings = Settings(_env_file=None, config_path=config, db_path=tmp_path / "t.db", log_dir=tmp_path / "logs",
                        telegram_bot_token="123456:TEST", telegram_chat_id=CHAT)
    return settings, config, prices


def run(coro):
    return asyncio.run(coro)


# --- quiet hours ---------------------------------------------------------------------------------


@pytest.mark.parametrize("hm, quiet", [("00:29", False), ("00:30", True), ("03:00", True), ("06:29", True),
                                       ("06:30", False), ("23:59", False)])
def test_quiet_hours(hm, quiet):
    h, m = map(int, hm.split(":"))
    assert in_quiet_hours(datetime(2026, 10, 15, h, m, tzinfo=PRAGUE), NotifyConfig()) is quiet


def test_quiet_hours_across_midnight():
    cfg = NotifyConfig(quiet_start="23:00", quiet_end="06:00")
    assert in_quiet_hours(datetime(2026, 10, 15, 23, 30, tzinfo=PRAGUE), cfg)
    assert in_quiet_hours(datetime(2026, 10, 15, 5, 0, tzinfo=PRAGUE), cfg)
    assert not in_quiet_hours(datetime(2026, 10, 15, 12, 0, tzinfo=PRAGUE), cfg)


# --- delivery ------------------------------------------------------------------------------------


def test_deliver_digest_and_single_alerts():
    db = Database(":memory:")
    db.add_alert("bazos", "1", 7000, "deal", "night 1", "s1", quiet=True)
    db.add_alert("bazos", "2", 8000, "deal", "night 2", "s2", quiet=True)
    db.add_alert("bazos", "3", 9000, "deal", "day text", "s3")
    db.add_alert("bazos", "4", 9000, "gone", "gone text", "s4")
    session = FakeSession()
    bot = make_bot("123456:TEST", session=session)

    assert run(deliver(bot, db, CHAT, quiet=True)) == 0 and session.calls == []
    assert run(deliver(bot, db, CHAT, quiet=False)) == 4
    texts = [c.text for c in session.calls]
    assert texts[0] == "☀️ <b>Сводка за ночь: 2</b>\ns1\ns2"
    assert texts[1:] == ["day text", "gone text"]
    buttons = session.calls[1].reply_markup.inline_keyboard
    assert [b.callback_data for row in buttons for b in row] == ["c:bazos:3", "h:bazos:3", "w:bazos:3"]
    assert session.calls[2].reply_markup is None
    assert db.pending_alerts() == []


# --- commands --------------------------------------------------------------------------------


def test_status_and_top(files):
    settings, _, _ = files
    db = Database(":memory:")
    now = datetime.now()
    db.upsert(Listing("bazos", "1", "https://x/1", "iPhone 15 128GB", 7000, "Pardubice", now.date()), now)
    db.upsert(Listing("bazos", "2", "https://x/2", "iPhone 15 128GB modrý", 7100, "Svitavy",
                      now.date() - timedelta(days=20)), now)
    run_id = db.start_run(now)
    db.finish_run(run_id, now, listings=2, new=2, candidates=2, alerts=1)
    cfg = load_config(settings.config_path)
    status = cmd_status(settings, cfg, db)
    assert "📦 Объявлений 2 · новых 2" in status and "GEMINI_API_KEY не задан" in status
    n = len(PriceList.load(cfg.pricing.prices_path).items)
    assert f"прайс {n} позиций" in status.split("\n")[1]
    assert f"Последний прогон: {now.astimezone(PRAGUE):%d.%m. %H:%M}" in status  # Prague time, not server time
    top = cmd_top(cfg, db, now)
    assert "<b>🏆 Топ за 24 ч: 1</b>" in top and "<b>🕸 Залежавшиеся (&gt; 14 дн.)</b>" in top
    assert top.index("https://x/1") < top.index("Залежавшиеся") < top.index("https://x/2")
    db.set_status("bazos", "1", Status.HIDDEN)
    assert "https://x/1" not in cmd_top(cfg, db, now)
    assert "Gemini за 24 ч ничего не отбраковал" in cmd_skipped(db)


def test_pct_changes_one_tier_and_keeps_the_file(files):
    settings, config, _ = files
    before = config.read_text(encoding="utf-8").split("\n")
    out = cmd_pct(settings, "2 80")
    assert "Ступень 2 → 80 %" in out and "market × 0.80" in out
    after = config.read_text(encoding="utf-8").split("\n")
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert changed == [("    - {up_to: 12000, pct: 78, min_margin: 1200}   # 5 000 – 12 000",
                        "    - {up_to: 12000, pct: 80, min_margin: 1200}   # 5 000 – 12 000")]
    assert "ступень: от 1 до 3" in cmd_pct(settings, "4 80")
    assert "процент" in cmd_pct(settings, "1 99")


def test_radius(files):
    settings, _, _ = files
    assert cmd_radius(settings, "45") == "Радиус Bazoš: 45 км (со следующего прогона)"
    assert load_config(settings.config_path).bazos.radius_km == 45
    assert "от 1 до 500" in cmd_radius(settings, "900")


def test_search_add_list_remove(files):
    settings, _, _ = files
    n = len(load_config(settings.config_path).bazos.searches)
    assert "Добавлен" in cmd_search(settings, "add mobil galaxy s24 3000 20000")
    searches = load_config(settings.config_path).bazos.searches
    added = next(s for s in searches if s.name == "galaxy s24")
    assert (added.section, added.query, added.price_min, added.price_max) == ("mobil", "galaxy s24", 3000, 20000)
    assert [s.section for s in searches].index("pc") > searches.index(added)  # grouped with "mobil"
    assert "уже есть" in cmd_search(settings, "add mobil galaxy s24")
    assert "galaxy s24" in cmd_search(settings, "list")
    assert "Удалён" in cmd_search(settings, "remove galaxy s24")
    assert len(load_config(settings.config_path).bazos.searches) == n
    assert "нет" in cmd_search(settings, "remove nothing")


def test_price_single_and_ambiguous(files):
    settings, _, prices = files
    db = Database(":memory:")
    before = prices.read_text(encoding="utf-8")
    text, markup = cmd_price(settings, db, "apple watch ultra 3 14000")
    assert markup is None and text == "💰 Apple Watch Ultra 3 49 мм: 13 800 → 14 000 Kč (prices.json)"
    diff = [(a, b) for a, b in zip(before.split("\n"), prices.read_text(encoding="utf-8").split("\n")) if a != b]
    assert diff == [('   "market_price": 13800,', '   "market_price": 14000,')]
    assert db.conn.execute("SELECT item, old_price, new_price FROM price_changes").fetchone()[:] == \
        ("Apple Watch Ultra 3 49 мм", 13800, 14000)

    text, markup = cmd_price(settings, db, "iphone 15 pro 15000")
    assert "Какую позицию" in text and len(markup.inline_keyboard) > 1
    assert all(b[0].callback_data.startswith("p:") and b[0].callback_data.endswith(":15000")
               for b in markup.inline_keyboard)
    assert cmd_price(settings, db, "nokia 3310 500")[0] == "В прайсе нет «nokia 3310»"


def test_config_edit_rejects_invalid_result(files):
    _, config, _ = files
    with pytest.raises(config_edit.ConfigEditError):
        config_edit.set_radius(config, 0)


def test_code_version_from_host_env(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "abcdef1234567")
    assert code_version() == "abcdef1"
    monkeypatch.delenv("GIT_COMMIT")
    assert code_version() != ""  # .git in a checkout, "?" in an image without it


# --- chat filter -------------------------------------------------------------------------------


def _update(chat_id: int, text: str) -> Update:
    return Update(update_id=1, message=Message(
        message_id=1, date=datetime.now(), text=text, chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="x"),
    ))


def test_bot_answers_only_its_chat(files):
    settings, _, _ = files
    session = FakeSession()
    app = ScannerApp(settings, bot=make_bot("123456:TEST", session=session))

    async def scenario():
        await app.dp.feed_update(app.bot, _update(999, "/help"))
        assert session.calls == []
        await app.dp.feed_update(app.bot, _update(CHAT, "/help"))
        assert len(session.calls) == 2 and "/status" in session.calls[0].text  # help + menu
        assert session.calls[0].reply_markup.keyboard[0][0].text == "📊 Статус"  # the bottom keyboard
        await app.dp.feed_update(app.bot, _update(CHAT, "/pause"))
        assert app.db.get_state("paused") == "1"

    run(scenario())


# --- health check, logs ------------------------------------------------------------------------


def test_health_problem_thresholds():
    cfg = NotifyConfig()  # every 10 min, 30 min to alert; quiet hours every 30 min -> 60 min
    start = datetime(2026, 10, 15, 8, 0)
    ok = start + timedelta(hours=1)
    assert health_problem(ok, start, ok + timedelta(minutes=30), False, cfg) is None
    msg = health_problem(ok, start, ok + timedelta(minutes=31), False, cfg)
    assert msg.startswith("⚠️ Нет успешного прогона Bazoš уже 31 мин") and "/logs" in msg
    assert health_problem(ok, start, ok + timedelta(minutes=45), True, cfg) is None  # quiet hours: 60 min
    # Right after a restart the last success may be old: the grace period counts from the start.
    assert health_problem(start - timedelta(days=1), start, start + timedelta(minutes=20), False, cfg) is None
    assert "ещё не было" in health_problem(None, start, start + timedelta(minutes=40), False, cfg)


def test_health_check_alerts_once_and_recovers(files):
    settings, _, _ = files
    session = FakeSession()
    app = ScannerApp(settings, bot=make_bot("123456:TEST", session=session))
    app.started_at = datetime.now() - timedelta(hours=2)
    run(app.health_check())
    run(app.health_check())
    assert [c.text.split(" ")[0] for c in session.calls] == ["⚠️"]  # once, not every 5 minutes
    run_id = app.db.start_run()
    app.db.finish_run(run_id, listings=300)
    run(app.health_check())
    assert session.calls[-1].text == "✅ Сканирование снова работает" and len(session.calls) == 2
    app.db.set_state("paused", "1")
    app.started_at = datetime.now() - timedelta(days=1)
    app.db.conn.execute("DELETE FROM runs")
    run(app.health_check())
    assert len(session.calls) == 2  # paused: no alert


def test_logs_command_and_files(files, tmp_path):
    settings, _, _ = files
    assert "В логе ошибок пусто 👍" in cmd_logs(settings)
    setup_logging("INFO", settings.log_dir, Console(file=io.StringIO()))
    try:
        logging.getLogger("scanner.test").info("just info")
        logging.getLogger("scanner.test").error("bazos <search> failed")
    finally:
        for h in logging.getLogger().handlers:
            h.flush()
    assert "just info" in (settings.log_dir / "scanner.log").read_text(encoding="utf-8")
    errors = (settings.log_dir / "errors.log").read_text(encoding="utf-8")
    assert "bazos <search> failed" in errors and "just info" not in errors
    out = cmd_logs(settings)
    assert out.startswith("<b>📜 Последние ошибки") and "bazos &lt;search&gt; failed" in out
    logging.basicConfig(force=True)  # detach the file handlers again


# --- menu screens -------------------------------------------------------------------------------


def _all_callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


@pytest.mark.parametrize("name", ["home", "status", "top", "skipped", "logs", "settings", "tiers", "radius",
                                  "searches", "unknown"])
def test_every_screen_renders(files, name):
    settings, _, _ = files
    db = Database(":memory:")
    text, markup = render(name, [], settings, db)
    assert text.startswith("<b>") and markup is not None
    assert all(len(d.encode()) <= 64 for d in _all_callbacks(markup))  # Telegram's callback_data limit
    if name not in ("home", "unknown"):  # unknown names fall back to the menu itself
        assert "m:home" in _all_callbacks(markup)  # every screen leads back to the menu


def test_top_numbers_open_cards(files):
    settings, _, _ = files
    db = Database(":memory:")
    now = datetime.now()
    db.upsert(Listing("bazos", "1", "https://x/1", "iPhone 15 128GB", 7000, "Pardubice", now.date()), now)
    text, markup = render("top", [], settings, db)
    assert "<b>1.</b> ⚪" in text and "Номер ниже откроет карточку" in text
    assert _all_callbacks(markup)[0] == "m:card:bazos:1"
    card, card_kb = render("card", ["bazos", "1"], settings, db)
    assert "iPhone 15 128 ГБ — 7 000 Kč" in card and "Gemini это объявление ещё не проверял" in card
    assert _all_callbacks(card_kb)[:3] == ["c:bazos:1", "h:bazos:1", "w:bazos:1"] and "m:top" in _all_callbacks(card_kb)
    db.add_alert("bazos", "1", 7000, "deal", "the alert text", "s")
    assert render("card", ["bazos", "1"], settings, db)[0] == "the alert text"  # the alert as it was sent


def test_menu_actions_edit_config(files):
    settings, _, _ = files
    db = Database(":memory:")
    toast, screen, _ = act("tier", ["2", "1"], settings, db)
    assert toast == "Ступень 2: 79 %" and screen == "tiers"
    assert load_config(settings.config_path).pricing.max_price_tiers[1].pct == 79
    assert act("radius", ["45"], settings, db)[0] == "Радиус 45 км"
    assert any(b.text == "✅ 45" for row in render("radius", [], settings, db)[1].inline_keyboard for b in row)
    n = len(load_config(settings.config_path).bazos.searches)
    confirm, kb = render("rmsearch", ["1"], settings, db)
    assert confirm == "Удалить поиск «iphone»?" and _all_callbacks(kb) == ["a:rmsearch:1", "m:searches"]
    assert act("rmsearch", ["1"], settings, db)[0] == "Удалён поиск «iphone»"
    assert len(load_config(settings.config_path).bazos.searches) == n - 1
    assert act("pause", ["status"], settings, db)[1] == "status" and db.get_state("paused") == "1"
    assert "a:resume:home" in _all_callbacks(render("home", [], settings, db)[1])
    act("resume", [], settings, db)
    assert db.get_state("paused") == "0"


def _callback(chat_id: int, data: str) -> Update:
    msg = Message(message_id=7, date=datetime.now(), text="menu", chat=Chat(id=chat_id, type="private"))
    return Update(update_id=2, callback_query=CallbackQuery(
        id="q1", from_user=User(id=chat_id, is_bot=False, first_name="x"), chat_instance="ci", message=msg, data=data))


def test_buttons_through_the_dispatcher(files):
    settings, _, _ = files
    session = FakeSession()
    app = ScannerApp(settings, bot=make_bot("123456:TEST", session=session))

    async def scenario():
        await app.dp.feed_update(app.bot, _update(CHAT, "🏆 Топ"))  # bottom keyboard button
        assert session.calls[-1].text.startswith("<b>🏆 Топ")
        await app.dp.feed_update(app.bot, _callback(CHAT, "m:settings"))
        edit = next(c for c in session.calls if type(c).__name__ == "EditMessageText")
        assert edit.text.startswith("<b>⚙️ Настройки") and edit.message_id == 7
        await app.dp.feed_update(app.bot, _callback(CHAT, "a:tier:1:1"))
        assert load_config(settings.config_path).pricing.max_price_tiers[0].pct == 71
        assert any(type(c).__name__ == "AnswerCallbackQuery" and c.text == "Ступень 1: 71 %" for c in session.calls)
        await app.dp.feed_update(app.bot, _update(CHAT, "⏯ Пауза / старт"))
        assert app.db.get_state("paused") == "1"

    run(scenario())
