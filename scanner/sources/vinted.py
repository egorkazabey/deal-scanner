"""Vinted.cz source: search result pages and listing details.

Page structure is documented in knowledge/KNOWLEDGE.md ("Техника парсинга → Vinted"). The site is a Next.js
app: search results are in the React flight data (`self.__next_f.push(...)` scripts), a listing detail has a
schema.org Product in ld+json. No login. Vinted ships across the country and mixes in foreign sellers
(Poland, Slovakia, Hungary, ...) with prices converted to CZK, so a listing costs its price + the buyer
protection fee (Vinted gives it) + shipping (config). Requests are strictly sequential with a random pause.
An anti-bot / captcha page raises VintedBlocked: never bypassed, the bot stops Vinted and says so.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from scanner.config import BazosSearch, VintedConfig
from scanner.models import Listing, ListingDetail
from scanner.sources.bazos import RETRY_STATUSES, BazosError, ScanResult

log = logging.getLogger(__name__)

SOURCE = "vinted"
BASE_URL = "https://www.vinted.cz"

_FLIGHT_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_ITEMS_KEY = '"items":{"items":'
_ITEM_PATH_RE = re.compile(r"^/items/\d+")
# Captcha / anti-bot pages (DataDome, Cloudflare challenge): we stop, never solve them.
_BLOCK_RE = re.compile(r"captcha-delivery\.com|datadome|cf-chl-|challenge-platform|Just a moment\.\.\.", re.I)
_LABEL_RE = {key: re.compile(rf"\b{word}: ([^,]+)") for key, word in
             (("brand", "Značka"), ("size", "Velikost"), ("condition", "Stav"))}


class VintedError(BazosError):
    """Same shape as BazosError (`.status`), so the gone / sold checks treat all HTTP sources alike."""


class VintedBlocked(VintedError):
    """Anti-bot page or captcha: Vinted is stopped until the owner checks it, nothing is bypassed."""


def search_url(search: BazosSearch, cfg: VintedConfig, page: int = 0) -> str:
    """Search URL, newest first; `page` is 0-based. A Bazoš category search uses `queries` or its name."""
    params: list[tuple[str, Any]] = [("search_text", cfg.queries.get(search.name) or search.query or search.name)]
    params += [("catalog[]", c) for c in cfg.catalogs.get(search.name, [])]
    if search.price_min is not None:
        params.append(("price_from", search.price_min))
    if search.price_max is not None:
        params.append(("price_to", search.price_max))
    params += [("currency", "CZK"), ("order", "newest_first")]
    if page:
        params.append(("page", page + 1))
    return f"{BASE_URL}/catalog?{urlencode(params)}"


def flight_text(page: str) -> str:
    """The React flight payload of a page: the string parts of all `self.__next_f.push([1, "..."])` calls."""
    parts = []
    for raw in _FLIGHT_RE.findall(page):
        try:
            chunk = json.loads(raw)
        except ValueError:
            continue
        if len(chunk) > 1 and isinstance(chunk[1], str):
            parts.append(chunk[1])
    return "".join(parts)


def _amount(money: Any) -> Decimal | None:
    try:
        return Decimal(str(money["amount"])) if isinstance(money, dict) else None
    except (KeyError, InvalidOperation):
        return None


def is_foreign(price: Decimal) -> bool:
    """Czech sellers price in whole koruna; a foreign price converted to CZK has hellers (2964.74)."""
    return price % 1 != 0


def extra_cost(fee: Decimal | None, price: Decimal, foreign: bool, cfg: VintedConfig) -> int:
    """Buyer protection fee (Vinted gives it; else 5 % + 18 Kč) + shipping to a pickup point."""
    if fee is None:
        fee = price * Decimal("0.05") + 18
    return int(round(fee)) + (cfg.shipping_abroad_czk if foreign else cfg.shipping_czk)


def parse_item(item: dict, cfg: VintedConfig) -> Listing | None:
    """One search result (`{"id", "productItem": {...}}`); broken items and other currencies give None."""
    p = item.get("productItem") if isinstance(item.get("productItem"), dict) else item
    price = _amount(p.get("price"))
    if not p.get("id") or not p.get("url") or price is None or (p.get("price") or {}).get("currencyCode") != "CZK":
        return None
    foreign = is_foreign(price)
    if foreign and not cfg.foreign:
        return None
    box = p.get("itemBox") or {}
    label = box.get("accessibilityLabel") or ""
    attrs = {key: (m.group(1).strip() if (m := rx.search(label)) else None) for key, rx in _LABEL_RE.items()}
    return Listing(
        source=SOURCE,
        id=str(p["id"]),
        url=urljoin(BASE_URL, p["url"]),
        title=(p.get("title") or "").strip(),
        price=int(price) or None,
        location=None,  # Vinted shows no city in the results; it ships anyway
        posted_date=None,  # not in the results: the pipeline keeps the day it was first seen
        is_top=bool(p.get("isPromoted")),
        extra_cost=extra_cost(_amount(p.get("serviceFee")), price, foreign, cfg),
        foreign=foreign,
        brand=attrs["brand"],
        size=attrs["size"],
        condition=attrs["condition"],
    )


def parse_search_page(page: str, cfg: VintedConfig) -> tuple[list[Listing], bool]:
    """Listings of one result page and whether another page follows."""
    text = flight_text(page)
    start = text.find(_ITEMS_KEY)
    if start < 0:
        if _BLOCK_RE.search(page[:20000]):
            raise VintedBlocked("Vinted: anti-bot / captcha page instead of search results")
        raise VintedError("Vinted: no items in the page data (layout changed?)")
    decoder = json.JSONDecoder()
    items, end = decoder.raw_decode(text, start + len(_ITEMS_KEY))
    listings = [l for item in items if isinstance(item, dict) and (l := parse_item(item, cfg))]
    has_next = False
    if (pos := text.find('"pagination":', end, end + 2000)) >= 0:
        pag, _ = decoder.raw_decode(text, pos + len('"pagination":'))
        has_next = (pag.get("current_page") or 0) < (pag.get("total_pages") or 0)
    return listings, has_next and bool(items)


def trim_search_page(page: str, keep: int = 12, ids: set[str] = frozenset()) -> str:
    """A small copy of a result page for test fixtures (a real one is ~3.5 MB): `keep` items + `ids`."""
    text = flight_text(page)
    start = text.index(_ITEMS_KEY) + len(_ITEMS_KEY)
    items, end = json.JSONDecoder().raw_decode(text, start)
    picked = [it for n, it in enumerate(items) if n < keep or str(it.get("id")) in ids]
    tail = text[end:text.find("}", text.find('"pagination":', end)) + 1]
    data = '{"catalog":{"items":{"items":' + json.dumps(picked, ensure_ascii=False) + tail + "}}}"
    return f"<html><body><script>self.__next_f.push({json.dumps([1, data], ensure_ascii=False)})</script></body></html>\n"


def trim_detail_page(page: str) -> str:
    """A small copy of a listing page for test fixtures: its ld+json Product only."""
    blocks = "".join(f'<script type="application/ld+json">{b}</script>' for b in _LD_RE.findall(page))
    return f"<html><body>{blocks}</body></html>\n"


def _product(page: str) -> dict:
    for block in _LD_RE.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "Product":
                return d
    return {}


def is_sold(page: str) -> bool:
    """A listing page whose offer is no longer in stock (sold or reserved and closed)."""
    offers = _product(page).get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    availability = offers.get("availability")
    return bool(availability) and not str(availability).endswith("InStock")


def parse_detail(page: str, url: str) -> ListingDetail:
    product = _product(page)
    if not product and _BLOCK_RE.search(page[:20000]):
        raise VintedBlocked("Vinted: anti-bot / captcha page instead of the listing")
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    try:
        price = int(Decimal(str(offers.get("price")))) or None
    except (InvalidOperation, TypeError):
        price = None
    description = html.unescape(product.get("description") or "").strip() or None
    return ListingDetail(url=url, title=html.unescape(product.get("name") or "").strip(), price=price,
                         description=description, posted_date=None)


class VintedClient:
    """Sequential, polite HTTP client. Use as `async with VintedClient(cfg, searches) as client:`."""

    source = SOURCE

    def __init__(self, cfg: VintedConfig, searches: list[BazosSearch]):
        self.cfg = cfg
        self.searches = searches
        self._http = httpx.AsyncClient(
            headers={"User-Agent": cfg.user_agent, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                     "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"},
            timeout=cfg.timeout,
            follow_redirects=False,
        )
        self._lock = asyncio.Lock()  # no parallel requests to Vinted
        self._last_request: float | None = None
        self._session = False  # the home page sets the anonymous session cookies, as in a browser

    async def __aenter__(self) -> VintedClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def _pause(self) -> None:
        if self._last_request is None:
            return
        wait = random.uniform(self.cfg.delay_min, self.cfg.delay_max)
        remaining = wait - (time.monotonic() - self._last_request)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def get(self, url: str) -> str:
        """A page's HTML. A listing page that is sold raises VintedError(410), like a deleted Bazoš listing."""
        async with self._lock:
            if not self._session:
                self._session = True
                await self._get(BASE_URL + "/")
            page = await self._get(url)
        if _ITEM_PATH_RE.match(urlsplit(url).path) and is_sold(page):
            raise VintedError(f"GET {url}: sold", 410)
        return page

    async def _get(self, url: str) -> str:
        error = ""
        for attempt in range(self.cfg.retries + 1):
            await self._pause()
            try:
                resp = await self._fetch(url)
            except httpx.TransportError as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    return resp.text
                if resp.status_code == 403 or (resp.status_code == 429 and _BLOCK_RE.search(resp.text[:20000])):
                    raise VintedBlocked(f"GET {url} -> HTTP {resp.status_code} (anti-bot)", resp.status_code)
                if resp.status_code not in RETRY_STATUSES:
                    raise VintedError(f"GET {url} -> HTTP {resp.status_code}", resp.status_code)
                error = f"HTTP {resp.status_code}"
            finally:
                self._last_request = time.monotonic()

            if attempt < self.cfg.retries:
                backoff = 5 * 2**attempt + random.uniform(0, 3)
                log.warning("Vinted %s (%s), retry %d in %.0fs", error, url, attempt + 1, backoff)
                await asyncio.sleep(backoff)
        raise VintedError(f"GET {url} failed after {self.cfg.retries + 1} attempts: {error}")

    async def _fetch(self, url: str) -> httpx.Response:
        """GET following redirects within vinted.cz only; a redirect elsewhere (captcha) means blocked."""
        for _ in range(4):
            resp = await self._http.get(url)
            if not resp.is_redirect:
                return resp
            url = urljoin(url, resp.headers.get("location", ""))
            if urlsplit(url).hostname != urlsplit(BASE_URL).hostname:
                raise VintedBlocked(f"Vinted redirected to {urlsplit(url).hostname} (anti-bot / login)")
        raise VintedError(f"Vinted: too many redirects ({url})")

    async def search(self, search: BazosSearch) -> list[Listing]:
        results: list[Listing] = []
        for page in range(self.cfg.pages):
            listings, has_next = parse_search_page(await self.get(search_url(search, self.cfg, page)), self.cfg)
            log.debug("vinted %r page %d: %d listings", search.name, page + 1, len(listings))
            results.extend(listings)
            if not has_next:
                break
        return results

    async def scan(self) -> ScanResult:
        """Run all searches (the Bazoš ones); listings are deduplicated by id across searches.

        VintedBlocked stops the whole run (raised): the next searches would hit the same wall.
        """
        result = ScanResult()
        seen: set[str] = set()
        now = datetime.now()
        for search in self.searches:
            try:
                listings = await self.search(search)
            except VintedBlocked:
                raise
            except BazosError as exc:
                log.error("vinted search %r failed: %s", search.name, exc)
                result.errors.append(f"{search.name}: {exc}")
                continue
            for listing in listings:
                if listing.id in seen:
                    continue
                seen.add(listing.id)
                listing.first_seen = listing.last_seen = now
                result.listings.append(listing)
        return result

    async def fetch_detail(self, url: str) -> ListingDetail:
        return parse_detail(await self.get(url), url)
