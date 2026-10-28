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
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, ValidationError

from scanner.config import GeminiConfig, PricingConfig, Settings
from scanner.db import Database
from scanner.models import Listing, ListingDetail
from scanner.pc import PC_SYSTEM_PROMPT, PcSpecs
from scanner.pricing import (
    Evaluation, PriceList, asking_ceiling, evaluate, max_buy_price, max_price_rule_lines, offer_price,
)
from scanner.sources.bazos import BazosError

log = logging.getLogger(__name__)


class Verdict(BaseModel):
    """Response schema (passed to Gemini as response_schema)."""

    is_device: bool = Field(description="A real device for sale: not an accessory, part, 'koupím' or swap-only")
    model: str = Field(
        description="Full model name of what is actually sold, as in the price list "
        "(e.g. 'Apple Watch Series 10', 'iPhone 15 Pro', 'Nintendo Switch OLED')"
    )
    variant: str = Field(description="Storage / size / chip / RAM-SSD, as in the price list; '' if unknown")
    matches_price_item: bool = Field(description="Whether the price list item matched by the rules is right")
    condition_notes: list[str] = Field(
        description="Strictly in Russian (translate, never copy Czech): facts from the listing: "
        "battery %, repairs, cracks, accessories, warranty"
    )
    adjusted_market_price: int = Field(description="market_price adjusted for condition per KNOWLEDGE.md, CZK")
    red_flags: list[str] = Field(description="Strictly in Russian, short")
    scam_risk: Literal["low", "medium", "high"]
    verdict: Literal["buy", "negotiate", "skip"]
    offer_price: int = Field(description="Price for the first message, CZK: min(offer_cap, max_price - 5 %)")
    max_price: int = Field(description="Buy ceiling by the tiered rule, CZK (may be above the asking price)")
    reason_ru: str = Field(description="1-2 sentences, strictly in Russian")
    message_cs: str = Field(description="First message to the seller in Czech, formal 'vy', short, with offer_price")


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
- max_price (потолок покупки) и offer_price считай от adjusted_market_price (market) ступенями:
{max_price_rule}
  Ступень выбирается по adjusted_market_price. min_margin и liquidity даны во входе. Входные
  price_item.max_price и listing.offer_price посчитаны по рыночной цене без поправок.
  Первое предложение всегда с торгом, даже если цена объявления уже ниже max_price.
- max_price может быть выше цены объявления: это значит «если продавец упрётся, бери по его цене».
- verdict: buy, если цена объявления ≤ max_price; negotiate, если выше max_price, но не больше чем
  на ~15 % и есть признаки торга; иначе skip. При matches_price_item=false, is_device=false или
  scam_risk=high — skip.
- scam_risk: по разделу «Шаблоны мошенников». Магазин/сервис (IČO, záruka 12M) — это red_flag, не scam.
- Старое объявление: если age_days > 14 и скидка к рынку (rules.discount_pct) > 40 %, то verdict не
  выше negotiate, в red_flags добавь «висит N дней: уточни, актуально ли» (N = age_days), а message_cs
  начни с вопроса, актуально ли ещё объявление.
- Язык: condition_notes, red_flags и reason_ru строго по-русски. Чешские фразы из объявления переводи,
  не копируй. Коротко.
- reason_ru: 1–2 предложения.
- listing.source = vinted: личной встречи нет, Vinted присылает посылкой. Сверху цены покупатель платит
  listing.extra_cost (ochrana kupujících + doprava): прибыль = adjusted_market_price − цена − extra_cost,
  а max_price из правила уменьши на extra_cost (тоже вниз до 100). seller_abroad=true: продавец за рубежом
  (PL/SK/HU…), заголовок может быть не по-чешски, цена пересчитана в Kč. condition = состояние по шкале
  Vinted; у электроники проверяй его по описанию. Просьба оплатить мимо Vinted, перейти в WhatsApp/e-mail
  или отправить «своей» доставкой = scam_risk high. message_cs для Vinted без «osobní předání»,
  торговаться будут кнопкой «Nabídnout cenu», поэтому сообщение — короткий вопрос про состояние и цену.
- message_cs: первое сообщение продавцу по-чешски, на «vy», 2–3 коротких предложения, по шаблону
  из базы знаний, с ценой offer_price (именно это число). Без лишней вежливости и без эмодзи. Для iPhone спроси про
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
    rematched: str | None = None  # "rules item → item by Gemini's model" when Gemini disagreed

    @property
    def adjusted_market(self) -> int | None:
        return self.verdict.adjusted_market_price if self.verdict else self.evaluation.market_price

    @property
    def final_profit(self) -> int | None:
        """Profit at the asking price (+ Vinted fee and shipping), by the condition-adjusted market price."""
        listing, market = self.evaluation.listing, self.adjusted_market
        return None if listing.price is None or market is None else market - listing.price - listing.extra_cost

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


