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
    enforce_rules,
    offer_cap,
    quota_day,
    review_candidates,
    system_prompt,
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
        GeminiConfig(**cfg), PRICING, PRICES, db, client=client, sleep=sleeps, now=lambda: NOW,
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
    assert '"market_price": 10000' in prompt and '"max_price": 7800' in prompt and '"liquidity"' in prompt
    assert '"min_margin": 1000' in prompt and '"offer_price": 6400' in prompt
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
    reviewer, client, sleeps = make_reviewer(db, [err] * 6, retries=2)
    review = run(reviewer.review(candidate()))
    assert review.verdict is None and review.notify and "503" in review.no_ai_reason
    assert client.calls == ["gemini-3.5-flash-lite"] * 3 + ["gemini-3.1-flash-lite"] * 3  # both models tried
    assert reviewer.usage()["gemini-3.5-flash-lite"]["errors"] == 3


def test_overloaded_main_model_falls_back():
    db = Database(":memory:")
    err = errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
    reviewer, client, _ = make_reviewer(db, [err, err, response(verdict())], retries=1)
    review = run(reviewer.review(candidate()))
    assert review.model == "gemini-3.1-flash-lite" and review.verdict
    assert reviewer.usage()["gemini-3.5-flash-lite"]["left"] > 0  # not marked as out of quota


def test_no_api_key_means_no_ai():
    db = Database(":memory:")
    reviewer = GeminiReviewer(Settings(_env_file=None, gemini_api_key=None), GeminiConfig(), PRICING, PRICES, db)
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


# --- hard rules after the answer --------------------------------------------------------------


def test_offer_capped_at_92_pct_and_message_follows():
    db = Database(":memory:")
    ev = candidate()  # price 7 000 -> cap 6 400
    v = verdict(offer_price=7000, max_price=7900, message_cs="Dobrý den, šlo by to za 7 000 Kč? Děkuji.")
    reviewer, _, _ = make_reviewer(db, [response(v)])
    review = run(reviewer.review(ev))
    assert review.verdict.offer_price == 6400
    assert "6400 Kč" in review.verdict.message_cs and "7 000" not in review.verdict.message_cs
    # max by the tiers from Gemini's adjusted market 11 000: min(11 000 × 0.78, 11 000 − 1 200) -> 8 500;
    # it may be above the asking price
    assert review.verdict.max_price == 8500


@pytest.mark.parametrize("price, cap", [(7000, 6400), (2500, 2300), (15990, 14700), (1000, 900)])
def test_offer_cap_values(price, cap):
    assert offer_cap(price, PRICING) == cap


def test_offer_and_max_follow_the_rule():
    ev = candidate()  # 7 000 Kč; Gemini: adjusted market 11 000 -> max 8 500, offer min(6 440, 8 075) -> 6 400
    v = enforce_rules(verdict(offer_price=5800, max_price=9900, message_cs="Šlo by to za 5800 Kč?"),
                      ev, date(2026, 10, 15), PRICING)
    assert (v.max_price, v.offer_price) == (8500, 6400) and v.message_cs == "Šlo by to za 6400 Kč?"


def test_offer_below_max_when_asking_is_high():
    ev = candidate()
    # Gemini: battery 79 %, adjusted market 7 500 -> max min(5 850, 6 300) -> 5 800, offer 5 800 × 0.95 -> 5 500
    v = enforce_rules(verdict(adjusted_market_price=7500, offer_price=6400, message_cs="Šlo by to za 6 400 Kč?"),
                      ev, date(2026, 10, 15), PRICING)
    assert (v.max_price, v.offer_price) == (5800, 5500)
    assert v.verdict == "negotiate"  # asking 7 000 > max: not "buy"
    assert v.message_cs == "Šlo by to za 5500 Kč?"


def test_skip_keeps_offer_but_gets_max():
    v = enforce_rules(verdict(verdict="skip", offer_price=0), candidate(), date(2026, 10, 15), PRICING)
    assert v.verdict == "skip" and v.offer_price == 0 and v.max_price == 8500


def test_system_prompt_has_tiered_rule():
    text = system_prompt(PRICING)
    assert "{max_price_rule}" not in text
    assert "market < 5 000: max_price = min(market × 0.70, market − 1 000)" in text
    assert "market > 12 000: max_price = min(market × 0.85, market − 2 000)" in text
    assert "offer_price = min(0.92 × цена объявления, max_price − 5 %)" in text


