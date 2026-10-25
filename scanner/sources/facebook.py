"""Facebook Marketplace source (Playwright, Chromium, a persistent profile logged in by hand).

Careful by design (see knowledge/KNOWLEDGE.md, "Техника парсинга → Facebook Marketplace"):
- off by default (FB_ENABLED), a run every ~25 min with jitter, sequential pages, human pauses;
- detail pages only for candidates, at most `facebook.details_per_run` per run;
- no passwords: `python -m scanner fb-login` opens a visible browser and you log in yourself;
- a login wall, checkpoint or captcha is never worked around: FbLoginRequired stops the source, the
  bot asks you to run fb-login and waits;
- language independent where possible (the UI may be Russian): prices by "CZK|Kč", item ids from links.
Location and radius come from the FB account settings; listings from cities outside the configured
list are dropped, FB mixes in the whole country.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
import logging
import random
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

from scanner.config import AppConfig, BazosSearch, FacebookConfig
from scanner.models import Listing, ListingDetail
from scanner.pricing import normalize
from scanner.sources.bazos import ScanResult

log = logging.getLogger(__name__)

SOURCE = "fb"
BASE = "https://www.facebook.com"

_ITEM_RE = re.compile(r"/marketplace/item/(\d+)")
_PRICE_RE = re.compile(r"(?:(?:CZK|Kč)\s*(\d[\d\s  .,]*\d|\d))|(?:(\d[\d\s  .,]*\d|\d)\s*(?:CZK|Kč))")
# Lines FB puts on cards that are neither a title nor a city, in any UI language we may see.
_NOISE_RE = re.compile(
    r"^(?:недавно опубликовано|just listed|nově přidáno|nově zveřejněno|бесплатно|free|zdarma|"
    r"продано|sold|prodáno|в наличии|available|·)$", re.I,
)
_SOLD_RE = re.compile(r"^(?:продано|sold|prodáno)$", re.I | re.M)
_SELLER_RE = re.compile(r"^(?:информация о продавце|seller information|informace o prodejci)", re.I | re.M)
_LOGIN_URL_RE = re.compile(r"/login|/checkpoint|/two_step_verification|/recover|captcha", re.I)


class FbLoginRequired(Exception):
    """FB shows a login wall / checkpoint / captcha: stop, ask the owner to run fb-login."""


class FbDetailLimit(Exception):
    """The per-run limit of detail pages is spent."""


# --- pure parsers ---------------------------------------------------------------------------


def search_url(search: BazosSearch, cfg: FacebookConfig) -> str:
    params = {"query": search.query or search.name, "daysSinceListed": cfg.days_since_listed, "exact": "false"}
    if search.price_min:
        params["minPrice"] = search.price_min
    if search.price_max:
        params["maxPrice"] = search.price_max
    if cfg.location:
        params["radius"] = cfg.radius_km
        return f"{BASE}/marketplace/{cfg.location}/search?{urlencode(params)}"
    return f"{BASE}/marketplace/search?{urlencode(params)}"


def parse_price(text: str) -> int | None:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1) or m.group(2))
    return int(digits) if digits else None


def _is_price_line(line: str) -> bool:
    """"12 500 CZK", "CZK 12,500", "12 500 Kč": only a number and the currency."""
    return bool(re.fullmatch(r"[\d\s  .,]*(?:CZK|Kč)[\d\s  .,]*", line)) and parse_price(line) is not None


def parse_card(href: str, text: str, today: date) -> Listing | None:
    """One result card: `a[href*="/marketplace/item/"]` and its innerText (price, title, city lines)."""
    m = _ITEM_RE.search(href)
    if not m:
        return None
    price, rest = None, []
    for line in (l.strip() for l in text.splitlines()):
        if not line or _NOISE_RE.match(line):
            continue
        if _is_price_line(line):
            if price is None:  # the first price; a second one is the old, crossed-out price
                price = parse_price(line)
            continue
        rest.append(line)
    if not rest:
        return None
    title = rest[0]
    city = rest[1].split(",")[0].strip() if len(rest) > 1 else None
    item_id = m.group(1)
    return Listing(source=SOURCE, id=item_id, url=f"{BASE}/marketplace/item/{item_id}/", title=title, price=price,
                   location=city, posted_date=today)


def parse_cards(cards: list[dict], today: date) -> list[Listing]:
    seen, out = set(), []
    for c in cards:
        listing = parse_card(c.get("href", ""), c.get("text", ""), today)
        if listing and listing.id not in seen:
            seen.add(listing.id)
            out.append(listing)
    return out


def allowed_cities(cfg: AppConfig) -> set[str]:
    return {normalize(c) for c in (*cfg.pricing.home_cities, *cfg.pricing.near_cities, *cfg.facebook.extra_cities)}


def nearby(listings: list[Listing], cities: set[str]) -> list[Listing]:
    return [l for l in listings if l.location and normalize(l.location) in cities]


def parse_detail_text(url: str, text: str) -> ListingDetail:
    """innerText of [role=main] / [role=dialog] of an item page."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title = lines[0] if lines else ""
    price = next((p for l in lines[:6] if (p := parse_price(l)) is not None), None)
    body = text
    if m := _SELLER_RE.search(text):
        body = text[:m.start()]
    body_lines = [l.strip() for l in body.splitlines() if l.strip()]
    # Drop the header (title, prices, "listed ... ago · city"); the description is what follows.
    start = next((i for i, l in enumerate(body_lines) if i > 0 and parse_price(l) is None and len(l) > 40), None)
    description = "\n".join(body_lines[start:]) if start is not None else None
    if _SOLD_RE.search(body):
        description = (description or "") + "\nProdáno"
    return ListingDetail(url=url, title=title, price=price, description=description, posted_date=None)


