"""Bot: quiet hours, delivery, commands, config.yaml / prices.json edits, chat filter."""

import asyncio
import json
import shutil
from datetime import datetime, timedelta

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from scanner import config_edit
from scanner.bot import (
    ScannerApp, cmd_pct, cmd_price, cmd_radius, cmd_search, cmd_skipped, cmd_status, cmd_top, deliver,
    in_quiet_hours, make_bot,
)
from scanner.config import PROJECT_ROOT, AppConfig, NotifyConfig, Settings, load_config
from scanner.db import Database, Status
from scanner.gemini import PRAGUE
from scanner.models import Listing

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
    settings = Settings(_env_file=None, config_path=config, db_path=tmp_path / "t.db",
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
    assert "Объявлений 2: новых 2" in status and "GEMINI_API_KEY не задан" in status
    top = cmd_top(cfg, db, now)
    assert "<b>Топ-1 за 24 ч</b>" in top and "<b>Залежавшиеся (> 14 дн.)</b>" in top
    assert top.index("https://x/1") < top.index("Залежавшиеся") < top.index("https://x/2")
    db.set_status("bazos", "1", Status.HIDDEN)
    assert "https://x/1" not in cmd_top(cfg, db, now)
    assert cmd_skipped(db) == "Gemini за 24 ч ничего не отбраковал"


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
        assert len(session.calls) == 1 and "/status" in session.calls[0].text
        await app.dp.feed_update(app.bot, _update(CHAT, "/pause"))
        assert app.db.get_state("paused") == "1"

    run(scenario())
