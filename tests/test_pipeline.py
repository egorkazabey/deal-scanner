"""Scan pipeline: which candidates become Telegram alerts (fresh / price drop / watch / gone)."""

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta

from scanner.config import AppConfig, GeminiConfig, NotifyConfig, PricingConfig, Settings
from scanner.db import Database, Status
from scanner.gemini import GeminiReviewer, Review
from scanner.models import Listing, ListingDetail
from scanner.notify import format_alert, split_messages
from scanner.pipeline import run_scan
from scanner.pricing import PriceList, evaluate
from scanner.sources.bazos import BazosError, ScanResult
from test_gemini import FakeClient, response, verdict

TODAY = date(2026, 10, 15)
START = datetime(2026, 10, 15, 14, 0)
PRICING = PricingConfig()
PRICES = PriceList.load(PRICING.prices_path)
SETTINGS = Settings(_env_file=None, gemini_api_key=None, telegram_bot_token=None, telegram_chat_id=None)


def listing(**kw) -> Listing:
    base = dict(source="bazos", id="1", url="https://mobil.bazos.cz/inzerat/1/iphone-15.php",
                title="iPhone 15 128GB", price=7000, location="Pardubice", posted_date=TODAY,
                description="Pěkný stav, baterie 90 %.")
    return Listing(**(base | kw))


class FakeBazos:
    def __init__(self, listings, gone=()):
        self.listings, self.gone = listings, set(gone)
        self.gets: list[str] = []

    async def scan(self):
        return ScanResult([replace(l) for l in self.listings], [])

    async def fetch_detail(self, url):
        return ListingDetail(url, "", None, "Popis.", None)

    async def get(self, url):
        self.gets.append(url)
        if url in self.gone:
            raise BazosError(f"GET {url} -> HTTP 410", 410)
        return "<html></html>"


def no_ai(settings, cfg, pricing, prices, db):
    return GeminiReviewer(settings, GeminiConfig(enabled=False), pricing, prices, db)


def scan(db, listings, *, at=START, cfg=None, gone=(), factory=no_ai, **kw):
    client = FakeBazos(listings, gone)
    report = asyncio.run(run_scan(SETTINGS, cfg or AppConfig(), db, client, now=lambda: at,
                                  reviewer_factory=factory, **kw))
    return report, client


def test_fresh_candidate_alerts_once():
    db = Database(":memory:")
    report, _ = scan(db, [listing()])
    [alert] = db.pending_alerts()
    assert alert["kind"] == "deal" and alert["price"] == 7000 and not alert["quiet"]
    assert "iPhone 15 128 ГБ — 7 000 Kč" in alert["text"] and "без ИИ" in alert["text"]
    assert db.get("bazos", "1")["status"] == Status.ALERTED
    report, _ = scan(db, [listing()], at=START + timedelta(minutes=10))
    assert report.alerts == [] and len(db.pending_alerts()) == 1


def test_older_listing_only_on_price_drop():
    db = Database(":memory:")
    old = listing(posted_date=TODAY - timedelta(days=5), price=7500)
    report, _ = scan(db, [old])
    assert report.candidates == 1 and report.alerts == []  # 5 days old: not fresh
    report, _ = scan(db, [replace(old, price=7000)], at=START + timedelta(hours=1))
    [alert] = db.pending_alerts()
    assert alert["kind"] == "drop" and "📉 7 500 → " in alert["text"]


def test_stale_never_alerts_even_with_drop():
    db = Database(":memory:")
    stale = listing(posted_date=TODAY - timedelta(days=20), price=7500)
    scan(db, [stale])
    report, _ = scan(db, [replace(stale, price=6500)], at=START + timedelta(hours=1))
    assert report.candidates == 1 and report.alerts == []


def test_old_drop_does_not_count():
    db = Database(":memory:")
    old = listing(posted_date=TODAY - timedelta(days=10), price=7500)
    scan(db, [old], at=START - timedelta(days=5))
    scan(db, [replace(old, price=7000)], at=START - timedelta(days=4))  # drop 4 days ago
    report, _ = scan(db, [replace(old, price=7000)])
    assert report.alerts == []