class NoAI(Exception):
    """No Gemini answer for this candidate; the message is the reason shown with "без ИИ"."""


def quota_day(tz: str, now: datetime | None = None) -> str:
    """Current quota day: the free tier resets RPD at midnight in `tz` (Pacific Time)."""
    return (now.astimezone(ZoneInfo(tz)) if now else datetime.now(ZoneInfo(tz))).date().isoformat()


def _retry_delay(exc: errors.APIError) -> float | None:
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', json.dumps(exc.details or {}))
    return float(m.group(1)) if m else None


def _is_daily_quota(exc: errors.APIError) -> bool:
    return "PerDay" in json.dumps(exc.details or {})


PRAGUE = ZoneInfo("Europe/Prague")
STALE_DAYS = 14
STALE_DISCOUNT = 0.40


def today_prague() -> date:
    return datetime.now(PRAGUE).date()


def offer_cap(price: int, pricing: PricingConfig) -> int:
    """Highest first offer: 92 % of the asking price, rounded down to 100."""
    return price * pricing.offer_cap_pct // 100 // 100 * 100


def system_prompt(pricing: PricingConfig) -> str:
    rule = "\n".join(f"  - {line}" for line in max_price_rule_lines(pricing))
    return SYSTEM_PROMPT.replace("{max_price_rule}", rule)


def age_days(ev: Evaluation, today: date) -> int | None:
    posted = ev.listing.posted_date
    return (today - posted).days if posted else None


def build_prompt(ev: Evaluation, pricing: PricingConfig, today: date | None = None) -> str:
    """User turn: the matched price list item, the rules' verdict and the listing itself."""
    assert ev.match and ev.market_price is not None and ev.listing.price is not None
    item, listing = ev.match.item, ev.listing
    today = today or today_prague()
    data = {
        "today": today.isoformat(),
        "price_item": {
            "category": item.category,
            "model": item.model,
            "variant": item.variant,
            "market_price": ev.market_price,
            "max_price": ev.max_price,
            "min_margin": pricing.category_min_profit(item.category),
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
            "offer_cap": offer_cap(listing.price, pricing),
            "offer_price": ev.offer_price,
            "location": listing.location,
            **({"extra_cost": listing.extra_cost, "seller_abroad": listing.foreign, "brand": listing.brand,
                "size": listing.size, "condition": listing.condition} if listing.source == "vinted" else {}),
            "posted_date": listing.posted_date.isoformat() if listing.posted_date else None,
            "age_days": age_days(ev, today),
            "description": listing.description or "(описания нет)",
        },
    }
    return json.dumps(data, ensure_ascii=False, indent=1)


def _price_re(value: int) -> re.Pattern[str]:
    """`value` as written in a message: 2500, 2 500, 2.500 (any space-like separator)."""
    digits = str(value)
    head, tail = digits[:-3], digits[-3:]
    if not head:
        return re.compile(rf"(?<!\d){digits}(?!\d)")
    return re.compile(rf"(?<!\d){head}[\s  .]?{tail}(?!\d)")


def enforce_rules(v: Verdict, ev: Evaluation, today: date, pricing: PricingConfig) -> Verdict:
    """Hard rules the model must not break, applied after every answer.

    max_price and offer_price are recomputed by the tiered rule from Gemini's adjusted market price.
    """
    price = ev.listing.price
    assert price is not None and ev.match is not None
    v = v.model_copy(deep=True)
    item = ev.match.item
    v.max_price = asking_ceiling(
        max_buy_price(max(0, v.adjusted_market_price), item.category, item.liquidity, pricing), ev.listing)
    if v.verdict != "skip":
        if v.verdict == "buy" and price > v.max_price:
            v.verdict = "negotiate"
        offer = offer_price(price, v.max_price, pricing)
        if v.offer_price != offer:
            old, v.offer_price = v.offer_price, offer
            if old > 0 and _price_re(old).search(v.message_cs):
                v.message_cs = _price_re(old).sub(str(offer), v.message_cs, count=1)
            elif v.message_cs and not _price_re(offer).search(v.message_cs):
                v.message_cs = v.message_cs.rstrip() + f" Šlo by to za {offer} Kč?"
    age = age_days(ev, today)
    if age is not None and age > STALE_DAYS and (ev.discount_pct or 0) > STALE_DISCOUNT:
        if v.verdict == "buy":
            v.verdict = "negotiate"
        if not any("висит" in f for f in v.red_flags):
            v.red_flags.append(f"висит {age} дней: уточни, актуально ли")
        first_question = v.message_cs.split("?")[0]
        if v.message_cs and not re.search(r"aktuáln|k dispozici|ještě prodáv", first_question):
            rest = re.sub(r"^\s*Dobrý den[,!.]?\s*", "", v.message_cs)
            v.message_cs = f"Dobrý den, je inzerát ještě aktuální? {rest[:1].upper()}{rest[1:]}"
    return v


