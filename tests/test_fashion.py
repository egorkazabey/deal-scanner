"""Clothes and sneakers (Vinted): fashion.json match, condition and size, fashion ceiling, replicas."""

from pathlib import Path

import pytest

from scanner.config import PricingConfig, VintedConfig
from scanner.fashion import shoe_size
from scanner.pricing import PriceList, evaluate, max_buy_price
from scanner.sources.vinted import parse_search_page
from test_pipeline import TODAY, listing

CFG = PricingConfig()
PRICES = PriceList.load(CFG.prices_path)


def fashion(title, price=500, brand=None, size="42", condition="Velmi dobrý", **kw):
    return listing(source="vinted", title=title, price=price, brand=brand, size=size, condition=condition,
                   location=None, description=None, **kw)


def market(label: str) -> int:
    return next(i.market_price for i in PRICES.fashion.items if i.label == label)


@pytest.mark.parametrize("title, brand, label", [
    ("Nike Dunk Low Panda", "Nike", "Nike Dunk Low"),
    ("Nike SB Dunk Low Staple Pigeon", "Nike", "Nike SB Dunk Low"),
    ("Buty Air Jordan 1 Mid Chicago", "Jordan", "Air Jordan 1 Mid"),
    ("Jordan 1 Retro High OG", "Nike", "Air Jordan 1 High"),
    ("Jordan 4 Military Black", None, "Air Jordan 4"),
    ("NB 2002R Protection Pack", "New Balance", "New Balance 2002R"),
    ("Sneakersy 550 białe", "New Balance", "New Balance 550"),
    ("Salomon XT-6 black", "Salomon", "Salomon XT-6"),
    ("Kurtka puchowa Nuptse 700", "The North Face", "The North Face Nuptse"),
    ("Stone Island bluza crewneck", "Stone Island", "Stone Island mikina"),
    ("Arc'teryx Beta LT jacket", "Arc'teryx", "Arc'teryx bunda"),
])
def test_match(title, brand, label):
    ev = evaluate(fashion(title, brand=brand), PRICES, CFG, TODAY)
    assert ev.match and ev.match.item.model == label and ev.match.item.category == "fashion"


@pytest.mark.parametrize("title, brand", [
    ("Nike Air Force 1", "Nike"),        # not in the list
    ("Stone Island čepice", "Stone Island"),  # a cap: no garment word we price
    ("550 Kč tričko", "H&M"),            # model word without the brand
    ("Dámské zimní boty The North Face Nuptse", "The North Face"),  # boots, not the jacket
    ("Carhartt short Detroit", "Carhartt"),  # shorts, not the jacket
    ("Stone Island dětský svetr", "Stone Island"),  # kids
])
def test_no_match(title, brand):
    assert evaluate(fashion(title, brand=brand), PRICES, CFG, TODAY).match is None


def test_condition_and_size_adjust_market():
    base = market("Nike Dunk Low")
    ev = evaluate(fashion("Nike Dunk Low", brand="Nike", condition="Nový s visačkou"), PRICES, CFG, TODAY)
    assert ev.market_price == round(base * 1.25, -1) and ev.match.confidence == "high"
    ev = evaluate(fashion("Nike Dunk Low", brand="Nike", condition="Dobrý", size="38"), PRICES, CFG, TODAY)
    assert ev.market_price == round(base * 0.8, -1)
    assert ev.match.item.liquidity == 2  # 38 is outside 40-46
    ev = evaluate(fashion("Nike Dunk Low", brand="Nike", condition=None), PRICES, CFG, TODAY)
    assert ev.match.confidence == "low"
    assert evaluate(fashion("Nike Dunk Low GS", brand="Nike", size="35,5"), PRICES, CFG, TODAY).match is None


def test_kids_sizes_do_not_match():
    assert evaluate(fashion("Stone Island svetr", brand="Stone Island", size="122–127 cm / 7 let"),
                    PRICES, CFG, TODAY).match is None


def test_clothing_sizes():
    ev = evaluate(fashion("Stone Island mikina", brand="Stone Island", size="XXL"), PRICES, CFG, TODAY)
    assert ev.match.item.liquidity == 1
    ev = evaluate(fashion("Stone Island mikina", brand="Stone Island", size="M / 38"), PRICES, CFG, TODAY)
    assert ev.match.item.liquidity == 2


def test_shoe_size():
    assert [shoe_size(s) for s in ("42", "42,5", "EU 44 / UK 9", "M", "")] == [42, 42.5, 44, None, None]


def test_fashion_ceiling_and_candidate():
    m = market("Stone Island bunda")
    assert max_buy_price(m, "fashion", 2, CFG) == min(m * 65 // 100, m - 400) // 100 * 100
    cheap = evaluate(fashion("Stone Island bunda", brand="Stone Island", price=m * 55 // 100, extra_cost=120),
                     PRICES, CFG, TODAY)
    assert cheap.is_candidate and cheap.max_price == (max_buy_price(m, "fashion", 2, CFG) - 120) // 100 * 100
    dear = evaluate(fashion("Stone Island bunda", brand="Stone Island", price=m * 95 // 100), PRICES, CFG, TODAY)
    assert not dear.is_candidate


def test_replica_is_stopped():
    ev = evaluate(fashion("Jordan 4 replika 1:1", brand="Nike", price=900), PRICES, CFG, TODAY)
    assert "replika" in ev.stop and not ev.is_candidate


def test_real_vinted_sneaker_page():
    listings, _ = parse_search_page((Path(__file__).parent / "fixtures" / "vinted_search_fashion.html")
                                    .read_text(encoding="utf-8"), VintedConfig())
    labels = [ev.match.item.model for l in listings if (ev := evaluate(l, PRICES, CFG, TODAY)).match]
    assert labels.count("Nike Dunk Low") >= 6 and "Nike SB Dunk Low" in labels