def test_hidden_and_contacted_get_no_deal_alerts():
    db = Database(":memory:")
    db.upsert(listing(id="2", url="https://x/2"))
    db.set_status("bazos", "2", Status.HIDDEN)
    db.upsert(listing(id="3", url="https://x/3"))
    db.set_status("bazos", "3", Status.CONTACTED)
    report, _ = scan(db, [listing(id="2", url="https://x/2"), listing(id="3", url="https://x/3")])
    assert report.candidates == 2 and report.alerts == []


def test_gemini_skip_is_not_sent_nor_asked_again():
    db = Database(":memory:")
    client = FakeClient([response(verdict(verdict="skip", reason_ru="Это муляж."))])

    def factory(settings, cfg, pricing, prices, db_):
        return GeminiReviewer(Settings(_env_file=None, gemini_api_key="x"), cfg, pricing, prices, db_, client=client)

    report, _ = scan(db, [listing()], factory=factory)
    assert report.alerts == [] and len(client.calls) == 1
    report, _ = scan(db, [listing()], factory=factory, at=START + timedelta(minutes=10))
    assert report.alerts == [] and len(client.calls) == 1  # cached skip: no new request


def test_gemini_verdict_goes_into_alert():
    db = Database(":memory:")
    client = FakeClient([response(verdict(offer_price=6400, message_cs="Dobrý den, šlo by to za 6400 Kč?"))])

    def factory(settings, cfg, pricing, prices, db_):
        return GeminiReviewer(Settings(_env_file=None, gemini_api_key="x"), cfg, pricing, prices, db_, client=client)

    scan(db, [listing()], factory=factory)
    [alert] = db.pending_alerts()
    assert alert["text"].startswith("🟢 ")
    assert "Рынок ~11 000 · предложить 6 400 · максимум 8 500" in alert["text"]
    assert "<code>Dobrý den, šlo by to za 6400 Kč?</code>" in alert["text"]


def test_watch_alerts_on_5_pct_drop_regardless_of_age():
    db = Database(":memory:")
    old = listing(posted_date=TODAY - timedelta(days=10), price=7000)
    scan(db, [old])
    db.set_watch("bazos", "1", 7000)
    scan(db, [replace(old, price=6800)], at=START + timedelta(hours=1))  # -2.9 %: an ordinary drop alert
    assert [r["kind"] for r in db.pending_alerts()] == ["drop"]
    report, _ = scan(db, [replace(old, price=6600)], at=START + timedelta(hours=2))  # -5.7 %
    [alert] = [r for r in db.pending_alerts() if r["id"] in report.alerts]
    assert alert["kind"] == "watch" and "🔁 Цена снизилась" in alert["text"] and "📉 7 000 → " in alert["text"]
    assert db.watches()[("bazos", "1")] == 6600  # the next alert counts from the new price


def test_contacted_listing_gone():
    db = Database(":memory:")
    gone = listing(id="9", url="https://x/9", title="PS5 Slim", price=8000)
    db.upsert(gone, START - timedelta(days=1))
    db.set_status("bazos", "9", Status.CONTACTED)
    report, client = scan(db, [listing()], gone={"https://x/9"})
    kinds = [r["kind"] for r in db.pending_alerts()]
    assert kinds == ["deal", "gone"] and client.gets == ["https://x/9"]
    assert "❌ Пропало объявление" in db.pending_alerts()[1]["text"]
    _, client = scan(db, [listing()], gone={"https://x/9"}, at=START + timedelta(hours=2))
    assert client.gets == []  # already gone: not checked again


def test_contacted_listing_checked_at_most_hourly():
    db = Database(":memory:")
    db.upsert(listing(id="9", url="https://x/9"), START - timedelta(days=1))
    db.set_status("bazos", "9", Status.CONTACTED)
    _, client = scan(db, [])
    assert client.gets == ["https://x/9"]
    _, client = scan(db, [], at=START + timedelta(minutes=10))
    assert client.gets == []
    _, client = scan(db, [], at=START + timedelta(minutes=61))
    assert client.gets == ["https://x/9"]


