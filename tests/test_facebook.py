"""Facebook Marketplace source: pure parsers, the pipeline with source "fb", the bot's login-wall stop,
and the Playwright client against local HTML (FB itself is not reachable from tests)."""

import asyncio
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from scanner.config import PROJECT_ROOT, AppConfig, GeminiConfig, PricingConfig, Settings, load_config
from scanner.db import Database
from scanner.gemini import GeminiReviewer
from scanner.models import ListingDetail
from scanner.pipeline import run_scan
from scanner.sources import facebook as fbmod
from scanner.sources.bazos import ScanResult
from scanner.sources.facebook import (
    FbLoginRequired, allowed_cities, login_required, nearby, parse_card, parse_cards, parse_detail_text,
    parse_price, search_url,
)

TODAY = date(2026, 10, 15)
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
CFG = load_config(PROJECT_ROOT / "config.yaml")


@pytest.mark.parametrize(
    "text, price, title, city",
    [
        ("Недавно опубликовано\n12 500 CZK\n14 000 CZK\niPhone 15 Pro 128GB\nPardubice, Pardubický kraj",
         12500, "iPhone 15 Pro 128GB", "Pardubice"),          # Russian UI, new + old price
        ("Just listed\nCZK 6,900\niPhone 15 128GB\nÚstí nad Orlicí", 6900, "iPhone 15 128GB", "Ústí nad Orlicí"),
        ("9 990 Kč\nPS5 Slim\nChrudim", 9990, "PS5 Slim", "Chrudim"),
        ("Бесплатно\nKrabice od iPhone\nSvitavy", None, "Krabice od iPhone", "Svitavy"),  # free: no price
        ("15 000 CZK\nRTX 3080 10GB", 15000, "RTX 3080 10GB", None),  # no city line
    ],
)
def test_parse_card(text, price, title, city):
    l = parse_card("/marketplace/item/123/?ref=search", text, TODAY)
    assert (l.source, l.id, l.url) == ("fb", "123", "https://www.facebook.com/marketplace/item/123/")
    assert (l.price, l.title, l.location, l.posted_date) == (price, title, city, TODAY)


def test_title_with_a_number_is_not_a_price():
    l = parse_card("/marketplace/item/9/", "4 500 CZK\nPS4 Pro 1 TB + 2 ovladače 2500 Kč hry\nPardubice", TODAY)
    assert l.price == 4500 and l.title.startswith("PS4 Pro")


def test_parse_card_rejects_non_items():
    assert parse_card("/groups/1/", "12 CZK\nX", TODAY) is None
    assert parse_card("/marketplace/item/1/", "12 500 CZK\nJust listed", TODAY) is None  # no title


@pytest.mark.parametrize("text, value", [("12 500 CZK", 12500), ("CZK 12,500", 12500), ("9 990 Kč", 9990),
                                         ("Free", None), ("CZK 1", 1)])
def test_parse_price(text, value):
    assert parse_price(text) == value


def test_nearby_cities_only():
    cards = [{"href": f"/marketplace/item/{i}/", "text": f"5 000 CZK\nPS5\n{city}"}
             for i, city in enumerate(["Pardubice", "Praha", "Usti nad Orlici", "Brno, Jihomoravský kraj"])]
    cards.append({"href": "/marketplace/item/9/", "text": "5 000 CZK\nPS5"})  # no city: dropped
    cfg = AppConfig(pricing=PricingConfig(home_cities=["Ústí nad Orlicí"], near_cities=["Pardubice"]))
    kept = nearby(parse_cards(cards, TODAY), allowed_cities(cfg))
    assert [l.location for l in kept] == ["Pardubice", "Usti nad Orlici"]


def test_search_url_uses_bazos_searches():
    iphone, graficka = CFG.bazos.searches[0], next(s for s in CFG.bazos.searches if s.category)
    url = search_url(iphone, CFG.facebook)
    assert url.startswith("https://www.facebook.com/marketplace/search?query=iphone&daysSinceListed=1")
    assert "minPrice=2000" in url and "maxPrice=45000" in url
    assert "query=graficka" in search_url(graficka, CFG.facebook)  # a category search uses its name


