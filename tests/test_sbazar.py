"""Sbazar.cz: URL builder, Astro props decoder, search / detail parsers (real HTML in fixtures)."""

import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest

from scanner.config import AppConfig, BazosSearch, SbazarConfig
from scanner.db import Database, Status
from scanner.pipeline import run_scan
from scanner.sources.sbazar import SbazarError, _decode, parse_detail, parse_item, parse_search_page, search_url
from test_pipeline import SETTINGS, START, FakeBazos, listing, no_ai

FIXTURES = Path(__file__).parent / "fixtures"
CFG = SbazarConfig(queries={"graficka": "graficka karta"})


def test_search_url():
    s = BazosSearch(name="herní pc", section="pc", query="herní pc", price_min=5000, price_max=60000)
    assert search_url(s, CFG) == ("https://www.sbazar.cz/hledej/hern%C3%AD%20pc/0-vsechny-kategorie/"
                                  "usti-nad-orlici/cena-od-5000-do-60000-kc?radius=30")
    assert search_url(s, CFG, page=1).endswith("cena-od-5000-do-60000-kc/nejnovejsi/2?radius=30")
    cat = BazosSearch(name="graficka", section="pc", category="graficka")
    assert search_url(cat, CFG) == ("https://www.sbazar.cz/hledej/graficka%20karta/0-vsechny-kategorie/"
                                    "usti-nad-orlici/cena-neomezena?radius=30")


def test_decode_astro_props():
    assert _decode({"a": [0, 5], "b": [1, [[0, "x"], [0, {"c": [0, None]}]]], "d": [0]}) == \
        {"a": 5, "b": ["x", {"c": None}], "d": [0]}


def test_parse_search_page():
    listings, has_next = parse_search_page((FIXTURES / "sbazar_search.html").read_text(encoding="utf-8"))
    assert has_next and len(listings) == 46  # 58 results minus 12 Zboží.cz shop ads
    first = listings[0]
    assert (first.source, first.id, first.title, first.price, first.location, first.is_top) == (
        "sbazar", "235124844", "Apple iPhone 16 Plus 128 GB v černé barvě", 15000, "Mladá Boleslav", True)
    assert first.url == "https://www.sbazar.cz/inzerat/235124844-apple-iphone-16-plus-128-gb-v-cerne-barve"
    assert first.posted_date == date(2026, 10, 22)
    assert listings[1].location == "Ostrava-město"  # no municipality: the district
    assert len({l.id for l in listings}) == len(listings)


def test_parse_item_skips_shop_ads_and_reads_price_by_agreement():
    base = {"id": 1, "seoName": "1-x", "name": "PS5", "price": 9000, "locality": {"municipality": "Žamberk"}}
    assert parse_item(base).price == 9000
    assert parse_item(base | {"priceByAgreement": True}).price is None
    assert parse_item(base | {"price": 0}).price is None
    assert parse_item(base | {"id": "a1b2", "locality": None}) is None


def test_parse_search_page_without_island():
    with pytest.raises(SbazarError):
        parse_search_page("<html><body>nothing</body></html>")


def test_parse_detail():
    url = "https://www.sbazar.cz/inzerat/235124844-apple-iphone-16-plus-128-gb-v-cerne-barve"
    d = parse_detail((FIXTURES / "sbazar_detail.html").read_text(encoding="utf-8"), url)
    assert d.title == "Apple iPhone 16 Plus 128 GB v černé barvě" and d.price == 15000
    assert d.description.startswith("Prodám Apple iPhone 16 Plus 128 GB") and "&quot;" not in d.description


class FakeSbazar(FakeBazos):
    source = "sbazar"


def test_pipeline_checks_contacted_sbazar_listings_only():
    db = Database(":memory:")
    for src in ("bazos", "sbazar"):
        db.upsert(listing(source=src, id="9", url=f"https://{src}/9"), START - timedelta(days=1))
        db.set_status(src, "9", Status.CONTACTED)
    client = FakeSbazar([], gone={"https://sbazar/9"})
    asyncio.run(run_scan(SETTINGS, AppConfig(), db, client, now=lambda: START, reviewer_factory=no_ai))
    assert client.gets == ["https://sbazar/9"]
    assert [(r["source"], r["kind"]) for r in db.pending_alerts()] == [("sbazar", "gone")]
