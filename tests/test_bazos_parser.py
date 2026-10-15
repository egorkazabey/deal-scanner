"""Parser tests on real Bazoš HTML saved by `python -m scanner save-fixtures`.

Fixture assertions are structural so the fixtures can be refreshed when Bazoš changes its layout;
exact values are checked on small inline snippets copied from real pages.
"""

from datetime import date
from pathlib import Path

import pytest

from scanner.config import BazosConfig, BazosSearch
from scanner.sources.bazos import (
    build_search_url,
    parse_date,
    parse_detail,
    parse_price,
    parse_search_page,
)

FIXTURES = Path(__file__).parent / "fixtures"
QUERY_URL = "https://mobil.bazos.cz/?hledat=iphone&rubriky=mobil&hlokalita=56201&humkreis=60"
CATEGORY_URL = "https://pc.bazos.cz/graficka/?hlokalita=56201&humkreis=60"

CARD_SNIPPET = """
<div class="inzeraty inzeratyflex">
<div class="inzeratynadpis"><a href="/inzerat/225124379/iphone-14-128gb-stav-a-nova-baterie-zaruka.php"><img src="x.jpg" class="obrazek"></a>
<h2 class=nadpis><a href="/inzerat/225124379/iphone-14-128gb-stav-a-nova-baterie-zaruka.php">iPhone 14 128GB - stav A-, nová baterie, záruka</a></h2><span class=velikost10> - <span title="TOP 1x Platí do 22.10. 2026" class="ztop">TOP</span> - [15.10. 2026]</span><br>
<div class=popis>Nabízím k prodeji plně funkční <b>iphone</b> 14 ...</div><br><br>
</div>
<div class="inzeratycena"><b><span translate="no">  6 990 Kč</span></b></div>
<div class="inzeratylok">Svitavy<br>568 02</div>
<div class="inzeratyview">29 x</div>
</div>
<div class="inzeraty inzeratyflex">
<div class="inzeratynadpis">
<h2 class=nadpis><a href="/inzerat/225060776/graficka-karta-msi-gtx-1080-ti.php">PRODÁNO</a></h2><span class=velikost10> - [4.9. 2026]</span><br>
</div>
<div class="inzeratycena"><b><span translate="no">Dohodou</span></b></div>
<div class="inzeratylok">Náchod<br>547 01</div>
</div>
<div class="strankovani">Stránka: <b><span class=cisla>1</span></b> <a href="/20/?hledat=iphone">2</a> <a href="/20/?hledat=iphone"><b>Další</b></a></div>
"""


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  6 990 Kč", 6990),
        ("12 500 Kč", 12500),
        ("1 000 Kč", 1000),
        ("500 Kč", 500),
        ("Dohodou", None),
        ("Nabídněte", None),
        ("V textu", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_parse_date():
    assert parse_date(" - TOP - [15.10. 2026]") == date(2026, 10, 15)
    assert parse_date("[4.9. 2026]") == date(2026, 9, 4)
    assert parse_date("no date") is None


def test_build_search_url_query_and_pagination():
    cfg = BazosConfig()
    s = BazosSearch(name="herní pc", section="pc", query="herní pc", price_min=5000, price_max=60000)
    assert build_search_url(s, cfg) == (
        "https://pc.bazos.cz/?hledat=hern%C3%AD+pc&rubriky=pc&hlokalita=56201&humkreis=60"
        "&cenaod=5000&cenado=60000"
    )
    assert build_search_url(s, cfg, page=1).startswith("https://pc.bazos.cz/20/?hledat=")


def test_build_search_url_category():
    cfg = BazosConfig()
    s = BazosSearch(name="graficka", section="pc", category="graficka", price_min=1800)
    assert build_search_url(s, cfg) == "https://pc.bazos.cz/graficka/?hlokalita=56201&humkreis=60&cenaod=1800"
    assert build_search_url(s, cfg, page=1).startswith("https://pc.bazos.cz/graficka/20/?")


def test_search_requires_query_or_category():
    with pytest.raises(ValueError):
        BazosSearch(name="x", section="pc")
    with pytest.raises(ValueError):
        BazosSearch(name="x", section="pc", query="a", category="b")


def test_parse_card_snippet():
    listings, has_next = parse_search_page(CARD_SNIPPET, QUERY_URL)
    assert has_next is True
    assert len(listings) == 2

    first, second = listings
    assert first.source == "bazos"
    assert first.id == "225124379"
    assert first.url == "https://mobil.bazos.cz/inzerat/225124379/iphone-14-128gb-stav-a-nova-baterie-zaruka.php"
    assert first.title == "iPhone 14 128GB - stav A-, nová baterie, záruka"
    assert first.price == 6990
    assert first.location == "Svitavy"
    assert first.postal_code == "568 02"
    assert first.posted_date == date(2026, 10, 15)
    assert first.is_top is True
    assert first.description is None

    assert second.price is None
    assert second.is_top is False
    assert second.posted_date == date(2026, 9, 4)


@pytest.mark.parametrize(
    ("fixture", "base_url", "host"),
    [
        ("bazos_search_query.html", QUERY_URL, "https://mobil.bazos.cz/inzerat/"),
        ("bazos_search_category.html", CATEGORY_URL, "https://pc.bazos.cz/inzerat/"),
    ],
)
def test_parse_search_fixture(fixture, base_url, host):
    listings, _ = parse_search_page(load(fixture), base_url)

    assert 1 <= len(listings) <= 20
    assert len({l.id for l in listings}) == len(listings)
    for l in listings:
        assert l.id.isdigit()
        assert l.url.startswith(f"{host}{l.id}/") and l.url.endswith(".php"), l.url  # full URL with slug
        assert l.title
        assert l.location
        assert l.postal_code and l.postal_code.replace(" ", "").isdigit()
        assert l.posted_date is not None
    # nearly all listings have a numeric price; "Dohodou" etc. are rare
    assert sum(l.price is not None for l in listings) >= len(listings) * 0.8
    assert all(l.price > 0 for l in listings if l.price is not None)


def test_parse_search_fixture_header_row_is_not_a_listing():
    listings, _ = parse_search_page(load("bazos_search_query.html"), QUERY_URL)
    assert all("Zobrazeno" not in l.title for l in listings)


def test_parse_detail_fixture():
    html = load("bazos_detail.html")
    detail = parse_detail(html, "https://mobil.bazos.cz/inzerat/1/x.php")

    assert detail.title
    assert detail.price is not None and detail.price > 0
    assert detail.description and len(detail.description) > 10
    assert "<br>" not in detail.description
    assert detail.posted_date is not None