def test_parse_detail_text():
    text = ("iPhone 15 128GB černý\n7 000 CZK\nОпубликовано 3 часа назад в Pardubice\n"
            "Prodám iPhone 15 128GB, baterie 91 %, bez škrábanců, krabice a nabíječka.\n"
            "Информация о продавце\nJan Novák")
    d = parse_detail_text("https://x", text)
    assert (d.title, d.price) == ("iPhone 15 128GB černý", 7000)
    assert d.description.startswith("Prodám iPhone 15") and "Jan Novák" not in d.description
    sold = parse_detail_text("https://x", "PS5\n9 000 CZK\nПродано\n" + "x" * 50)
    assert sold.description.endswith("Prodáno")


@pytest.mark.parametrize("url, form, wall", [
    ("https://www.facebook.com/marketplace/112/search?query=x", False, False),
    ("https://www.facebook.com/login/?next=marketplace", False, True),
    ("https://www.facebook.com/checkpoint/828281030927956/", False, True),
    ("https://www.facebook.com/marketplace/", True, True),
])
def test_login_required(url, form, wall):
    assert login_required(url, form) is wall


# --- pipeline with source "fb" ------------------------------------------------------------------


class FakeFb:
    source = "fb"

    def __init__(self, listings, error=None):
        self.listings, self.error, self.gets = listings, error, []

    async def scan(self):
        if self.error:
            raise self.error
        return ScanResult([replace(l) for l in self.listings], [])

    async def fetch_detail(self, url):
        return ListingDetail(url, "", None, "Prodám, stav jako nový.", None)

    async def get(self, url):  # must never be used for FB
        self.gets.append(url)
        return ""


def test_pipeline_fb_listing_alerts_without_bazos_checks():
    db = Database(":memory:")
    listing = parse_card("/marketplace/item/555/", "7 000 CZK\niPhone 15 128GB\nPardubice", TODAY)
    db.upsert(replace(listing, id="old", url="https://www.facebook.com/marketplace/item/old/"), datetime(2026, 10, 1))
    db.set_status("fb", "old", "contacted")
    client = FakeFb([listing])
    report = asyncio.run(run_scan(
        Settings(_env_file=None, gemini_api_key=None), CFG, db, client, now=lambda: datetime(2026, 10, 15, 12, 0),
        reviewer_factory=lambda s, c, p, pr, d: GeminiReviewer(s, GeminiConfig(enabled=False), p, pr, d),
    ))
    [alert] = db.pending_alerts()
    assert alert["source"] == "fb" and "(FB, Pardubice, сегодня)" in alert["text"]
    assert client.gets == []  # no "gone" / sold checks over HTTP for FB
    assert db.last_runs(1, source="fb")[0]["listings"] == 1 and db.last_runs(1) == []
    assert report.candidates == 1


# --- the bot stops FB at a login wall ------------------------------------------------------------


def test_bot_stops_fb_at_login_wall(tmp_path, monkeypatch):
    from scanner import bot as botmod
    from test_bot import CHAT, FakeSession

    for name in ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db", log_dir=tmp_path / "logs", fb_enabled=True,
                        telegram_bot_token="123456:TEST", telegram_chat_id=CHAT)
    session = FakeSession()
    app = botmod.ScannerApp(settings, bot=botmod.make_bot("123456:TEST", session=session))
    monkeypatch.setattr(botmod, "in_quiet_hours", lambda now, cfg: False)

    async def browser_ok(settings):
        return []

    monkeypatch.setattr(botmod, "ensure_chromium", browser_ok)

    class Wall(FakeFb):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    opened = []

    def factory(*a, **kw):
        opened.append(1)
        return Wall([], error=FbLoginRequired("FB requires a login (https://www.facebook.com/login/)"))

    monkeypatch.setattr(botmod, "FacebookClient", factory)
    asyncio.run(app.tick_fb())
    assert app.db.get_state("fb_blocked") == "1"
    msg = session.calls[-1]
    assert "Facebook требует входа" in msg.text and "fb-login" in msg.text
    assert msg.reply_markup.inline_keyboard[0][0].callback_data == "a:fbretry"
    asyncio.run(app.tick_fb())
    assert len(opened) == 1  # blocked: no new attempt until the owner says so
    from scanner.menu import act, fb_status_line
    assert "ждёт входа" in fb_status_line(settings, app.cfg(), app.db)
    act("fbretry", [], settings, app.db)
    assert app.db.get_state("fb_blocked") == "0"


