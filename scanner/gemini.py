"""Gemini "second opinion" for pricing candidates.

Only candidates from pricing.py are sent, once per listing and price (cached in SQLite).
Rate limits are ours (~50 % of the free tier) and enforced per model: RPM/TPM in a sliding
window in this process, RPD from the `gemini_usage` table. When the main model's daily quota is
spent we switch to the fallback model; when both are spent (or Gemini is down) the candidate is
returned without a verdict, so the caller notifies "without AI" instead of failing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, ValidationError

from scanner.config import GeminiConfig, PricingConfig, Settings
from scanner.db import Database
from scanner.models import ListingDetail
from scanner.pricing import MIN_PROFIT, Evaluation, PriceList, evaluate

log = logging.getLogger(__name__)


class Verdict(BaseModel):
    """Response schema (passed to Gemini as response_schema)."""

    is_device: bool = Field(description="A real device for sale: not an accessory, part, 'koupím' or swap-only")
    model: str = Field(description="What is actually sold, per title and description")
    variant: str = Field(description="Storage / size / chip / RAM-SSD, as in the price list; '' if unknown")
    matches_price_item: bool = Field(description="Whether the price list item matched by the rules is right")
    condition_notes: list[str] = Field(
        description="Facts from the listing: battery %, repairs, cracks, accessories, warranty"
    )
    adjusted_market_price: int = Field(description="market_price adjusted for condition per KNOWLEDGE.md, CZK")
    red_flags: list[str]
    scam_risk: Literal["low", "medium", "high"]
    verdict: Literal["buy", "negotiate", "skip"]
    offer_price: int = Field(description="Price for the first message, CZK")
    max_price: int = Field(description="Do not pay more than this, CZK")
    reason_ru: str = Field(description="1-2 sentences in Russian")
    message_cs: str = Field(description="First message to the seller in Czech, formal 'vy', short")


SYSTEM_PROMPT = """\
Ты помощник перекупщика б/у электроники в Чехии. Тебе дают одно объявление, которое правила
(pricing.py) уже отобрали как кандидата, и позицию прайса, с которой правила его сопоставили.
Твоя задача: второе мнение по базе знаний ниже. Ответ строго JSON по схеме.

Правила ответа:
- Опирайся только на факты из заголовка и описания. Не выдумывай состояние, которого нет в тексте.
- is_device=false: аксессуар, запчасть, коробка, «koupím», только обмен, игры без консоли.
- model/variant: что продают на самом деле (модель, память/размер/чип).
- matches_price_item=false, если позиция прайса не та (другая модель, поколение, память, размер).
  Тогда verdict=skip, а в reason_ru напиши, что это на самом деле.
- adjusted_market_price: market_price из прайса с поправками из раздела «Корректировки рыночной цены
  по состоянию». Нет данных о состоянии — без поправок. Целое число Kč.
- max_price: adjusted_market_price × buy_pct / 100, округлить до 100 (если «cena pevná», можно цель + 3 %).
  Прибыль adjusted_market_price − max_price не меньше min_profit.
- offer_price: на 5–10 % ниже max_price, округлить до 100, и не выше цены объявления.
- verdict: buy, если цена объявления ≤ max_price; negotiate, если выше max_price, но не больше чем
  на ~15 % и есть признаки торга; иначе skip. При matches_price_item=false, is_device=false или
  scam_risk=high — skip.
- scam_risk: по разделу «Шаблоны мошенников». Магазин/сервис (IČO, záruka 12M) — это red_flag, не scam.
- red_flags: конкретно и коротко, по-русски. condition_notes: по-русски, коротко.
- reason_ru: 1–2 предложения по-русски.
- message_cs: первое сообщение продавцу по-чешски, на «vy», 2–3 коротких предложения, по шаблону
  из базы знаний, с ценой offer_price. Без лишней вежливости и без эмодзи. Для iPhone спроси про
  батарею и сервис, для ПК/видеокарт про модели комплектующих и майнинг, если в описании этого нет.

