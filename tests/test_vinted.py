"""Vinted.cz: URL builder, flight-data search parser, detail / sold, blocking, cost on top (real pages, trimmed)."""

import asyncio
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from scanner.config import AppConfig, BazosSearch, PricingConfig, VintedConfig
from scanner.db import Database
from scanner.gemini import Review
from scanner.notify import fallback_message, format_alert
from scanner.pipeline import run_scan
from scanner.pricing import PriceList, evaluate
from scanner.sources.vinted import (
    VintedBlocked, VintedClient, VintedError, is_sold, parse_detail, parse_item, parse_search_page, search_url,
)
from test_pipeline import SETTINGS, START, TODAY, FakeBazos, listing, no_ai

FIXTURES = Path(__file__).parent / "fixtures"
CFG = VintedConfig(catalogs={"iphone": [3661]}, queries={"graficka": "radeon"})
PRICING = PricingConfig()
PRICES = PriceList.load(PRICING.prices_path)


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_search_url():
    s = BazosSearch(name="iphone", section="mobil", query="iphone", price_min=2000, price_max=45000)
    assert search_url(s, CFG) == ("https://www.vinted.cz/catalog?search_text=iphone&catalog%5B%5D=3661"
                                  "&price_from=2000&price_to=45000&currency=CZK&order=newest_first")
    assert search_url(s, CFG, page=1).endswith("&order=newest_first&page=2")
    cat = BazosSearch(name="graficka", section="pc", category="graficka")
    assert search_url(cat, CFG) == "https://www.vinted.cz/catalog?search_text=radeon&currency=CZK&order=newest_first"


def test_parse_search_page_phones():
    listings, has_next = parse_search_page(_page("vinted_search.html"), CFG)
    assert has_next and len(listings) == 12
    first = listings[0]
    assert (first.source, first.id, first.title, first.price, first.foreign) == (
        "vinted", "10573387944", "Apple IPhone 16 pro 512gb pustynny tytan", 2964, True)
    assert first.url == "https://www.vinted.cz/items/10573387944-apple-iphone-16-pro-512gb-pustynny-tytan"
    assert first.extra_cost == 166 + CFG.shipping_abroad_czk  # Vinted's fee + shipping from abroad
    assert (first.brand, first.condition, first.size, first.posted_date, first.location) == (
        "Apple", "Velmi dobrý", None, None, None)

    czech = next(l for l in listings if l.id == "10567709830")
    assert (czech.title, czech.price, czech.foreign, czech.extra_cost) == (
        "Apple iPhone 16", 13500, False, 693 + CFG.shipping_czk)  # 5 % + 18 Kč
    case = next(l for l in listings if l.id == "10572563548")
    assert (case.title, case.brand, case.condition) == ("Iphone 15 pro kryt", "Prada", "Nový bez visačky")
    assert len({l.id for l in listings}) == len(listings)


def test_parse_search_page_fashion_has_size_and_condition():
    listings, _ = parse_search_page(_page("vinted_search_fashion.html"), CFG)
    first = listings[0]
    assert (first.title, first.brand, first.size, first.condition) == (
        "Nike Dunk Low czerwono-srebrne", "Nike", "40", "Velmi dobrý")


def test_czech_sellers_only():
    listings, _ = parse_search_page(_page("vinted_search.html"), VintedConfig(foreign=False))
    assert {l.id for l in listings} == {"10567709830", "10567479167", "10572563548", "10572102680"}
    assert not any(l.foreign for l in listings)


def test_parse_item_without_fee_uses_vinted_formula():
    item = {"id": 5, "productItem": {"id": 5, "title": "PS5", "url": "/items/5-ps5",
                                     "price": {"amount": "8000.00", "currencyCode": "CZK"}}}
    assert parse_item(item, CFG).extra_cost == 8000 * 5 // 100 + 18 + CFG.shipping_czk
    item["productItem"]["price"]["currencyCode"] = "EUR"
    assert parse_item(item, CFG) is None


def test_search_page_without_items():
    with pytest.raises(VintedBlocked):
        parse_search_page('<html><script src="https://ct.captcha-delivery.com/c.js"></script></html>', CFG)
    with pytest.raises(VintedError):
        parse_search_page("<html><body>nothing</body></html>", CFG)


def test_parse_detail_and_sold():
    page = _page("vinted_detail.html")
    d = parse_detail(page, "https://www.vinted.cz/items/10567709830-apple-iphone-16")
    assert (d.title, d.price, d.description) == ("Apple iPhone 16", 13500, "Prodám")
    assert not is_sold(page)
    assert is_sold(page.replace('"availability":"InStock"', '"availability":"SoldOut"'))


