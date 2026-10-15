import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from google.genai import errors
from pydantic import ValidationError

from scanner.config import GeminiConfig, PricingConfig, Settings
from scanner.db import Database
from scanner.gemini import (
    GeminiReviewer,
    RateLimiter,
    Verdict,
    build_prompt,
    quota_day,
    review_candidates,
)
from scanner.models import Listing, ListingDetail
from scanner.pricing import PriceList, evaluate

PRAGUE = ZoneInfo("Europe/Prague")
NOW = datetime(2026, 10, 15, 14, 0, tzinfo=PRAGUE)
PRICING = PricingConfig()
PRICES = PriceList.load(PRICING.prices_path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Settings must not pick up the developer's real GEMINI_* variables."""
    for name in ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACK_MODEL", "GEMINI_RPM", "GEMINI_RPD",
                 "GEMINI_TPM", "GEMINI_QUOTA_TZ"):
        monkeypatch.delenv(name, raising=False)


def verdict(**kw) -> Verdict:
    base = dict(
        is_device=True, model="iPhone 15", variant="128 GB", matches_price_item=True,
        condition_notes=["батарея 88 %"], adjusted_market_price=11000, red_flags=[], scam_risk="low",
        verdict="buy", offer_price=7400, max_price=7900, reason_ru="Хорошая цена.",
        message_cs="Dobrý den, je iPhone ještě k dispozici? Šlo by to za 7400 Kč? Děkuji.",
    )
    return Verdict(**(base | kw))


def response(v: Verdict, prompt_tokens: int = 4000, total: int = 4200):
    return SimpleNamespace(
        parsed=v, text=v.model_dump_json(),
        usage_metadata=SimpleNamespace(prompt_token_count=prompt_tokens, total_token_count=total),
    )


def quota_error(daily: bool) -> errors.ClientError:
    quota_id = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if daily else "GenerateRequestsPerMinute"
    details = [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"},
    ]
    return errors.ClientError(
        429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": details}}
    )


