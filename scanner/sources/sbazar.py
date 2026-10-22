"""Sbazar.cz source: search result pages and listing details.

Page structure is documented in knowledge/KNOWLEDGE.md ("Техника парсинга → Sbazar"). The site is
built on Astro: search results are the JSON props of the "SearchPageList" island, a listing detail
has a schema.org Product in ld+json. Requests are strictly sequential with a random pause between them.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from scanner.config import BazosSearch, SbazarConfig
from scanner.models import Listing, ListingDetail
from scanner.sources.bazos import RETRY_STATUSES, BazosError, ScanResult

log = logging.getLogger(__name__)

SOURCE = "sbazar"
BASE_URL = "https://www.sbazar.cz"

_ISLAND_RE = re.compile(r"<astro-island\b[^>]*>")
_PROPS_RE = re.compile(r'\bprops="([^"]*)"')
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class SbazarError(BazosError):
    """Same shape as BazosError (`.status`), so the gone / sold checks treat both sources alike."""


def search_url(search: BazosSearch, cfg: SbazarConfig, page: int = 0) -> str:
    """Search URL; `page` is 0-based. A Bazoš category search uses its name (or `sbazar_queries`) as text."""
    query = cfg.queries.get(search.name) or search.query or search.name
    lo, hi = search.price_min, search.price_max
    if lo is None and hi is None:
        price = "cena-neomezena"
    else:
        price = f"cena-od-{lo or 0}-do-{hi or 99999999}-kc"
    path = f"/hledej/{quote(query)}/0-vsechny-kategorie/{cfg.locality}/{price}"
    if page:
        path += f"/nejnovejsi/{page + 1}"
    return f"{BASE_URL}{path}?radius={cfg.radius_km}"


def _decode(value: Any) -> Any:
    """Astro island props: [0, value] is a value, [1, [...]] an array; everything nested the same way."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        kind, inner = value
        if kind == 0:
            return _decode(inner) if isinstance(inner, (dict, list)) else inner
        if kind == 1:
            return [_decode(x) for x in inner]
        return inner
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(x) for x in value]
    return value


def _island_props(page: str, component: str) -> dict | None:
    for tag in _ISLAND_RE.findall(page):
        if component in tag and (m := _PROPS_RE.search(tag)):
            return _decode(json.loads(html.unescape(m.group(1))))
    return None


def _date(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(value).date() if value else None
    except (TypeError, ValueError):
        return None


def parse_item(item: dict) -> Listing | None:
    """One search result. Zboží.cz shop ads (string id, no locality) and broken items give None."""
    locality = item.get("locality")
    if not isinstance(item.get("id"), int) or not isinstance(locality, dict) or not item.get("seoName"):
        return None
    price = item.get("price")
    if item.get("priceByAgreement") or not isinstance(price, int) or price <= 0:
        price = None
    return Listing(
        source=SOURCE,
        id=str(item["id"]),
        url=f"{BASE_URL}/inzerat/{item['seoName']}",
        title=(item.get("name") or "").strip(),
        price=price,
        location=locality.get("municipality") or locality.get("district") or None,
        posted_date=_date(item.get("createDate")),
        postal_code=locality.get("zip") or None,
        is_top=bool(item.get("topped")),
    )


def parse_search_page(page: str) -> tuple[list[Listing], bool]:
    """Listings of one result page and whether another page follows."""
    props = _island_props(page, "SearchPageList")
    if props is None:
        raise SbazarError("Sbazar: no SearchPageList island (layout changed?)")
    offers = props.get("offers") or {}
    listings = [l for item in offers.get("results") or [] if isinstance(item, dict) and (l := parse_item(item))]
    pag = offers.get("pagination") or {}
    has_next = (pag.get("offset") or 0) + (pag.get("limit") or 0) < (pag.get("total") or 0)
    return listings, has_next and bool(offers.get("results"))


def parse_detail(page: str, url: str) -> ListingDetail:
    product: dict = {}
    for block in _LD_RE.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "Product":
                product = d
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    try:
        price = int(float(offers.get("price"))) or None
    except (TypeError, ValueError):
        price = None
    description = html.unescape(product.get("description") or "").strip() or None
    return ListingDetail(url=url, title=html.unescape(product.get("name") or "").strip(), price=price,
                         description=description, posted_date=None)


class SbazarClient:
    """Sequential, polite HTTP client. Use as `async with SbazarClient(cfg) as client:`."""

    source = SOURCE

    def __init__(self, cfg: SbazarConfig, searches: list[BazosSearch]):
        self.cfg = cfg
        self.searches = searches
        # A browser-like "Accept: text/html" sends us to Seznam autologin + the cookie consent wall
        # (cmp.seznam.cz); a plain Accept gets the page itself.
        self._http = httpx.AsyncClient(
            headers={"User-Agent": cfg.user_agent, "Accept": "*/*", "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"},
            timeout=cfg.timeout,
            follow_redirects=False,
        )
        self._lock = asyncio.Lock()  # no parallel requests to Sbazar
        self._last_request: float | None = None

    async def __aenter__(self) -> SbazarClient:
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
        async with self._lock:
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
                    if resp.status_code not in RETRY_STATUSES:
                        raise SbazarError(f"GET {url} -> HTTP {resp.status_code}", resp.status_code)
                    error = f"HTTP {resp.status_code}"
                finally:
                    self._last_request = time.monotonic()

                if attempt < self.cfg.retries:
                    backoff = 5 * 2**attempt + random.uniform(0, 3)
                    log.warning("Sbazar %s (%s), retry %d in %.0fs", error, url, attempt + 1, backoff)
                    await asyncio.sleep(backoff)
            raise SbazarError(f"GET {url} failed after {self.cfg.retries + 1} attempts: {error}")

    async def _fetch(self, url: str) -> httpx.Response:
        """GET following redirects within sbazar.cz only; login / consent redirects are an error."""
        for _ in range(4):
            resp = await self._http.get(url)
            if not resp.is_redirect:
                return resp
            url = urljoin(url, resp.headers.get("location", ""))
            if urlsplit(url).hostname != urlsplit(BASE_URL).hostname:
                raise SbazarError(f"Sbazar redirected to {urlsplit(url).hostname} (login / consent wall)")
        raise SbazarError(f"Sbazar: too many redirects ({url})")

    async def search(self, search: BazosSearch) -> list[Listing]:
        results: list[Listing] = []
        for page in range(self.cfg.pages):
            url = search_url(search, self.cfg, page)
            listings, has_next = parse_search_page(await self.get(url))
            log.debug("sbazar %r page %d: %d listings", search.name, page + 1, len(listings))
            results.extend(listings)
            if not has_next:
                break
        return results

    async def scan(self) -> ScanResult:
        """Run all searches (the Bazoš ones); listings are deduplicated by id across searches."""
        result = ScanResult()
        seen: set[str] = set()
        now = datetime.now()
        for search in self.searches:
            try:
                listings = await self.search(search)
            except BazosError as exc:
                log.error("sbazar search %r failed: %s", search.name, exc)
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
