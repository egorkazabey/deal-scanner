"""Rule-based pricing: title normalization, price list matching, flags, metrics and score."""

from datetime import date

import pytest

from scanner.config import PROJECT_ROOT, PricingConfig, load_config
from scanner.models import Listing
from scanner.pricing import (
    PriceList,
    evaluate,
    evaluate_many,
    is_accessory,
    identify,
    max_buy_price,
    max_price_rule_lines,
    normalize,
    offer_price,
    rank,
    stop_reasons,
)

TODAY = date(2026, 10, 15)
CFG = PricingConfig(home_cities=["Ústí nad Orlicí", "Choceň"], near_cities=["Pardubice", "Svitavy"])


@pytest.fixture(scope="module")
def prices() -> PriceList:
    return PriceList.load(PROJECT_ROOT / "knowledge" / "prices.json")


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
    ("GAINWARD GeForce GTX 1660 Super 6G GHOST OC", "GTX 1660 Super", "high"),
    ("MSI GeForce GTX 1650 4GB", "GTX 1650", "high"),
    ("Sapphire Nitro+ RX 7900 XTX 24GB", "RX 7900 XTX", "high"),
    ("Palit RTX 4070 Ti Super 16GB", "RTX 4070 Ti Super", "high"),
    ("RTX 4060 Ti 16GB Asus Dual", "RTX 4060 Ti 16 GB", "high"),
    ("Gigabyte RTX 2060 12GB", None, None),  # only the 6 GB version is priced
    ("Rx 5500 xt", None, None),
    ("XFX 308 AMD RX6600XT 8 GB", "RX 6600 / 6600 XT 8 GB", "high"),
    ("Grafická karta GeForce 1050ti", None, None),
    ("Externí grafická karta eGPU GPD G1 V2", None, None),
    ("GIGABYTE VGA AMD Radeon RX 7600 GAMING OC 8G, 8G GDDR6, 2xDP", "RX 7600", "high"),
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
    assert ev.max_price == 7800  # min(10 000 × 0.78, 10 000 − 1 200)
    assert ev.offer_price == 7300  # min(7 990 × 0.92 = 7 350, 7 800 × 0.95 = 7 410) -> down to 100
    assert ev.expected_profit == 2010
    assert ev.discount_pct == pytest.approx(0.201)
    assert ev.is_candidate and 0 < ev.score <= 100


def test_low_liquidity_drops_pct(prices):
    item = prices.match("iPhone 15 Plus 128GB").item
    assert item.liquidity == 1
    ev = evaluate(make("iPhone 15 Plus 128GB", 8000), prices, CFG, TODAY)
    assert ev.max_price == max_buy_price(item.market_price, "iphone", 1, CFG)
    assert ev.max_price == min(item.market_price * 74 // 100, item.market_price - 1200) // 100 * 100


# --- tiered buy ceiling (config.yaml pricing.max_price_tiers) and first offer ------------------


@pytest.mark.parametrize(
    "market, category, liquidity, expected",
    [
        # market < 5 000: min(market × 0.70, market − 1 000)
        (1500, "airpods", 2, 500),      # 1 050 vs 500: the margin wins
        (3333, "airpods", 3, 2300),     # 2 333 vs 2 333 -> 2 300
        (4000, "apple_watch", 2, 2800),
        (4999, "iphone", 3, 3400),      # upper bound of the band: 3 499 vs 3 999
        (4000, "airpods", 1, 2600),     # liquidity 1: 0.66 -> 2 640
        (4000, "gpu", 2, 2000),         # GPU: margin 2 000 in every band
        (800, "airpods", 2, 0),         # never negative
        # 5 000 – 12 000: min(market × 0.78, market − 1 200)
        (5000, "iphone", 2, 3800),      # lower bound: 3 900 vs 3 800
        (5400, "iphone", 2, 4200),      # 4 212 vs 4 200
        (10000, "iphone", 2, 7800),
        (12000, "playstation", 2, 9300),  # upper bound: 9 360
        (10000, "iphone", 1, 7400),     # liquidity 1: 0.74
        (6000, "macbook", 2, 4000),     # MacBook: 4 680 vs 6 000 − 2 000
        (9000, "pc", 1, 6600),          # PC, liquidity 1: 6 660 vs 7 000
        # > 12 000: min(market × 0.85, market − 2 000)
        (12001, "iphone", 2, 10000),    # 10 200 vs 10 001
        (13800, "apple_watch", 2, 11700),  # Ultra 3: 11 730 vs 11 800
        (20000, "iphone", 2, 17000),
        (20000, "iphone", 1, 16200),    # liquidity 1: 0.81
        (30000, "macbook", 2, 25500),
    ],
)
def test_max_buy_price_tiers(market, category, liquidity, expected):
    assert max_buy_price(market, category, liquidity, CFG) == expected


@pytest.mark.parametrize(
    "asking, max_price, expected",
    [
        (11999, 11700, 11000),  # 11 039 vs 11 115
        (7000, 8500, 6400),     # 6 440 vs 8 075: 92 % of the asking price
        (7000, 5800, 5500),     # 6 440 vs 5 510: max − 5 %
        (2500, 2300, 2100),     # 2 300 vs 2 185
        (999, 0, 0),
    ],
)
def test_offer_price(asking, max_price, expected):
    assert offer_price(asking, max_price, CFG) == expected


def test_ultra_3_offer_and_max(prices):
    ev = evaluate(make("Apple Watch Ultra 3 49mm", 11999), prices, CFG, TODAY)
    assert ev.match.item.model == "Apple Watch Ultra 3" and ev.market_price == 13800
    assert (ev.max_price, ev.offer_price) == (11700, 11000)


def test_config_yaml_tiers_and_knowledge_agree():
    cfg = load_config(PROJECT_ROOT / "config.yaml").pricing
    assert cfg.max_price_tiers == CFG.max_price_tiers  # the defaults are the configured rule
    knowledge = (PROJECT_ROOT / "knowledge" / "KNOWLEDGE.md").read_text(encoding="utf-8")
    for line in max_price_rule_lines(cfg):
        assert line in knowledge


def test_tiers_must_be_ordered():
    with pytest.raises(ValueError):
        PricingConfig(max_price_tiers=[{"up_to": 12000, "pct": 78, "min_margin": 1200},
                                       {"up_to": 4999, "pct": 70, "min_margin": 1000},
                                       {"pct": 85, "min_margin": 2000}])
    with pytest.raises(ValueError):
        PricingConfig(max_price_tiers=[{"up_to": 4999, "pct": 70, "min_margin": 1000}])


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