class FakeClient:
    """Mimics client.aio.models.generate_content; `script` items are responses or exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model, contents, config):
        self.calls.append(model)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Sleeps:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_listing(**kw) -> Listing:
    base = dict(
        source="bazos", id="1", url="https://mobil.bazos.cz/inzerat/1/iphone-15.php",
        title="iPhone 15 128GB", price=None, location="Pardubice", posted_date=date(2026, 10, 15),
        description="Telefon v pěkném stavu, baterie 88 %.",
    )
    return Listing(**(base | kw))


def candidate(**kw):
    market = PRICES.match("iPhone 15 128GB").item.market_price
    ev = evaluate(make_listing(price=int(market * 0.7), **kw), PRICES, PRICING, today=date(2026, 10, 15))
    assert ev.is_candidate
    return ev


def make_reviewer(db, script, *, settings=None, **cfg):
    client = FakeClient(script)
    sleeps = Sleeps()
    reviewer = GeminiReviewer(
        settings or Settings(_env_file=None, gemini_api_key="test"),
        GeminiConfig(**cfg), PRICING, db, client=client, sleep=sleeps, now=lambda: NOW,
    )
    return reviewer, client, sleeps


def run(coro):
    return asyncio.run(coro)


# --- settings / helpers ------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Flash", "gemini-flash-latest", "gemini-flash-lite-latest"])
def test_settings_reject_aliases(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, gemini_model=bad)


def test_settings_defaults_and_fallback_off():
    s = Settings(_env_file=None, gemini_model="", gemini_fallback_model="none")
    assert s.gemini_model == "gemini-3.5-flash-lite"
    assert s.gemini_fallback_model is None
    assert (s.gemini_rpm, s.gemini_rpd, s.gemini_tpm) == (7, 250, 120_000)


def test_quota_day_resets_at_pacific_midnight():
    tz = "America/Los_Angeles"
    assert quota_day(tz, datetime(2026, 10, 15, 8, 30, tzinfo=PRAGUE)) == "2026-10-14"
    assert quota_day(tz, datetime(2026, 10, 15, 9, 30, tzinfo=PRAGUE)) == "2026-10-15"
    assert quota_day("Europe/Prague", datetime(2026, 10, 15, 0, 30, tzinfo=PRAGUE)) == "2026-10-15"


def test_rate_limiter_rpm_and_tpm():
    now = [0.0]
    waits = []

    async def sleep(s):
        waits.append(s)
        now[0] += s

    limiter = RateLimiter(rpm=2, tpm=10_000, clock=lambda: now[0], sleep=sleep)

    async def scenario():
        await limiter.acquire(1000)
        await limiter.acquire(1000)
        await limiter.acquire(1000)  # 3rd request in the same minute waits
        assert waits and 59 < waits[-1] <= 60.1
        waits.clear()
        await limiter.acquire(9500)  # one slot left, but tokens would exceed TPM
        assert waits

    run(scenario())


def test_build_prompt_has_item_and_listing():
    prompt = build_prompt(candidate(), PRICING, date(2026, 10, 15))
    assert '"market_price"' in prompt and '"target_buy"' in prompt and '"liquidity"' in prompt
    assert "baterie 88 %" in prompt and '"location": "Pardubice"' in prompt


# --- reviewer ----------------------------------------------------------------------------------


def test_review_caches_and_logs_usage():
    db = Database(":memory:")
    ev = candidate()
    db.upsert(ev.listing)
    reviewer, client, _ = make_reviewer(db, [response(verdict())])

    first = run(reviewer.review(ev))
    assert first.verdict.verdict == "buy" and first.model == "gemini-3.5-flash-lite" and first.notify
    assert first.final_profit == 11000 - ev.listing.price

    second = run(reviewer.review(ev))  # same listing and price: from the cache, no request
    assert second.cached and second.verdict == first.verdict
    assert len(client.calls) == 1

    usage = reviewer.usage()["gemini-3.5-flash-lite"]
    assert usage["requests"] == 1 and usage["prompt_tokens"] == 4000 and usage["output_tokens"] == 200
    assert usage["left"] == 249


@pytest.mark.parametrize("kw", [{"verdict": "skip"}, {"scam_risk": "high", "verdict": "negotiate"}])
def test_skip_or_high_scam_is_stored_but_not_sent(kw):
    db = Database(":memory:")
    ev = candidate()
    db.upsert(ev.listing)
    reviewer, _, _ = make_reviewer(db, [response(verdict(**kw))])
    review = run(reviewer.review(ev))
    assert not review.notify
    rows = db.reviews(datetime(2026, 1, 1), only_hidden=True)
    assert [r["listing_id"] for r in rows] == ["1"]


def test_per_minute_429_backs_off_and_retries():
    db = Database(":memory:")
    reviewer, client, sleeps = make_reviewer(db, [quota_error(daily=False), response(verdict())])
    review = run(reviewer.review(candidate()))
    assert review.verdict and client.calls == ["gemini-3.5-flash-lite"] * 2
    assert 12 <= sleeps.calls[0] <= 14  # the API's retryDelay


def test_daily_quota_switches_to_fallback_then_no_ai():
    db = Database(":memory:")
    reviewer, client, _ = make_reviewer(
        db, [quota_error(daily=True), response(verdict()), quota_error(daily=True)]
    )
    review = run(reviewer.review(candidate()))
    assert review.model == "gemini-3.1-flash-lite"
    assert reviewer.usage()["gemini-3.5-flash-lite"]["left"] == 0  # exhausted until the quota day ends

    review = run(reviewer.review(candidate(id="2")))
    assert review.verdict is None and review.notify and "лимит" in review.no_ai_reason
    assert client.calls == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.1-flash-lite"]


def test_own_rpd_limit_uses_fallback_without_asking_main_model():
    db = Database(":memory:")
    db.add_gemini_usage("2026-10-15", "gemini-3.5-flash-lite", requests=250)
    reviewer, client, _ = make_reviewer(db, [response(verdict())])
    assert run(reviewer.review(candidate())).model == "gemini-3.1-flash-lite"
    assert client.calls == ["gemini-3.1-flash-lite"]


def test_server_errors_give_no_ai_review():
    db = Database(":memory:")
    err = errors.ServerError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
    reviewer, client, sleeps = make_reviewer(db, [err] * 3, retries=2)
    review = run(reviewer.review(candidate()))
    assert review.verdict is None and review.notify and "503" in review.no_ai_reason
    assert len(client.calls) == 3 and len(sleeps.calls) == 2
    assert reviewer.usage()["gemini-3.5-flash-lite"]["errors"] == 3


def test_no_api_key_means_no_ai():
    db = Database(":memory:")
    reviewer = GeminiReviewer(Settings(_env_file=None, gemini_api_key=None), GeminiConfig(), PRICING, db)
    review = run(reviewer.review(candidate()))
    assert review.verdict is None and review.notify


def test_review_candidates_rechecks_rules_with_description():
    db = Database(":memory:")
    ev = candidate(description=None)
    db.upsert(ev.listing)
    reviewer, client, _ = make_reviewer(db, [])

    async def fetch_detail(url):
        return ListingDetail(url, ev.listing.title, ev.listing.price, "Telefon je blokovaný na iCloud.", None)

    [review] = run(review_candidates([ev], reviewer, db, PRICES, PRICING, fetch_detail))
    assert review.rules_skip and not review.notify
    assert client.calls == []  # no Gemini request for a listing the rules reject
    assert db.get("bazos", "1")["description"] == "Telefon je blokovaný na iCloud."