def test_max_alerts_per_run_and_rest_next_time():
    db = Database(":memory:")
    many = [listing(id=str(i), url=f"https://x/{i}", price=6000 + i * 100) for i in range(5)]
    cfg = AppConfig(notify=NotifyConfig(max_alerts_per_run=2))
    report, _ = scan(db, many, cfg=cfg)
    assert len(report.alerts) == 2
    report, _ = scan(db, many, cfg=cfg, at=START + timedelta(minutes=10))
    assert len(report.alerts) == 2 and len(db.pending_alerts()) == 4


def test_dry_run_stores_nothing():
    db = Database(":memory:")
    report, _ = scan(db, [listing()], dry=True)
    assert report.alerts == [] and len(report.previews) == 1 and db.pending_alerts() == []


def test_quiet_flag_and_run_log():
    db = Database(":memory:")
    scan(db, [listing()], quiet=True)
    assert db.pending_alerts()[0]["quiet"] == 1
    [run] = db.last_runs()
    assert (run["listings"], run["new"], run["candidates"], run["alerts"]) == (1, 1, 1, 1)


# --- message format ----------------------------------------------------------------------------


def test_format_alert_rules_only():
    ev = evaluate(listing(title="iPhone 15 128GB <top>", price=7000), PRICES, PRICING, TODAY)
    text, summary = format_alert("deal", Review(ev, no_ai_reason="лимит"), PRICING, TODAY)
    lines = text.split("\n")
    assert lines[0] == "⚪ <b>iPhone 15 128 ГБ — 7 000 Kč</b>  (Bazoš, Pardubice, сегодня)"
    assert lines[1] == "Рынок ~10 000 · предложить 6 400 · максимум 7 800"
    assert lines[2] == "Прибыль ~3 000–3 600 · ликвидность ●●●"
    assert "🤖 без ИИ: лимит" in text and "Šlo by to za 6400 Kč" in text
    assert text.endswith("🔗 https://mobil.bazos.cz/inzerat/1/iphone-15.php")
    assert summary.startswith('⚪ <a href="https://mobil.bazos.cz/inzerat/1/iphone-15.php">')


def test_split_messages_respects_limit():
    parts = split_messages("H", ["x" * 30] * 10, limit=100)
    assert all(len(p) <= 100 for p in parts) and parts[0].startswith("H\n")
    assert sum(p.count("x" * 30) for p in parts) == 10


def test_price_increase_is_not_alerted_again():
    db = Database(":memory:")
    scan(db, [listing(price=7000)])
    report, _ = scan(db, [listing(price=7500)], at=START + timedelta(minutes=10))
    assert report.candidates == 1 and report.alerts == []  # still fresh, but dearer than what was sent
    report, _ = scan(db, [listing(price=6500)], at=START + timedelta(minutes=20))
    assert [db.conn.execute("SELECT kind FROM alerts WHERE id = ?", (i,)).fetchone()[0] for i in report.alerts] \
        == ["drop"]


class DeletedDetail(FakeBazos):
    async def fetch_detail(self, url):
        raise BazosError(f"GET {url} -> HTTP 410", 410)


def test_listing_deleted_before_its_detail_gets_no_alert():
    db = Database(":memory:")
    client = DeletedDetail([listing(description=None)])
    report = asyncio.run(run_scan(SETTINGS, AppConfig(), db, client, now=lambda: START, reviewer_factory=no_ai))
    assert report.alerts == [] and report.reviews[0].rules_skip == "объявление удалено (HTTP 410)"


def test_fb_listing_keeps_the_day_it_was_first_seen():
    db = Database(":memory:")
    fb = listing(source="fb", id="9", url="https://www.facebook.com/marketplace/item/9/", posted_date=TODAY)
    scan(db, [fb])
    later = TODAY + timedelta(days=20)  # FB cards always say "today"
    report, _ = scan(db, [replace(fb, posted_date=later)], at=START + timedelta(days=20))
    assert db.get("fb", "9")["posted_date"] == TODAY.isoformat()
    assert "stale" in report.evaluations[0].flags