# База знаний
"""


@dataclass
class Review:
    """Gemini verdict (or why there is none) for one candidate."""

    evaluation: Evaluation
    verdict: Verdict | None = None
    model: str | None = None
    cached: bool = False
    no_ai_reason: str | None = None  # set when the candidate goes out "without AI"
    rules_skip: str | None = None  # the candidate failed the rules after the description was loaded

    @property
    def adjusted_market(self) -> int | None:
        return self.verdict.adjusted_market_price if self.verdict else self.evaluation.market_price

    @property
    def final_profit(self) -> int | None:
        """Profit at the asking price, by the condition-adjusted market price."""
        price, market = self.evaluation.listing.price, self.adjusted_market
        return None if price is None or market is None else market - price

    @property
    def notify(self) -> bool:
        if self.rules_skip:
            return False
        if self.verdict is None:
            return True  # rules-only notification, marked "без ИИ"
        return self.verdict.verdict != "skip" and self.verdict.scam_risk != "high"


# --- rate limiting ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding 60 s window for requests and tokens of one model."""

    def __init__(
        self, rpm: int, tpm: int, *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.rpm, self.tpm = rpm, tpm
        self._clock, self._sleep = clock, sleep
        self._window: deque[list[float]] = deque()  # [timestamp, tokens]

    def _prune(self, now: float) -> None:
        while self._window and now - self._window[0][0] >= 60:
            self._window.popleft()

    async def acquire(self, tokens: int) -> list[float]:
        """Wait for a slot; returns the window entry so the caller can correct the token count."""
        while True:
            now = self._clock()
            self._prune(now)
            used = sum(t for _, t in self._window)
            if len(self._window) < self.rpm and (not self._window or used + tokens <= self.tpm):
                entry = [now, float(tokens)]
                self._window.append(entry)
                return entry
            wait = 60 - (now - self._window[0][0]) + 0.1
            log.info("Gemini rate limit: waiting %.0fs", wait)
            await self._sleep(wait)


# --- client ----------------------------------------------------------------------------------


class GeminiUnavailable(Exception):
    """Gemini can't answer now (network, 5xx, repeated 429, bad request); go on without AI."""


class DailyQuotaExceeded(Exception):
    pass


def quota_day(tz: str, now: datetime | None = None) -> str:
    """Current quota day: the free tier resets RPD at midnight in `tz` (Pacific Time)."""
    return (now.astimezone(ZoneInfo(tz)) if now else datetime.now(ZoneInfo(tz))).date().isoformat()


def _retry_delay(exc: errors.APIError) -> float | None:
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', json.dumps(exc.details or {}))
    return float(m.group(1)) if m else None


def _is_daily_quota(exc: errors.APIError) -> bool:
    return "PerDay" in json.dumps(exc.details or {})


def build_prompt(ev: Evaluation, pricing: PricingConfig, today: date | None = None) -> str:
    """User turn: the matched price list item, the rules' verdict and the listing itself."""
    assert ev.match and ev.market_price is not None
    item, listing = ev.match.item, ev.listing
    data = {
        "today": (today or date.today()).isoformat(),
        "price_item": {
            "category": item.category,
            "model": item.model,
            "variant": item.variant,
            "market_price": item.market_price,
            "target_buy": ev.target_buy,
            "buy_pct": pricing.buy_pct_low_liquidity if item.liquidity <= 1 else pricing.buy_pct,
            "min_profit": MIN_PROFIT.get(item.category, 1000),
            "liquidity": item.liquidity,
            "confidence": item.confidence,
        },
        "rules": {
            "match_confidence": ev.match.confidence,
            "match_notes": ev.match.notes,
            "discount_pct": round(ev.discount_pct * 100) if ev.discount_pct is not None else None,
            "expected_profit": ev.expected_profit,
            "flags": ev.flags,
        },
        "listing": {
            "source": listing.source,
            "url": listing.url,
            "title": listing.title,
            "price": listing.price,
            "location": listing.location,
            "posted_date": listing.posted_date.isoformat() if listing.posted_date else None,
            "description": listing.description or "(описания нет)",
        },
    }
    return json.dumps(data, ensure_ascii=False, indent=1)


class GeminiReviewer:
    def __init__(
        self,
        settings: Settings,
        cfg: GeminiConfig,
        pricing: PricingConfig,
        db: Database,
        *,
        client: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.settings, self.cfg, self.pricing, self.db = settings, cfg, pricing, db
        self.models = [m for m in (settings.gemini_model, settings.gemini_fallback_model) if m]
        self._sleep, self._now = sleep, now
        self._limiters = {m: RateLimiter(settings.gemini_rpm, settings.gemini_tpm, sleep=sleep) for m in self.models}
        self._client = client
        self._system: str | None = None
        self.disabled_reason: str | None = None
        if not cfg.enabled:
            self.disabled_reason = "Gemini выключен в config.yaml"
        elif client is None and not settings.gemini_api_key:
            self.disabled_reason = "GEMINI_API_KEY не задан"

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = genai.Client(
                api_key=self.settings.gemini_api_key,
                http_options=types.HttpOptions(timeout=int(self.cfg.timeout * 1000)),
            )
        return self._client

    @property
    def system_instruction(self) -> str:
        if self._system is None:
            self._system = SYSTEM_PROMPT + self.cfg.knowledge_path.read_text(encoding="utf-8")
        return self._system

    def day(self) -> str:
        return quota_day(self.settings.gemini_quota_tz, self._now())

    def usage(self) -> dict[str, dict[str, int]]:
        """Today's usage per configured model, with what is left of our RPD."""
        rows = self.db.gemini_usage(self.day())
        out = {}
        for model in self.models:
            r = rows.get(model)
            used = r["requests"] if r else 0
            out[model] = {
                "requests": used,
                "left": 0 if r and r["exhausted"] else max(0, self.settings.gemini_rpd - used),
                "errors": r["errors"] if r else 0,
                "prompt_tokens": r["prompt_tokens"] if r else 0,
                "output_tokens": r["output_tokens"] if r else 0,
                "total_tokens": r["total_tokens"] if r else 0,
            }
        return out

    def available_model(self) -> str | None:
        usage = self.usage()
        return next((m for m in self.models if usage[m]["left"] > 0), None)

    async def review(self, ev: Evaluation) -> Review:
        listing = ev.listing
        assert ev.is_candidate and listing.price is not None
        row = self.db.get_review(listing.source, listing.id, listing.price)
        if row:
            return Review(ev, Verdict.model_validate_json(row["response"]), row["model"], cached=True)
        if self.disabled_reason:
            return Review(ev, no_ai_reason=self.disabled_reason)

        prompt = build_prompt(ev, self.pricing, self._now().date())
        while model := self.available_model():
            try:
                verdict = await self._call(model, prompt)
            except DailyQuotaExceeded:
                log.warning("Gemini %s: daily quota exceeded, switching model", model)
                self.db.mark_gemini_exhausted(self.day(), model)
                continue
            except GeminiUnavailable as exc:
                log.warning("Gemini unavailable (%s), candidate goes out without AI", exc)
                return Review(ev, no_ai_reason=f"Gemini недоступен: {exc}")
            review = Review(ev, verdict, model)
            self.db.save_review(
                listing.source, listing.id, listing.price, model, verdict.model_dump_json(),
                verdict.verdict, verdict.scam_risk, review.notify,
            )
            return review
        return Review(ev, no_ai_reason="дневной лимит Gemini исчерпан")

    async def _call(self, model: str, prompt: str) -> Verdict:
        config = types.GenerateContentConfig(
            system_instruction=self.system_instruction,
            response_mime_type="application/json",
            response_schema=Verdict,
            temperature=0.2,
        )
        estimate = (len(self.system_instruction) + len(prompt)) // 3 + 1000
        error = ""
        for attempt in range(self.cfg.retries + 1):
            slot = await self._limiters[model].acquire(estimate)
            delay = None
            try:
                resp = await self.client.aio.models.generate_content(model=model, contents=prompt, config=config)
            except errors.APIError as exc:
                if exc.code == 429:
                    if _is_daily_quota(exc):
                        raise DailyQuotaExceeded from exc
                    delay = _retry_delay(exc)
                else:
                    # Failed requests that reached the model count towards the quota too.
                    self.db.add_gemini_usage(self.day(), model, requests=1, errors=1)
                    if exc.code is not None and exc.code < 500:
                        raise GeminiUnavailable(f"HTTP {exc.code}: {exc.message}") from exc
                error = f"HTTP {exc.code}: {exc.message}"
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                usage = resp.usage_metadata
                prompt_t = (usage.prompt_token_count or 0) if usage else 0
                total_t = (usage.total_token_count or 0) if usage else 0
                slot[1] = total_t or estimate
                self.db.add_gemini_usage(
                    self.day(), model, requests=1, prompt_tokens=prompt_t,
                    output_tokens=total_t - prompt_t, total_tokens=total_t,
                )
                log.info("Gemini %s: %d prompt + %d output tokens", model, prompt_t, total_t - prompt_t)
                try:
                    return resp.parsed if isinstance(resp.parsed, Verdict) else Verdict.model_validate_json(resp.text)
                except (ValidationError, ValueError, TypeError) as exc:
                    error = f"invalid JSON: {exc}"
            if attempt < self.cfg.retries:
                backoff = delay if delay is not None else self.cfg.backoff_base * 2**attempt
                backoff += random.uniform(0, 2)
                log.warning("Gemini %s: %s, retry %d in %.0fs", model, error, attempt + 1, backoff)
                await self._sleep(backoff)
        raise GeminiUnavailable(f"{error} (после {self.cfg.retries + 1} попыток)")


# --- pipeline --------------------------------------------------------------------------------


async def review_candidates(
    candidates: list[Evaluation],
    reviewer: GeminiReviewer,
    db: Database,
    prices: PriceList,
    pricing: PricingConfig,
    fetch_detail: Callable[[str], Awaitable[ListingDetail]],
) -> list[Review]:
    """Load descriptions, re-check the rules with them, then ask Gemini (in the given order)."""
    reviews = []
    for ev in candidates:
        listing = ev.listing
        cached = listing.price is not None and db.get_review(listing.source, listing.id, listing.price)
        if listing.description is None and not cached:
            try:
                detail = await fetch_detail(listing.url)
            except Exception as exc:  # noqa: BLE001 - one broken detail page must not stop the run
                log.warning("detail %s failed: %s", listing.url, exc)
            else:
                listing.description = detail.description or ""
                db.set_description(listing.source, listing.id, listing.description)
            ev = evaluate(listing, prices, pricing)
            if not ev.is_candidate:
                reasons = ev.flags + [f"stop:{s}" for s in ev.stop] + ev.scam_reasons
                reviews.append(Review(ev, rules_skip=", ".join(reasons) or "не прошёл правила"))
                continue
        reviews.append(await reviewer.review(ev))
    return reviews