def rematch(ev: Evaluation, v: Verdict, prices: PriceList, pricing: PricingConfig, today: date) -> Evaluation | None:
    """Re-evaluate the listing as the device Gemini says it is; None if that model is not priced."""
    named = replace(ev.listing, title=f"{v.model} {v.variant}".strip())
    new = evaluate(named, prices, pricing, today)
    if new.match is None:
        return None
    new.listing = ev.listing  # keep the real title for display and the cache
    return new


_SHARED_LIMITERS: dict[tuple[str, int, int], RateLimiter] = {}


def _limiter(model: str, settings: Settings, sleep: Callable[[float], Awaitable[None]]) -> RateLimiter:
    """One limiter per model for the whole process: a run makes several reviewers (deals, PCs) and Bazoš
    and Sbazar run back to back, so private limiters would together go over the RPM limit."""
    if sleep is not asyncio.sleep:  # tests with a fake sleep get their own
        return RateLimiter(settings.gemini_rpm, settings.gemini_tpm, sleep=sleep)
    key = (model, settings.gemini_rpm, settings.gemini_tpm)
    if key not in _SHARED_LIMITERS:
        _SHARED_LIMITERS[key] = RateLimiter(settings.gemini_rpm, settings.gemini_tpm)
    return _SHARED_LIMITERS[key]