# --- the Playwright client on local pages --------------------------------------------------------


def _browser_ok() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _browser_ok(), reason="playwright not installed (pip install -e '.[fb]')")
def test_client_reads_cards_and_stops_at_login_wall(tmp_path, monkeypatch):
    pages = {"search": (FIXTURES / "fb_search.html").as_uri(), "login": (FIXTURES / "fb_login.html").as_uri(),
             "item": (FIXTURES / "fb_item.html").as_uri()}
    monkeypatch.setattr(fbmod, "search_url", lambda search, cfg: pages["search"])
    cfg = AppConfig(pricing=PricingConfig(home_cities=["Ústí nad Orlicí"], near_cities=["Pardubice"]),
                    bazos=CFG.bazos.model_copy(update={"searches": CFG.bazos.searches[:1]}))

    async def scenario():
        try:
            client = fbmod.FacebookClient(cfg, tmp_path / "profile", headless=True)
            await client.__aenter__()
        except Exception as exc:  # noqa: BLE001 - no Chromium in this environment
            pytest.skip(f"Chromium not available: {exc}")
        try:
            async def no_pause(lo, hi):
                return None
            client._pause = no_pause
            result = await client.scan()
            assert [(l.id, l.price, l.location) for l in result.listings] == [
                ("111", 12500, "Pardubice"), ("333", 6900, "Ústí nad Orlicí")]  # Praha dropped, dup removed
            detail = await client.fetch_detail(pages["item"])
            assert detail.price == 7000 and detail.description.startswith("Prodám iPhone 15")
            with pytest.raises(FbLoginRequired):
                await client._open(pages["login"])
        finally:
            await client.__aexit__(None, None, None)

    asyncio.run(scenario())


# --- a session sent from a PC ------------------------------------------------------------------

STATE = {"cookies": [
    {"name": "c_user", "value": "1000123", "domain": ".facebook.com", "path": "/", "expires": 2000000000,
     "httpOnly": False, "secure": True, "sameSite": "None"},
    {"name": "xs", "value": "abc", "domain": ".facebook.com", "path": "/", "expires": 2000000000,
     "httpOnly": True, "secure": True, "sameSite": "None"},
], "origins": []}


def test_valid_state():
    assert fbmod.valid_state(STATE)
    assert not fbmod.valid_state({"cookies": [{"name": "datr", "domain": ".facebook.com"}]})
    assert not fbmod.valid_state([]) and not fbmod.valid_state({"cookies": "x"})


def test_import_fb_state_from_telegram(tmp_path, monkeypatch):
    import io
    import json as _json

    from scanner import bot as botmod

    settings = Settings(_env_file=None, fb_enabled=True, fb_state_path=tmp_path / "data" / "fb_state.json")

    class FakeBot:
        def __init__(self, payload):
            self.payload = payload

        async def download(self, file_id):
            return io.BytesIO(self.payload)

    ok = asyncio.run(botmod.import_fb_state(FakeBot(_json.dumps(STATE).encode()), "f1", settings))
    assert ok.startswith("✅") and _json.loads(settings.fb_state_path.read_text())["cookies"][0]["name"] == "c_user"
    bad = asyncio.run(botmod.import_fb_state(FakeBot(b'{"cookies": []}'), "f2", settings))
    assert "c_user" in bad
    assert "не JSON" in asyncio.run(botmod.import_fb_state(FakeBot(b"\xff\xfe"), "f3", settings))


@pytest.mark.skipif(not _browser_ok(), reason="playwright not installed")
def test_client_imports_the_session_once(tmp_path):
    import json as _json

    state = tmp_path / "fb_state.json"
    state.write_text(_json.dumps(STATE), encoding="utf-8")

    async def scenario():
        try:
            client = fbmod.FacebookClient(CFG, tmp_path / "profile", headless=True, state_path=state)
            await client.__aenter__()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {exc}")
        try:
            assert await client.is_logged_in()
        finally:
            await client.__aexit__(None, None, None)

    asyncio.run(scenario())
    assert not state.exists() and (tmp_path / "fb_state.imported.json").exists()  # not imported again