def _client(handler) -> VintedClient:
    client = VintedClient(VintedConfig(delay_min=0, delay_max=0, retries=0), [])
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def test_client_get_sold_is_410_and_antibot_is_blocked():
    sold = _page("vinted_detail.html").replace('"availability":"InStock"', '"availability":"SoldOut"')

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/items/1-sold":
            return httpx.Response(200, text=sold)
        if path == "/items/2-forbidden":
            return httpx.Response(403, text="blocked")
        if path == "/items/3-captcha":
            return httpx.Response(302, headers={"location": "https://geo.captcha-delivery.com/captcha/"})
        if path == "/items/4-gone":
            return httpx.Response(404, text="")
        return httpx.Response(200, text="<html></html>")  # home page: session cookies

    async def run():
        async with _client(handler) as client:
            with pytest.raises(VintedError) as sold_exc:
                await client.get("https://www.vinted.cz/items/1-sold")
            with pytest.raises(VintedBlocked):
                await client.get("https://www.vinted.cz/items/2-forbidden")
            with pytest.raises(VintedBlocked):
                await client.get("https://www.vinted.cz/items/3-captcha")
            with pytest.raises(VintedError) as gone_exc:
                await client.get("https://www.vinted.cz/items/4-gone")
            return sold_exc.value.status, gone_exc.value.status

    assert asyncio.run(run()) == (410, 404)


def test_extra_cost_lowers_ceiling_and_profit():
    plain = evaluate(listing(title="iPhone 15 128GB", price=7000), PRICES, PRICING, TODAY)
    vinted = evaluate(listing(source="vinted", title="iPhone 15 128GB", price=7000, extra_cost=500),
                      PRICES, PRICING, TODAY)
    assert vinted.market_price == plain.market_price
    assert vinted.expected_profit == plain.expected_profit - 500
    assert vinted.max_price == (plain.max_price - 500) // 100 * 100
    assert vinted.discount_pct < plain.discount_pct


def test_extra_cost_can_make_it_no_candidate():
    market = evaluate(listing(title="iPhone 15 128GB", price=7000), PRICES, PRICING, TODAY).market_price
    price = int(market * 0.88)  # a candidate at 88 % of the market ...
    assert evaluate(listing(title="iPhone 15 128GB", price=price), PRICES, PRICING, TODAY).is_candidate
    ev = evaluate(listing(source="vinted", title="iPhone 15 128GB", price=price, extra_cost=400), PRICES, PRICING, TODAY)
    assert not ev.is_candidate  # ... but not with fee and shipping on top (> 90 %)


def test_foreign_seller_too_cheap_below_55_pct():
    market = evaluate(listing(title="iPhone 15 128GB", price=7000), PRICES, PRICING, TODAY).market_price
    price = int(market * 0.5)
    assert not evaluate(listing(title="iPhone 15 128GB", price=price), PRICES, PRICING, TODAY).too_cheap
    foreign = evaluate(listing(source="vinted", title="iPhone 15 128GB", price=price, foreign=True), PRICES, PRICING, TODAY)
    assert foreign.too_cheap and not foreign.is_candidate


def test_polish_titles():
    ev = evaluate(listing(title="Apple iPhone 13 128GB uszkodzony na części", price=3000), PRICES, PRICING, TODAY)
    assert ev.stop == ["na díly"]
    assert evaluate(listing(title="Apple watch ultra 2 defect", price=4000), PRICES, PRICING, TODAY).stop == ["na díly"]
    ev = evaluate(listing(title="Szkło hartowane do iPhone 15", price=3000), PRICES, PRICING, TODAY)
    assert "accessory" in ev.flags


def test_alert_shows_cost_and_vinted_message():
    l = listing(source="vinted", title="iPhone 15 128GB", price=7000, extra_cost=430, foreign=True, location=None,
                url="https://www.vinted.cz/items/1-iphone-15")
    ev = evaluate(l, PRICES, PRICING, TODAY)
    text, _ = format_alert("deal", Review(ev, no_ai_reason="test"), PRICING, TODAY)
    assert "Vinted, 🌍 из-за рубежа" in text and "📦 7 000 + ochrana и доставка 430 = 7 430 Kč" in text
    assert "osobním" not in fallback_message(l, 6400)
    assert "osobním" in fallback_message(listing(), 6400)


def test_pipeline_keeps_first_seen_day_and_extra_cost():
    class FakeVinted(FakeBazos):
        source = "vinted"

    db = Database(":memory:")
    item = listing(source="vinted", id="77", posted_date=None, extra_cost=250, foreign=True, brand="Apple",
                   condition="Dobrý", url="https://www.vinted.cz/items/77-iphone")
    asyncio.run(run_scan(SETTINGS, AppConfig(), db, FakeVinted([item]), now=lambda: START, dry=True,
                         reviewer_factory=no_ai))
    asyncio.run(run_scan(SETTINGS, AppConfig(), db, FakeVinted([item]), now=lambda: START + timedelta(days=2),
                         dry=True, reviewer_factory=no_ai))
    stored = db.listing("vinted", "77")
    assert stored.posted_date == date(2026, 10, 15)  # the first day, not the day of the second run
    assert (stored.extra_cost, stored.foreign, stored.brand, stored.condition) == (250, True, "Apple", "Dobrý")