class GeminiReviewer:
    def __init__(
        self,
        settings: Settings,
        cfg: GeminiConfig,
        pricing: PricingConfig,
        prices: PriceList,
        db: Database,
        *,
        client: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.settings, self.cfg, self.pricing, self.prices, self.db = settings, cfg, pricing, prices, db
        self.models = [m for m in (settings.gemini_model, settings.gemini_fallback_model) if m]
        self._sleep, self._now = sleep, now
        self._limiters = {m: _limiter(m, settings, sleep) for m in self.models}
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
            self._system = system_prompt(self.pricing) + self.cfg.knowledge_path.read_text(encoding="utf-8")
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

    def available_model(self, exclude: set[str] = frozenset()) -> str | None:
        usage = self.usage()
        return next((m for m in self.models if usage[m]["left"] > 0 and m not in exclude), None)

    async def review(self, ev: Evaluation) -> Review:
        listing = ev.listing
        assert ev.is_candidate and listing.price is not None
        row = self.db.get_review(listing.source, listing.id, listing.price)
        if row:
            return Review(
                ev, Verdict.model_validate_json(row["response"]), row["model"], cached=True,
                rematched=row["rematched"],
            )
        if self.disabled_reason:
            return Review(ev, no_ai_reason=self.disabled_reason)

        today = self._now().astimezone(PRAGUE).date()
        try:
            model, verdict = await self._ask(ev, today)
        except NoAI as exc:
            return Review(ev, no_ai_reason=str(exc))

        rematched = None
        if not verdict.is_device:  # the rules took an accessory / game for a device
            log.warning("pricing mismatch %s: %r is not a device per Gemini", listing.url, listing.title)
            self.db.log_mismatch(
                listing.source, listing.id, listing.title, ev.match.item.label,
                f"{verdict.model} {verdict.variant}".strip(), None, "не устройство",
            )
        elif not verdict.matches_price_item:
            ev, model, verdict, rematched = await self._rematch(ev, model, verdict, today)
        if not verdict.matches_price_item or not verdict.is_device:
            verdict = verdict.model_copy(update={"verdict": "skip"})
        verdict = enforce_rules(verdict, ev, today, self.pricing)

        review = Review(ev, verdict, model, rematched=rematched)
        self.db.save_review(
            listing.source, listing.id, listing.price, model, verdict.model_dump_json(),
            verdict.verdict, verdict.scam_risk, review.notify, rematched=rematched,
        )
        return review

    async def _ask(self, ev: Evaluation, today: date) -> tuple[str, Verdict]:
        """One answer from the first model with quota left; raises NoAI when there is none."""
        return await self._ask_prompt(build_prompt(ev, self.pricing, today), Verdict, self.system_instruction)

    async def _ask_prompt(self, prompt: str, schema: type[BaseModel], system: str) -> tuple[str, Any]:
        failed: set[str] = set()  # 503 "high demand" is per model: the fallback model often still answers
        error: GeminiUnavailable | None = None
        while model := self.available_model(exclude=failed):
            try:
                return model, await self._call(model, prompt, schema, system)
            except DailyQuotaExceeded:
                log.warning("Gemini %s: daily quota exceeded, switching model", model)
                self.db.mark_gemini_exhausted(self.day(), model)
            except GeminiUnavailable as exc:
                log.warning("Gemini %s unavailable (%s), trying the next model", model, exc)
                failed.add(model)
                error = exc
        if error is not None:
            log.warning("Gemini: no model answered, candidate goes out without AI")
            raise NoAI(f"Gemini недоступен: {error}") from error
        raise NoAI("дневной лимит Gemini исчерпан")

    async def extract_pc(self, listing: Listing) -> PcSpecs:
        """Hardware of a ready-built PC from its title and description (cached per listing).

        Raises NoAI when Gemini can't answer (the PC is then not valued in this run).
        """
        row = self.db.get_pc_specs(listing.source, listing.id)
        if row:
            return PcSpecs.model_validate_json(row["response"])
        if self.disabled_reason:
            raise NoAI(self.disabled_reason)
        prompt = json.dumps({"title": listing.title, "price": listing.price,
                             "description": listing.description or "(описания нет)"}, ensure_ascii=False, indent=1)
        model, specs = await self._ask_prompt(prompt, PcSpecs, PC_SYSTEM_PROMPT)
        self.db.save_pc_specs(listing.source, listing.id, model, specs.model_dump_json())
        return specs

    async def _rematch(
        self, ev: Evaluation, model: str, verdict: Verdict, today: date
    ) -> tuple[Evaluation, str, Verdict, str]:
        """Gemini says the rules picked the wrong price list item: retry with the item it names.

        The model is not in the price list -> skip. It is, but the listing is no candidate at that
        market price -> skip, recomputed without a request. Otherwise one more request.
        """
        old_label = ev.match.item.label
        new_ev = rematch(ev, verdict, self.prices, self.pricing, today)
        if new_ev is None:
            outcome, note = None, "модели нет в прайсе"
        elif new_ev.match.item is ev.match.item:
            outcome, note = new_ev, "та же позиция прайса"
        elif not new_ev.is_candidate:
            outcome, note = new_ev, "пересчитано без запроса: не кандидат"
            reasons = ", ".join(new_ev.flags) or "цена выше 90 % рынка"
            verdict = verdict.model_copy(update={
                "verdict": "skip",
                "adjusted_market_price": new_ev.market_price,
                "reason_ru": f"{verdict.reason_ru} По позиции «{new_ev.match.item.label}» "
                             f"(рынок {new_ev.market_price} Kč) не кандидат: {reasons}.",
            })
        else:
            try:
                model, verdict = await self._ask(new_ev, today)
                outcome, note = new_ev, "повторный запрос"
            except NoAI as exc:
                outcome, note = None, f"повторный запрос не удался ({exc})"
        new_label = outcome.match.item.label if outcome else "—"
        rematched = f"{old_label} → {new_label} ({note})"
        log.warning(
            "pricing mismatch %s: %r matched %r, Gemini says %r %r -> %s",
            ev.listing.url, ev.listing.title, old_label, verdict.model, verdict.variant, rematched,
        )
        self.db.log_mismatch(
            ev.listing.source, ev.listing.id, ev.listing.title, old_label,
            f"{verdict.model} {verdict.variant}".strip(), new_label if outcome else None, note,
        )
        return (outcome or ev), model, verdict, rematched

    async def _call(self, model: str, prompt: str, schema: type[BaseModel], system: str) -> Any:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
        )
        estimate = (len(system) + len(prompt)) // 3 + 1000
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
                    return resp.parsed if isinstance(resp.parsed, schema) else schema.model_validate_json(resp.text)
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
            except BazosError as exc:
                if exc.status in (404, 410):  # deleted since the search: no alert for it
                    reviews.append(Review(ev, rules_skip=f"объявление удалено (HTTP {exc.status})"))
                    continue
                log.warning("detail %s failed: %s", listing.url, exc)
            except Exception as exc:  # noqa: BLE001 - one broken detail page must not stop the run
                log.warning("detail %s failed: %s", listing.url, exc)
            else:
                listing.description = detail.description or ""
                db.set_description(listing.source, listing.id, listing.description)
            ev = evaluate(listing, prices, pricing, today_prague())
            if not ev.is_candidate:
                reasons = ev.flags + [f"stop:{s}" for s in ev.stop] + ev.scam_reasons
                reviews.append(Review(ev, rules_skip=", ".join(reasons) or "не прошёл правила"))
                continue
        reviews.append(await reviewer.review(ev))
    return reviews