def test_stale_big_discount_is_negotiate_with_question():
    market = PRICES.match("iPhone 15 128GB").item.market_price
    ev = evaluate(make_listing(price=int(market * 0.55), posted_date=date(2026, 9, 20)), PRICES, PRICING,
                  today=date(2026, 10, 15))
    assert ev.is_candidate
    v = enforce_rules(
        verdict(offer_price=5000, message_cs="Dobrý den, šlo by to za 5000 Kč při osobním předání?"),
        ev, date(2026, 10, 15), PRICING,
    )
    assert v.verdict == "negotiate"
    assert "висит 25 дней: уточни, актуально ли" in v.red_flags
    assert v.message_cs == "Dobrý den, je inzerát ještě aktuální? Šlo by to za 5000 Kč při osobním předání?"
    # "je ... ještě k dispozici?" already asks whether the listing is still up
    asks = "Dobrý den, je iPhone ještě k dispozici? Šlo by to za 5000 Kč?"
    assert enforce_rules(verdict(offer_price=5000, message_cs=asks), ev, date(2026, 10, 15), PRICING).message_cs == asks


def test_stale_small_discount_untouched():
    ev = evaluate(make_listing(price=7000, posted_date=date(2026, 9, 20)), PRICES, PRICING, today=date(2026, 10, 15))
    v = enforce_rules(verdict(offer_price=6000), ev, date(2026, 10, 15), PRICING)  # 30 % discount
    assert v.verdict == "buy" and not v.red_flags


def test_prompt_has_dates_and_offer_cap():
    prompt = build_prompt(candidate(posted_date=date(2026, 10, 1)), PRICING, date(2026, 10, 15))
    assert '"today": "2026-10-15"' in prompt and '"posted_date": "2026-10-01"' in prompt
    assert '"age_days": 14' in prompt and '"offer_cap": 6400' in prompt


# --- rematch when Gemini disagrees with pricing.py ---------------------------------------------


def test_rematch_to_priced_model_makes_one_more_request():
    db = Database(":memory:")
    ev = candidate()  # rules: iPhone 15 128 GB, price 7 000
    wrong = verdict(matches_price_item=False, model="iPhone 15 Pro", variant="128 GB", verdict="skip")
    right = verdict(model="iPhone 15 Pro", adjusted_market_price=13000, offer_price=6400, max_price=9300)
    reviewer, client, _ = make_reviewer(db, [response(wrong), response(right)])
    review = run(reviewer.review(ev))
    assert len(client.calls) == 2
    assert review.verdict.verdict == "buy" and review.evaluation.match.item.model.startswith("iPhone 15 Pro")
    assert review.evaluation.listing.title == "iPhone 15 128GB"  # the real title is kept
    assert "повторный запрос" in review.rematched
    [m] = db.mismatches(datetime(2026, 1, 1))
    assert m["gemini_model"] == "iPhone 15 Pro 128 GB" and m["gemini_item"]


def test_rematch_not_a_candidate_is_recomputed_without_request():
    db = Database(":memory:")
    ev = candidate()  # 7 000 Kč is no deal for an iPhone 13 128 GB
    wrong = verdict(matches_price_item=False, model="iPhone 13", variant="128 GB", verdict="skip")
    reviewer, client, _ = make_reviewer(db, [response(wrong)])
    review = run(reviewer.review(ev))
    assert len(client.calls) == 1 and review.verdict.verdict == "skip"
    assert review.verdict.adjusted_market_price == review.evaluation.market_price
    assert "без запроса" in review.rematched


def test_rematch_model_not_in_price_list_is_skip():
    db = Database(":memory:")
    wrong = verdict(matches_price_item=False, model="Samsung Galaxy S23", variant="128 GB", verdict="buy")
    reviewer, client, _ = make_reviewer(db, [response(wrong)])
    review = run(reviewer.review(candidate()))
    assert len(client.calls) == 1 and review.verdict.verdict == "skip" and not review.notify
    assert "нет в прайсе" in review.rematched
    assert db.mismatches(datetime(2026, 1, 1))[0]["gemini_item"] is None


def test_not_a_device_is_logged_for_pricing():
    db = Database(":memory:")
    game = verdict(is_device=False, matches_price_item=False, model="Unrailed (hra)", verdict="buy")
    reviewer, client, _ = make_reviewer(db, [response(game)])
    review = run(reviewer.review(candidate()))
    assert len(client.calls) == 1 and review.verdict.verdict == "skip" and not review.notify
    assert db.mismatches(datetime(2026, 1, 1))[0]["outcome"] == "не устройство"
