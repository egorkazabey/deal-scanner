"""Rule-based pricing: title normalization, price list matching, flags, metrics and score."""

from datetime import date

import pytest

from scanner.config import PROJECT_ROOT, PricingConfig
from scanner.models import Listing
from scanner.pricing import (
    PriceList,
    evaluate,
    evaluate_many,
    is_accessory,
    identify,
    normalize,
    rank,
    stop_reasons,
)

TODAY = date(2026, 10, 15)
CFG = PricingConfig(home_cities=["Ústí nad Orlicí", "Choceň"], near_cities=["Pardubice", "Svitavy"])


@pytest.fixture(scope="module")
def prices() -> PriceList:
    return PriceList.load(PROJECT_ROOT / "data" / "prices.json")


def make(title: str, price: int | None, location: str = "Pardubice", **kw) -> Listing:
    kw.setdefault("posted_date", TODAY)
    return Listing(source=kw.pop("source", "bazos"), id=kw.pop("id", "1"), url="https://x/1",
                   title=title, price=price, location=location, **kw)


# --- normalization ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Iphone 16pro 256gb", "iphone 16 pro 256 gb"),
        ("iPhone16ProMax 1TB", "iphone 16 pro max 1 tb"),
        ("Zánovní Apple iPhone", "zanovni apple iphone"),
        ("iPhone 13 128 ГБ", "iphone 13 128 gb"),
        ("MacBook Air 13″ M1", "macbook air 13 inch m1"),
        ("XFX 308 AMD RX6600XT 8 GB", "xfx 308 amd rx 6600 xt 8 gb"),
        ("PlayStation 5 Digital Edition", "ps5 digital"),
        ("AirPods Pro (2. generace) USB-C", "airpods pro 2 gen usbc"),
        ("Apple Watch SE2 40mm", "apple watch se 2 40 mm"),
        ("iPad Pro 12,9\"", "ipad pro 12.9 inch"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_every_price_list_item_is_parseable(prices):
    assert prices.unmatched_items() == []


# --- matching: real Bazoš titles (tests/fixtures, 15.10.2026) ---------------------------------
# (title, expected price list label or None, expected match confidence)

REAL_TITLES = [
    ("iPhone 14 128GB - stav A-, nová baterie, záruka", "iPhone 14 128 ГБ", "high"),
    ("IPhone 13 Pro 128 gb", "iPhone 13 Pro 128 ГБ", "high"),
    ("Iphone 16pro 256gb TOP STAV bez škábanečků", "iPhone 16 Pro 256 ГБ", "high"),
    ("iPhone 13 Pro Max 128GB – 100% baterie, záruka", None, None),  # only 256 GB in price list
    ("IPhone 16 - 128 GB", "iPhone 16 128 ГБ", "high"),
    ("apple iphone 16 pro", "iPhone 16 Pro 128 ГБ", "low"),
    ("iPhone 14 Pro 128GB vesmírně černá (eSIM)", "iPhone 14 Pro 128 ГБ", "high"),
    ("Apple iPhone 13 mini", "iPhone 13 mini 128 ГБ", "low"),
    ("iPhone 17 Pro 256GB kosmicky oranžová - nový, nerozbalený", "iPhone 17 Pro 256 ГБ", "high"),
    ("iPhone 18 Pro Max 512GB černý nový nerozbalený HK",
     "iPhone 18 Pro Max 512 ГБ, новый, запечатанный", "high"),
    ("Zánovní Apple iPhone 16 Pro 128GB Natural titan", "iPhone 16 Pro 128 ГБ", "high"),
    ("iPhone 15 Pro 512", "iPhone 15 Pro 256 ГБ", "low"),  # bigger than any variant -> closest below
    ("IPhone 11 a 64 gb", None, None),
    ("iPhone 15 128GB černý", "iPhone 15 128 ГБ", "high"),
    ("iPhone 12 Bílý 128GB Pěkný stav", None, None),
    ("IPhone 12 64 GB", None, None),
    ("Iphone 14 pro", "iPhone 14 Pro 128 ГБ", "low"),
    ("Apple iPhone 16 Pro 128 GB Titanium – TOP STAV, kompletní ba", "iPhone 16 Pro 128 ГБ", "high"),
    ("iPhone 16 Pro (eSIM) - 128GB - 93% - 12M ZÁRUKA", "iPhone 16 Pro 128 ГБ", "high"),
    ("MSI GeForce RTX 4060", "RTX 4060 8 GB", "low"),
    ("ASRock Radeon RX 9060 XT Challenger 16GB OC – Zcela nová, ne", "RX 9060 XT 16 GB", "high"),
    ("XFX Radeon RX 590 FATBOY 8GB", None, None),
    ("Rtx 3070 8gb /prodáno/", "RTX 3070 8 GB", "high"),
    ("EVGA GeForce RTX 3060 XC Gaming 12GB + EVGA PowerLink", "RTX 3060 12 GB", "high"),
    ("Prodám RTX 5070 gigabyte windforce oc sff 12gb", None, None),
    ("EVGA 3080 Ti 12 GB Komplet", "RTX 3080 Ti 12 GB", "high"),
    ("Nvidia RTX 5060 nvidia Gainward Ghost", "RTX 5060 8 GB", "low"),
    ("ASUS ROG Strix RTX 3080 10GB – kompletní balení", "RTX 3080 10 GB", "high"),
    ("Inno3D RTX 4070 SUPER X3 OC", "RTX 4070 Super 12 GB", "low"),
    ("GAINWARD GeForce GTX 1660 Super 6G GHOST OC", None, None),
    ("Rx 5500 xt", None, None),
    ("XFX 308 AMD RX6600XT 8 GB", "RX 6600 / 6600 XT 8 GB", "high"),
    ("Grafická karta GeForce 1050ti", None, None),
    ("Externí grafická karta eGPU GPD G1 V2", None, None),
    ("GIGABYTE VGA AMD Radeon RX 7600 GAMING OC 8G, 8G GDDR6, 2xDP", None, None),
    ("Bitaxe Gamma 602", None, None),
    ("PRODÁNO", None, None),
]

# Typical titles for categories missing from the fixtures (constructed, not scraped).
TYPICAL_TITLES = [
    ("Apple Watch Series 9 45mm", "Apple Watch Series 9 41 / 45 мм", "high"),
    ("Apple Watch SE 2. generace 44mm", "Apple Watch SE 2 40 / 44 мм", "high"),
    ("Apple watch SE 40mm", "Apple Watch SE (1 gen) 40 / 44 мм", "low"),
    ("Apple Watch Series 8 45mm nerez ocel", "Apple Watch Series 8/9 нерж. сталь", "high"),
    ("Apple Watch Ultra 2 49mm titan", "Apple Watch Ultra 2 49 мм", "high"),
    ("Apple Watch Series 10 42mm", "Apple Watch Series 10 42 мм", "high"),
    ("AirPods Pro 2 USB-C", "AirPods Pro 2 Lightning / USB-C", "high"),
    ("Airpods pro", "AirPods Pro 1 2019–2021", "low"),
    ("AirPods 4 s ANC", "AirPods 4 с ANC", "high"),
    ("AirPods 4 bez ANC", "AirPods 4 без ANC", "high"),
    ("AirPods Max USB-C 2024", "AirPods Max USB-C, 2024", "high"),
    ("Levé sluchátko AirPods Pro 2", "Одно ухо / кейс отдельно замена потерянного", "high"),
    ("MacBook Air M1 8GB 256GB", "MacBook Air 13″ M1 8 / 256", "high"),
    ("Apple MacBook Air 15\" M3 8/256 GB", "MacBook Air 15″ M3 8 / 256", "high"),
    ("MacBook Pro 14 M1 Pro 16GB 512GB", "MacBook Pro 14″ M1 Pro 16 / 512", "high"),
    ("MacBook Pro 13 M1 2020 8GB/256GB", "MacBook Pro 13″ M1 8–16 / 256–512", "high"),
    ("Notebook Apple MacBook Air M1 16GB RAM 512GB SSD", "MacBook Air 13″ M1 16 / 256 (CTO)", "low"),
    ("MacBook Air M2", "MacBook Air 13″ M2 8 / 256", "low"),
    ("Macbook Pro 2019 i5 13\"", None, None),
    ("PS5 Slim Digital 1TB", "PlayStation 5 Slim Digital 825 ГБ – 1 ТБ", "high"),
    ("PlayStation 5 s mechanikou CFI-1216A", "PlayStation 5 (с приводом) 825 ГБ, CFI-1xxx", "high"),
    ("Playstation 4 Pro 1TB + 2 ovladače", "PlayStation 4 Pro 1 ТБ", "high"),
    ("PS4 500GB", "PlayStation 4 Slim 500 ГБ / 1 ТБ", "low"),
    ("Ovladač DualSense PS5 bílý", "DualSense (контроллер) новый / б/у", "high"),
    ("Xbox Series S 512GB bílý", "Xbox Series S 512 ГБ", "high"),
    ("Xbox One S 1TB + Kinect", "Xbox One S 1 ТБ", "high"),
    ("Nintendo Switch OLED bílý", "Nintendo Switch OLED 64 GB", "high"),
    ("Nintendo Switch 2 + Mario Kart World", "Nintendo Switch 2 256 GB", "high"),
    ("Nintendo Switch + 2 hry", "Nintendo Switch V1 (2017) 32 GB", "low"),
    ("iPad 9. generace 64GB WiFi", "iPad 9. gen 64 GB", "high"),
    ("Apple iPad Air 5 M1 64GB", "iPad Air M1 (5. gen) 64 GB", "high"),
    ("iPad Pro 11 M1 128GB", 'iPad Pro 11" M1/M2 128 GB', "high"),
    ("Herní PC RTX 3060, Ryzen 5 5600", None, None),
]


@pytest.mark.parametrize("title, label, confidence", REAL_TITLES + TYPICAL_TITLES)
def test_match(prices, title, label, confidence):
    m = prices.match(title)
    assert (m.item.label if m else None) == label
    assert (m.confidence if m else None) == confidence


def test_missing_storage_takes_cheapest_variant(prices):
    m = prices.match("iPhone 15 Pro")
    assert m.item.variant == "128 ГБ" and m.confidence == "low"
    assert any("storage" in n for n in m.notes)


def test_bigger_variant_matches_closest_lower(prices):
    m = prices.match("iPhone 17 Pro Max 1TB")
    assert m.item.variant == "512 ГБ" and m.confidence == "low"


def test_controller_vs_console():
    assert identify(normalize("Ovladač Xbox Series X")).model == "xbox controller"
    assert identify(normalize("Xbox Series X + ovladač")).model == "xbox series x"


# --- stop words / accessories ----------------------------------------------------------------


@pytest.mark.parametrize(
    "title, reasons",
    [
        ("Koupím iPhone 15 Pro", ["koupím"]),
        ("iPhone 12 výměna za Android", ["výměna"]),
        ("iPhone 13 na díly", ["na díly"]),
        ("iPhone 14 blokovaný na iCloud", ["blokovaný", "iCloud"]),
        ("Rtx 3070 8gb /prodáno/", ["prodáno"]),
        ("iPhone 13 iCloud odhlášen 128GB", []),
        ("iPhone 13 128GB výměna baterie", []),
        ("iPhone 14 neblokovaný, plně funkční", []),
    ],
)
def test_stop_reasons(title, reasons):
    assert stop_reasons(normalize(title)) == reasons


@pytest.mark.parametrize(
    "title, accessory",
    [
        ("Kryt na iPhone 15 Pro", True),
        ("Tvrzené sklo iPhone 16", True),
        ("Řemínek na Apple Watch 45mm", True),
        ("Pouzdro na AirPods Pro", True),
        ("Hry na PS5", True),
        ("iPhone 15 Pro 128GB + kryt a sklo", False),
        ("iPhone 14 128GB, obal na telefon zdarma", False),
        ("Nabíjecí pouzdro AirPods Pro 2", False),
    ],
)
def test_accessory(title, accessory):
    t = normalize(title)
    assert is_accessory(t, identify(t)) is accessory


# --- metrics, candidates, scam flags ---------------------------------------------------------


def test_metrics_and_candidate(prices):
    ev = evaluate(make("iPhone 15 128GB černý", 7990, "Hradec Králové"), prices, CFG, TODAY)
    assert ev.market_price == 10000
    assert ev.target_buy == 7200  # 72 %
    assert ev.expected_profit == 2010
    assert ev.discount_pct == pytest.approx(0.201)
    assert ev.is_candidate and 0 < ev.score <= 100


def test_low_liquidity_uses_68_pct(prices):
    ev = evaluate(make("iPhone 15 Plus 128GB", 8000), prices, CFG, TODAY)
    assert ev.target_buy == 6800


def test_not_candidate_above_90_pct(prices):
    ev = evaluate(make("iPhone 15 128GB", 9500), prices, CFG, TODAY)
    assert not ev.is_candidate and ev.score == 0


def test_too_cheap(prices):
    ev = evaluate(make("iPhone 15 128GB", 3500), prices, CFG, TODAY)
    assert ev.too_cheap and not ev.is_candidate


def test_stop_word_blocks_candidate(prices):
    ev = evaluate(make("iPhone 15 128GB na díly", 6000), prices, CFG, TODAY)
    assert ev.match and not ev.is_candidate and ev.stop == ["na díly"]


def test_accessory_blocks_candidate(prices):
    ev = evaluate(make("Kryt na iPhone 15 Pro", 9000), prices, CFG, TODAY)
    assert "accessory" in ev.flags and not ev.is_candidate


def test_scam_jesenik_iphone_14_pro(prices):
    ev = evaluate(make("iPhone 14 Pro 128GB vesmírně černá (eSIM)", 7500, "Jeseník"), prices, CFG, TODAY)
    assert ev.scam_suspect and not ev.is_candidate
    same_elsewhere = evaluate(make("iPhone 14 Pro 128GB", 7500, "Svitavy"), prices, CFG, TODAY)
    assert not same_elsewhere.scam_suspect and same_elsewhere.is_candidate


def test_scam_fb_pro_max_price_band(prices):
    ev = evaluate(make("iPhone 16 Pro Max 256GB", 8590, source="fb"), prices, CFG, TODAY)
    assert ev.scam_suspect


def test_scam_email_phrase(prices):
    listing = make("Xbox Series X 1TB", 12000, description="Pošlete zprávu na e-mail, odpovím.")
    ev = evaluate(listing, prices, CFG, TODAY)
    assert ev.scam_suspect and not ev.is_candidate


def test_scam_weak_phrase_needs_low_price(prices):
    cheap = evaluate(make("iPhone 16 128GB", 7000, description="Platba předem."), prices, CFG, TODAY)
    normal = evaluate(make("iPhone 16 128GB", 10500, description="Platba předem."), prices, CFG, TODAY)
    assert cheap.scam_suspect and not normal.scam_suspect


def test_identical_listings_batch(prices):
    batch = [make("iPhone 15 Pro 128GB", 9900, "Jablonec nad Nisou", id=str(i)) for i in range(3)]
    assert all(e.scam_suspect for e in evaluate_many(batch, prices, CFG, TODAY))


def test_shop_flag(prices):
    ev = evaluate(make("iPhone 16 Pro (eSIM) - 128GB - 93% - 12M ZÁRUKA", 14000), prices, CFG, TODAY)
    assert "shop" in ev.flags


# --- score / ranking -------------------------------------------------------------------------


def test_score_prefers_bigger_discount_closer_and_fresher(prices):
    base = dict(title="iPhone 15 Pro 128GB", location="Pardubice")
    good = make(price=9500, id="a", **base)
    weak = make(price=11500, id="b", **base)
    assert evaluate(good, prices, CFG, TODAY).score > evaluate(weak, prices, CFG, TODAY).score

    home = make(price=10500, id="c", title=base["title"], location="Ústí nad Orlicí")
    far = make(price=10500, id="d", title=base["title"], location="Olomouc")
    assert evaluate(home, prices, CFG, TODAY).score > evaluate(far, prices, CFG, TODAY).score

    old = make(price=10500, id="e", posted_date=date(2026, 9, 1), **base)
    new = make(price=10500, id="f", **base)
    assert evaluate(new, prices, CFG, TODAY).score > evaluate(old, prices, CFG, TODAY).score


def test_low_confidence_scores_lower(prices):
    exact = evaluate(make("iPhone 15 Pro 128GB", 10000), prices, CFG, TODAY)
    vague = evaluate(make("iPhone 15 Pro", 10000), prices, CFG, TODAY)
    assert exact.score > vague.score


def test_rank_only_candidates_sorted(prices):
    evs = evaluate_many(
        [
            make("iPhone 15 128GB", 7500, id="1"),
            make("iPhone 15 128GB", 9800, id="2"),  # > 90 %
            make("iPhone 16 Pro 128GB", 12500, "Ústí nad Orlicí", id="3"),
        ],
        prices, CFG, TODAY,
    )
    ranked = rank(evs)
    assert [e.listing.id for e in ranked] == sorted(
        [e.listing.id for e in ranked], key=lambda i: -next(e.score for e in ranked if e.listing.id == i)
    )
    assert {e.listing.id for e in ranked} == {"1", "3"}


# --- accessories by brand / word, stale, min profit, discount cap, bundles ----------------------


@pytest.mark.parametrize(
    "title, accessory",
    [
        ("HORI Racing Wheel APEX PS4", True),
        ("Herní set volantu a pedálů HORI RWA pro PS4/PC – jako nový", True),
        ("Logitech G29 volant PS5", True),
        ("Thrustmaster T150 PS4", True),
        ("PS5 headset Pulse 3D", True),
        ("Sluchátka k iPhone 15", True),
        ("Nabíječka iPhone 13", True),
        ("PlayStation Camera PS4", True),
        ("Dock Nintendo Switch", True),
        ("FIFA 24 hra PS5", True),
        ("Ovladač PS4 DualShock", True),
        ("Gamepad Xbox One", False),  # priced item "Геймпад Xbox"
        ("DualSense ovladač PS5 bílý", False),  # priced item
        ("AirPods Pro 2 sluchátka", False),
        ("iPhone 13 128GB + nabíječka", False),
        ("PS5 Slim + 2 hry", False),
        ("Nintendo Switch OLED s dockem", False),
        ("Konzole Xbox Series S + ovladač", False),
    ],
)
def test_accessory_words_and_brands(title, accessory):
    t = normalize(title)
    assert is_accessory(t, identify(t)) is accessory


def test_hori_wheel_is_not_a_candidate(prices):
    ev = evaluate(make("HORI Racing Wheel APEX PS4", 1550), prices, CFG, TODAY)
    assert "accessory" in ev.flags and not ev.is_candidate


def test_stale_listing_flag_and_penalty(prices):
    fresh = evaluate(make("iPhone 15 128GB", 7500), prices, CFG, TODAY)
    stale = evaluate(make("iPhone 15 128GB", 7500, posted_date=date(2026, 9, 30)), prices, CFG, TODAY)
    edge = evaluate(make("iPhone 15 128GB", 7500, posted_date=date(2026, 10, 1)), prices, CFG, TODAY)
    assert "stale" in stale.flags and stale.is_candidate  # still a candidate, just ranked lower
    assert "stale" not in edge.flags  # exactly 14 days is not stale
    assert fresh.score - stale.score >= 10


def test_min_profit_is_required(prices):
    # Switch Lite: market 2 700; 2 400 is < 90 % of market, but the profit of 300 Kč is not worth a trip.
    ev = evaluate(make("Nintendo Switch Lite", 2400), prices, CFG, TODAY)
    assert "low_profit" in ev.flags and not ev.is_candidate
    ok = evaluate(make("Nintendo Switch Lite", 1600), prices, CFG, TODAY)
    assert "low_profit" not in ok.flags and ok.is_candidate


def test_discount_above_cap_scores_the_same(prices):
    market = 10000  # iPhone 15 128GB
    at_cap = evaluate(make("iPhone 15 128GB", int(market * 0.55)), prices, CFG, TODAY)
    beyond = evaluate(make("iPhone 15 128GB", int(market * 0.45)), prices, CFG, TODAY)
    assert at_cap.is_candidate and beyond.is_candidate
    # Bigger profit still counts, but the 45-point discount part is capped.
    assert beyond.score - at_cap.score <= 20 * (market * 0.10) / (1000 * 4) + 1


def test_bundle_extra_controllers_count_half(prices):
    single = evaluate(make("PS5 Slim 1TB", 8000), prices, CFG, TODAY)
    double = evaluate(make("PS5 Slim 1TB + 2 ovladače", 8000), prices, CFG, TODAY)
    dualsense = prices.match("DualSense").item.market_price
    assert double.market_price - single.market_price == round(dualsense * 0.5, -2)
    assert any("комплект" in n for n in double.match.notes)
    with_one = evaluate(make("PS5 Slim 1TB s ovladačem", 8000), prices, CFG, TODAY)
    assert with_one.market_price == single.market_price


@pytest.mark.parametrize(
    "title, description, excluded",
    [
        ("iPhone 15 128GB", "Prodám nebo možná výměna.", False),
        ("Apple Watch Ultra 3 (2025) 49m", "Důvodem prodeje je výměna za nový model.", False),
        ("iPhone 15 128GB prodám nebo vyměním", None, False),
        ("iPhone 15 128GB výměna za Android", None, True),
        ("iPhone 15 128GB vyměním", None, True),
    ],
)
def test_exchange_excludes_only_swap_titles(prices, title, description, excluded):
    ev = evaluate(make(title, 7500, description=description), prices, CFG, TODAY)
    assert ("výměna" in ev.stop) is excluded
    assert ev.is_candidate is (not excluded)
    if not excluded:
        assert "возможен обмен" in ev.flags
