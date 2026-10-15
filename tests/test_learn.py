"""Self-learning price list (scanner.learn) and settings that survive a redeploy (scanner.overrides)."""

import asyncio
import json
import shutil
from datetime import date, datetime, timedelta

import pytest

from scanner import learn, overrides
from scanner.config import PROJECT_ROOT, LearnConfig, Settings, load_config
from scanner.db import Database
from scanner.menu import act, render
from scanner.models import Listing
from scanner.pricing import PriceList, evaluate_many
from test_pipeline import FakeBazos

NOW = datetime(2026, 10, 18, 19, 0)
CFG = LearnConfig()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def files(tmp_path):
    prices = tmp_path / "prices.json"
    shutil.copy(PROJECT_ROOT / "knowledge" / "prices.json", prices)
    text = (PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(text.replace("pricing:\n", f"pricing:\n  prices_path: {prices}\n", 1), encoding="utf-8")
    return Settings(_env_file=None, config_path=config, db_path=tmp_path / "t.db", log_dir=tmp_path / "logs"), config, prices


def listing(i: int, title: str = "iPhone 15 128GB", price: int | None = None, **kw) -> Listing:
    """Distinct prices by default: 3+ identical listings are a scam pattern for the rules."""
    price = 9000 + i * 10 if price is None else price
    base = dict(source="bazos", id=str(i), url=f"https://x/{i}", title=title, price=price, location="Pardubice",
                posted_date=NOW.date())
    return Listing(**(base | kw))


def observe(db, listings, at=NOW):
    prices = PriceList.load(PROJECT_ROOT / "knowledge" / "prices.json")
    for l in listings:
        db.upsert(l, at)
    return learn.record(db, evaluate_many(listings, prices, load_config(PROJECT_ROOT / "config.yaml").pricing,
                                          at.date()), at)


def market(label: str) -> int:
    return next(i for i in PriceList.load(PROJECT_ROOT / "knowledge" / "prices.json").items if i.label == label).market_price


# --- collecting --------------------------------------------------------------------------------


def test_record_only_confident_matches_and_unknown_models():
    db = Database(":memory:")
    n = observe(db, [
        listing(1),                                       # iPhone 15 128 GB, high
        listing(2, "iPhone 15"),                          # no storage: low confidence, skipped
        listing(3, price=1500),                           # too cheap, skipped
        listing(4, "Kryt na iPhone 15 128GB", 300),       # accessory, skipped
        listing(5, "iPhone 11 Pro 64GB", 4500),           # recognized, not in the price list
    ])
    assert n == 1
    [obs] = db.conn.execute("SELECT item, price FROM price_observations").fetchall()
    assert tuple(obs) == ("iPhone 15 128 ГБ", 9010)
    [unknown] = db.conn.execute("SELECT model, price FROM unknown_models").fetchall()
    assert tuple(unknown) == ("iphone 11 pro 64gb", 4500)


def test_record_keeps_one_row_per_listing_with_the_last_price():
    db = Database(":memory:")
    observe(db, [listing(1, price=9500)], NOW - timedelta(days=2))
    observe(db, [listing(1, price=9000)])
    rows = db.conn.execute("SELECT price, first_seen, last_seen FROM price_observations").fetchall()
    assert len(rows) == 1 and rows[0]["price"] == 9000 and rows[0]["first_seen"] < rows[0]["last_seen"]


def test_check_sold_marks_deleted_listings():
    db = Database(":memory:")
    observe(db, [listing(1), listing(2), listing(3)], NOW - timedelta(days=2))
    observe(db, [listing(3)])  # still in the results: not checked
    client = FakeBazos([], gone={"https://x/1"})
    assert asyncio.run(learn.check_sold(db, client, CFG, NOW)) == 1
    assert sorted(client.gets) == ["https://x/1", "https://x/2"]
    gone = dict(db.conn.execute("SELECT listing_id, gone_at IS NOT NULL FROM price_observations").fetchall())
    assert gone == {"1": 1, "2": 0, "3": 0}
    client = FakeBazos([], gone={"https://x/1"})
    asyncio.run(learn.check_sold(db, client, CFG, NOW + timedelta(days=1)))
    assert client.gets == []  # 2 was checked a day ago (recheck after 3 days), 1 is already gone
    asyncio.run(learn.check_sold(db, client, CFG, NOW + timedelta(days=4)))
    assert sorted(client.gets) == ["https://x/2", "https://x/3"]  # 2 rechecked, 3 left the results meanwhile


# --- the report ----------------------------------------------------------------------------------


def test_report_from_asking_prices_is_capped_at_8_pct():
    db = Database(":memory:")
    observe(db, [listing(i, price=p) for i, p in enumerate([8000, 8200, 8400, 8500, 8800, 9000], 1)])
    report = learn.build_report(db, PriceList.load(PROJECT_ROOT / "knowledge" / "prices.json"), CFG, NOW)
    [p] = report.proposals
    m = market("iPhone 15 128 ГБ")
    # median asking 8 450 × 95 % = 8 027, but at most −8 %
    assert (p.market, p.ask_median, p.asks, p.sold) == (m, 8450, 6, 0)
    assert p.proposed == -(-m * 0.92 // 100) * 100 and p.basis == "объявления × 95 %"
    assert p.change_pct >= -8


def test_report_uses_sales_when_there_are_enough():
    db = Database(":memory:")
    m = market("iPhone 15 128 ГБ")
    observe(db, [listing(i, price=m + i) for i in range(1, 7)])
    db.conn.execute("UPDATE price_observations SET gone_at = ? WHERE listing_id IN ('1', '2', '3')", (NOW.isoformat(),))
    db.conn.execute("UPDATE price_observations SET price = ? WHERE listing_id IN ('1', '2', '3')", (m - 400,))
    [p] = learn.build_report(db, PriceList.load(PROJECT_ROOT / "knowledge" / "prices.json"), CFG, NOW).proposals
    assert (p.sold, p.sold_median, p.basis, p.proposed) == (3, m - 400, "ушло за", m - 400)


def test_report_small_change_is_stable_and_few_observations_wait():
    db = Database(":memory:")
    m = market("iPhone 15 128 ГБ")
    observe(db, [listing(i, price=int(m / 0.95) + i) for i in range(1, 7)])  # × 95 % ≈ market: no change
    observe(db, [listing(10 + i, "iPhone 14 128GB", 7000 + i) for i in range(3)])  # only 3 observations
    report = learn.build_report(db, PriceList.load(PROJECT_ROOT / "knowledge" / "prices.json"), CFG, NOW)
    assert report.proposals == [] and report.stable == 1 and report.observed_items == 2
    assert "Предложений нет" in learn.format_report(report, CFG)


def test_report_new_models_and_estimate_first():
    db = Database(":memory:")
    observe(db, [listing(i, "iPhone 11 Pro 64GB", 4000 + i * 100) for i in range(1, 5)])
    report = learn.build_report(db, PriceList.load(PROJECT_ROOT / "knowledge" / "prices.json"), CFG, NOW)
    [m] = report.new_models
    assert (m.model, m.listings, m.median_price) == ("iphone 11 pro 64gb", 4, 4250)
    assert "🆕 Часто встречаются, но нет в прайсе" in learn.format_report(report, CFG)


@pytest.mark.parametrize("asks, market_label", [(range(4700, 4760, 10), "PlayStation 4 Slim 500 ГБ / 1 ТБ")])
def test_cap_holds_after_rounding(asks, market_label):
    db = Database(":memory:")
    observe(db, [listing(i, "PS4 Slim 1TB", p) for i, p in enumerate(asks, 1)])
    [p] = learn.build_report(db, PriceList.load(PROJECT_ROOT / "knowledge" / "prices.json"), CFG, NOW).proposals
    assert p.item == market_label and 0 < p.change_pct <= 8 and p.proposed % 100 == 0


def test_bare_family_is_not_suggested():
    db = Database(":memory:")
    observe(db, [listing(i, "MacBook Air 2017 8GB 128GB", 6000 + i) for i in range(1, 5)])
    report = learn.build_report(db, PriceList.load(PROJECT_ROOT / "knowledge" / "prices.json"), CFG, NOW)
    assert report.new_models == []


# --- applying and the menu -------------------------------------------------------------------


def _report_with_two(settings, db):
    observe(db, [listing(i, price=8000 + i) for i in range(1, 7)])
    observe(db, [listing(10 + i, "iPhone 14 128GB", 5000 + i) for i in range(6)])
    text, kb = render("prices", [], settings, db)
    return text, kb


def test_menu_report_apply_selected(files):
    settings, _, prices = files
    db = Database(":memory:")
    text, kb = _report_with_two(settings, db)
    assert text.startswith("<b>📈 Отчёт по ценам</b>") and "1. iPhone" in text and "2. iPhone" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas[:2] == ["a:papply:1:all", "m:pselect:1"]
    act("ptoggle", ["1", "1"], settings, db)  # unselect the first
    first = learn.proposals(db, 1)[0]
    toast, screen, args = act("papply", ["1", "sel"], settings, db)
    assert toast == "Применено: 1" and (screen, args) == ("preport", ["1"])
    items = {f"{i['model']} {i['variant']}".strip(): i["market_price"]
             for i in json.loads(prices.read_text(encoding="utf-8"))["items"]}
    second = learn.proposals(db, 1)[1]
    assert items[first["item"]] == first["old_price"] and items[second["item"]] == second["new_price"]
    assert "✅ применено" in render("preport", ["1"], settings, db)[0]
    assert db.conn.execute("SELECT COUNT(*) FROM price_changes").fetchone()[0] == 1


def test_apply_skips_items_changed_since_the_report(files):
    settings, _, prices = files
    db = Database(":memory:")
    _report_with_two(settings, db)
    p = learn.proposals(db, 1)[0]
    index, _ = overrides.item_price(prices, p["item"])
    overrides.set_market_price(settings, db, index, p["old_price"] + 300)
    notes = learn.apply(settings, db, 1, only_selected=False)
    assert notes[0].endswith("пропущено") and "→" in notes[1]


# --- overrides survive a redeploy ------------------------------------------------------------


def test_overrides_replay_after_redeploy(files):
    settings, config, prices = files
    db = Database(":memory:")
    repo_config, repo_prices = config.read_text(encoding="utf-8"), prices.read_text(encoding="utf-8")
    overrides.set_radius(settings, db, 45)
    overrides.set_radius(settings, db, 30)  # the base stays the repo's 60
    overrides.set_tier_pct(settings, db, 2, 80)
    overrides.add_search(settings, db, "mobil", "galaxy s24", 3000, 20000)
    overrides.remove_search(settings, db, "airpods")
    index, old = overrides.item_price(prices, "iPhone 15 128 ГБ")
    overrides.set_market_price(settings, db, index, old - 500)
    assert json.loads(db.conn.execute("SELECT base FROM overrides WHERE kind = 'radius'").fetchone()[0]) == 60

    config.write_text(repo_config, encoding="utf-8")  # a redeploy brings the repo's files back
    prices.write_text(repo_prices, encoding="utf-8")
    notes = overrides.replay(settings, db)
    assert notes == ["радиус 30 км", "ступень 2 → 80 %", "поиск «galaxy s24» добавлен", "поиск «airpods» удалён",
                     f"iPhone 15 128 ГБ: {old - 500} Kč"]
    cfg = load_config(settings.config_path)
    assert cfg.bazos.radius_km == 30 and cfg.pricing.max_price_tiers[1].pct == 80
    names = [s.name for s in cfg.bazos.searches]
    assert "galaxy s24" in names and "airpods" not in names
    assert overrides.item_price(prices, "iPhone 15 128 ГБ")[1] == old - 500
    assert overrides.replay(settings, db) == []  # already applied: nothing to say


def test_repo_change_wins_over_an_override(files):
    settings, config, _ = files
    db = Database(":memory:")
    repo = config.read_text(encoding="utf-8")
    overrides.set_radius(settings, db, 45)
    config.write_text(repo.replace("radius_km: 60", "radius_km: 70"), encoding="utf-8")  # changed in the repo
    assert overrides.replay(settings, db) == []
    assert load_config(settings.config_path).bazos.radius_km == 70 and db.overrides() == []


def test_back_to_the_repo_value_drops_the_override(files):
    settings, _, _ = files
    db = Database(":memory:")
    overrides.set_radius(settings, db, 45)
    overrides.set_radius(settings, db, 60)
    assert db.overrides() == []
