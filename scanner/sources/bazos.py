"""Bazoš.cz source: search result pages and listing details.

Page structure is documented in data/KNOWLEDGE.md ("Техника парсинга → Bazoš").
Requests are strictly sequential with a random pause between them.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlencode, urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser

from scanner.config import BazosConfig, BazosSearch
from scanner.models import Listing, ListingDetail

log = logging.getLogger(__name__)

SOURCE = "bazos"
PAGE_SIZE = 20
RETRY_STATUSES = {429, 500, 502, 503, 504}

_ID_RE = re.compile(r"/inzerat/(\d+)/")
_DATE_RE = re.compile(r"\[(\d{1,2})\.(\d{1,2})\.\s*(\d{4})\]")
_PRICE_RE = re.compile(r"(\d[\d\s ]*)\s*Kč")
_CENA_RE = re.compile(r"Cena:\s*([^\n]+)")


class BazosError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status of a final non-retryable answer (410 = listing deleted)


# --- parsing (pure functions, covered by tests) ---------------------------------------------


def parse_price(text: str | None) -> int | None:
    """'6 990 Kč' -> 6990; 'Dohodou' / 'Nabídněte' / 'V textu' -> None."""
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    return int(re.sub(r"\D", "", m.group(1)))


def parse_date(text: str | None) -> date | None:
    """'... - [15.10. 2026]' -> date(2026, 10, 15)."""
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def build_search_url(search: BazosSearch, cfg: BazosConfig, page: int = 0) -> str:
    """Page is 0-based; Bazoš paginates by offset in the path: /20/, /40/ ..."""
    path = f"/{search.category}/" if search.category else "/"
    if page:
        path += f"{page * PAGE_SIZE}/"
    params: dict[str, str | int] = {}
    if search.query:
        params["hledat"] = search.query
        params["rubriky"] = search.section
    params["hlokalita"] = cfg.location
    params["humkreis"] = cfg.radius_km
    if search.price_min is not None:
        params["cenaod"] = search.price_min
    if search.price_max is not None:
        params["cenado"] = search.price_max
    return f"https://{search.section}.bazos.cz{path}?{urlencode(params)}"


def parse_search_page(html: str, base_url: str) -> tuple[list[Listing], bool]:
    """Returns (listings, has_next_page)."""
    tree = LexborHTMLParser(html)
    listings: list[Listing] = []
    for card in tree.css("div.inzeraty"):
        link = card.css_first(".nadpis a")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        m = _ID_RE.search(href)
        if not m:
            continue

        city, postal_code = None, None
        lok = card.css_first(".inzeratylok")
        if lok is not None:
            parts = [p.strip() for p in lok.text(separator="\n").split("\n") if p.strip()]
            city = parts[0] if parts else None
            postal_code = parts[1] if len(parts) > 1 else None

        cena = card.css_first(".inzeratycena")
        meta = card.css_first(".velikost10")
        listings.append(
            Listing(
                source=SOURCE,
                id=m.group(1),
                url=urljoin(base_url, href),
                title=link.text(strip=True),
                price=parse_price(cena.text() if cena else None),
                location=city,
                postal_code=postal_code,
                posted_date=parse_date(meta.text() if meta else None),
                is_top=card.css_first(".ztop") is not None,
            )
        )

    has_next = any(a.text(strip=True) == "Další" for a in tree.css(".strankovani a"))
    return listings, has_next


def _clean_description(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_detail(html: str, url: str) -> ListingDetail:
    tree = LexborHTMLParser(html)
    h1 = tree.css_first("h1.nadpisdetail") or tree.css_first("h1")
    body_text = tree.body.text(separator="\n") if tree.body else ""
    cena = _CENA_RE.search(body_text)
    popis = tree.css_first(".popisdetail")
    return ListingDetail(
        url=url,
        title=h1.text(strip=True) if h1 else "",
        price=parse_price(cena.group(1)) if cena else None,
        description=_clean_description(popis.text()) if popis else None,
        posted_date=parse_date(body_text),
    )


# --- network ---------------------------------------------------------------------------------


@dataclass
class ScanResult:
    listings: list[Listing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class BazosClient:
    """Sequential, polite HTTP client. Use as `async with BazosClient(cfg) as client:`."""

    def __init__(self, cfg: BazosConfig):
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            headers={
                "User-Agent": cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
            },
            timeout=cfg.timeout,
            follow_redirects=True,
        )
        self._lock = asyncio.Lock()  # guarantees no parallel requests to Bazoš
        self._last_request: float | None = None

    async def __aenter__(self) -> BazosClient:
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
            error: str = ""
            for attempt in range(self.cfg.retries + 1):
                await self._pause()
                try:
                    resp = await self._http.get(url)
                except httpx.TransportError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                else:
                    if resp.status_code < 400:
                        return resp.text
                    if resp.status_code not in RETRY_STATUSES:
                        raise BazosError(f"GET {url} -> HTTP {resp.status_code}", resp.status_code)
                    error = f"HTTP {resp.status_code}"
                finally:
                    self._last_request = time.monotonic()

                if attempt < self.cfg.retries:
                    backoff = 5 * 2**attempt + random.uniform(0, 3)
                    log.warning("Bazoš %s (%s), retry %d in %.0fs", error, url, attempt + 1, backoff)
                    await asyncio.sleep(backoff)
            raise BazosError(f"GET {url} failed after {self.cfg.retries + 1} attempts: {error}")

    async def search(self, search: BazosSearch) -> list[Listing]:
        results: list[Listing] = []
        for page in range(self.cfg.pages):
            url = build_search_url(search, self.cfg, page)
            html = await self.get(url)
            listings, has_next = parse_search_page(html, url)
            log.debug("bazos %r page %d: %d listings", search.name, page + 1, len(listings))
            results.extend(listings)
            if not has_next:
                break
        return results

    async def scan(self) -> ScanResult:
        """Run all configured searches; listings are deduplicated by id across searches."""
        result = ScanResult()
        seen: set[str] = set()
        now = datetime.now()
        for search in self.cfg.searches:
            try:
                listings = await self.search(search)
            except BazosError as exc:
                log.error("bazos search %r failed: %s", search.name, exc)
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
        """`url` must be the full listing URL including the slug (see KNOWLEDGE.md)."""
        return parse_detail(await self.get(url), url)