def valid_state(data: dict) -> bool:
    """A Playwright storage state with a logged-in Facebook session (the c_user cookie)."""
    cookies = data.get("cookies") if isinstance(data, dict) else None
    return isinstance(cookies, list) and any(
        isinstance(c, dict) and c.get("name") == "c_user" and "facebook.com" in str(c.get("domain", ""))
        for c in cookies)


def login_required(url: str, has_login_form: bool) -> bool:
    return bool(_LOGIN_URL_RE.search(url)) or has_login_form


# --- the browser client --------------------------------------------------------------------

_CARDS_JS = """() => Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]'))
    .map(a => ({href: a.getAttribute('href') || '', text: a.innerText || ''}))"""
_LOGIN_FORM_JS = """() => !!document.querySelector(
    'form[action*="login"], input[name="email"][type="text"], input[name="pass"], iframe[src*="captcha"]')"""
_DETAIL_JS = """() => (document.querySelector('div[role=dialog]') || document.querySelector('[role=main]')
    || document.body).innerText"""


class FacebookClient:
    """Use as `async with FacebookClient(cfg, profile_dir) as fb:`; same interface as BazosClient."""

    source = SOURCE

    def __init__(self, cfg: AppConfig, profile_dir: Path, browser_path: str | None = None, headless: bool | None = None,
                 state_path: Path | None = None, channel: str | None = None):
        self.cfg, self.fb = cfg, cfg.facebook
        self.profile_dir, self.browser_path, self.state_path = profile_dir, browser_path, state_path
        self.channel = channel  # "chrome" = the installed Google Chrome (fb-login on a PC)
        self.headless = self.fb.headless if headless is None else headless
        self._pw = self._ctx = self._page = None
        self._details = 0
        self._raw: dict[str, str] = {}
        self.stage = "запуск браузера"  # what it is doing now, for the bot's timeout message
        self._lock = asyncio.Lock()  # one page at a time

    async def __aenter__(self) -> FacebookClient:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # optional dependency: pip install -e ".[fb]"
            raise RuntimeError("Playwright is not installed: pip install -e \".[fb]\"") from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless, viewport={"width": 1280, "height": 900},
                **({"channel": self.channel} if self.channel else {"executable_path": self.browser_path}),
            )
        except Exception:
            await self._pw.stop()
            self._pw = None
            raise
        self._ctx.set_default_timeout(self.fb.timeout * 1000)
        await self._import_state()
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        return self

    async def _import_state(self) -> None:
        """A session sent from a PC (fb-login there): its cookies go into this profile, once.

        The file is renamed afterwards, so newer cookies FB sets in the profile are not overwritten
        by the old file on the next start.
        """
        if not self.state_path or not self.state_path.exists():
            return
        cookies = json.loads(self.state_path.read_text(encoding="utf-8")).get("cookies", [])
        await self._ctx.add_cookies(cookies)
        self.state_path.replace(self.state_path.with_name(self.state_path.stem + ".imported.json"))
        log.info("fb: imported %d cookies from %s", len(cookies), self.state_path.name)

    async def export_state(self, path: Path) -> None:
        """Save the logged-in session (cookies) to a file, to send it to the bot on a server."""
        await self._ctx.storage_state(path=str(path))

    async def __aexit__(self, *exc) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()

    async def _pause(self, lo: float, hi: float) -> None:
        await asyncio.sleep(random.uniform(lo, hi))

    async def _open(self, url: str) -> None:
        log.info("fb: open %s", url.split("?")[0])
        try:
            await self._page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - FB is sometimes slow: one more try after a pause
            if "Timeout" not in str(exc):
                raise
            log.warning("fb: %s timed out, one more try", url.split("?")[0])
            await self._pause(5, 10)
            await self._page.goto(url, wait_until="domcontentloaded")
        await self._pause(1.5, 3.0)
        if login_required(self._page.url, await self._page.evaluate(_LOGIN_FORM_JS)):
            raise FbLoginRequired(f"FB requires a login ({self._page.url})")

    async def search(self, search: BazosSearch) -> list[Listing]:
        self.stage = f"поиск «{search.name}»"
        async with self._lock:
            await self._open(search_url(search, self.fb))
            for _ in range(self.fb.scrolls):
                await self._page.mouse.wheel(0, random.randint(1800, 2600))
                await self._pause(self.fb.scroll_pause, self.fb.scroll_pause + 1.0)
            cards = await self._page.evaluate(_CARDS_JS)
            log.info("fb: %r: %d item links on %s", search.name, len(cards), self._page.url.split("?")[0])
        for c in cards:  # raw card texts, to explain dropped listings in the log
            if m := _ITEM_RE.search(c.get("href", "")):
                self._raw[m.group(1)] = c.get("text", "")
        return parse_cards(cards, date.today())

    async def scan(self) -> ScanResult:
        """All searches (the Bazoš ones), sequential, human pauses; FbLoginRequired propagates."""
        result, seen = ScanResult(), set()
        cities, dropped, dropped_cities = allowed_cities(self.cfg), 0, Counter()
        samples: dict[str | None, str] = {}
        now = datetime.now()
        for i, search in enumerate(self.cfg.bazos.searches):
            if i:
                await self._pause(*self.fb.search_pause)
            try:
                found = await self.search(search)
            except FbLoginRequired:
                raise
            except Exception as exc:  # noqa: BLE001 - one broken search must not stop the run
                log.error("fb search %r failed: %s", search.name, exc)
                result.errors.append(f"fb {search.name}: {exc}")
                continue
            near = nearby(found, cities)
            dropped += len(found) - len(near)
            kept = {l.id for l in near}
            for l in found:
                if l.id not in kept:
                    dropped_cities[l.location] += 1
                    samples.setdefault(l.location, self._raw.get(l.id, ""))
            for listing in near:
                if listing.id not in seen:
                    seen.add(listing.id)
                    listing.first_seen = listing.last_seen = now
                    result.listings.append(listing)
        log.info("fb: %d listings nearby, %d dropped as too far / no city", len(result.listings), dropped)
        if dropped_cities:
            log.info("fb: dropped cities: %s", ", ".join(f"{c or '(no city)'} x{n}" for c, n in dropped_cities.most_common(15)))
            for city in list(samples)[:3]:
                log.info("fb: sample card (%s): %s", city or "no city", " | ".join(samples[city].splitlines())[:200])
        self.stage = "оценка и Gemini"
        return result

    async def fetch_detail(self, url: str) -> ListingDetail:
        if self._details >= self.fb.details_per_run:
            raise FbDetailLimit(f"fb detail limit ({self.fb.details_per_run}) reached")
        self._details += 1
        self.stage = f"страница объявления {url}"
        async with self._lock:
            await self._pause(3.0, 7.0)
            await self._open(url)
            if "unavailable_product" in self._page.url:
                return ListingDetail(url=url, title="", price=None, description="Prodáno / smazáno", posted_date=None)
            text = await self._page.evaluate(_DETAIL_JS)
        return parse_detail_text(url, text)

    async def is_logged_in(self) -> bool:
        cookies = await self._ctx.cookies(BASE)
        return any(c["name"] == "c_user" for c in cookies)

    async def login_interactively(self, timeout_s: float = 600) -> bool:
        """fb-login: a visible browser on Marketplace; you log in by hand (2FA included).

        Returns True as soon as FB sets its login cookie, False when the window is closed or after
        `timeout_s` without a login.
        """
        await self._page.goto(f"{BASE}/marketplace/", wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if self._page.is_closed():
                return False
            try:
                if await self.is_logged_in():
                    await self._pause(3, 5)  # let FB finish its redirects so the profile is complete
                    return True
            except Exception:  # noqa: BLE001 - the browser was closed
                return False
            await asyncio.sleep(2)
        return False
