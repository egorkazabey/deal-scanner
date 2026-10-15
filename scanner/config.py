"""Settings from .env (secrets) and config.yaml (searches and source options)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None
    gemini_api_key: str | None = None
    # Explicit model ids only (no *-latest aliases): the free tier quota is per model.
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_fallback_model: str | None = "gemini-3.1-flash-lite"  # used when the main model's RPD is spent
    # Our own limits per model, ~50 % of the free tier (15 RPM, 250K TPM, 500 RPD per model).
    gemini_rpm: int = 7
    gemini_rpd: int = 250
    gemini_tpm: int = 120_000
    # Free tier daily quota resets at midnight Pacific Time (~09:00 in Prague).
    gemini_quota_tz: str = "America/Los_Angeles"

    db_path: Path = PROJECT_ROOT / "data" / "scanner.db"
    config_path: Path = PROJECT_ROOT / "config.yaml"
    log_level: str = "INFO"
    log_dir: Path = PROJECT_ROOT / "data" / "logs"  # scanner.log + errors.log, rotated (persistent volume)

    @field_validator("gemini_model", "gemini_fallback_model", mode="before")
    @classmethod
    def _explicit_model(cls, value: str | None, info: ValidationInfo) -> str | None:
        value = (value or "").strip()
        if not value:
            return cls.model_fields[info.field_name].default
        if value.lower() in {"none", "off"} and info.field_name == "gemini_fallback_model":
            return None
        if not re.fullmatch(r"gemini-[a-z0-9.\-]+", value) or value.endswith("latest"):
            raise ValueError(f"{value!r}: use an explicit Gemini model id like 'gemini-3.5-flash-lite'")
        return value

    @field_validator("gemini_quota_tz")
    @classmethod
    def _known_tz(cls, value: str) -> str:
        ZoneInfo(value)  # raises for an unknown zone
        return value


class BazosSearch(BaseModel):
    name: str
    section: Literal["mobil", "pc"]
    query: str | None = None
    category: str | None = None  # category path, e.g. "graficka" -> pc.bazos.cz/graficka/
    price_min: int | None = None
    price_max: int | None = None

    @model_validator(mode="after")
    def _query_or_category(self) -> BazosSearch:
        if bool(self.query) == bool(self.category):
            raise ValueError(f"search {self.name!r}: set exactly one of 'query' or 'category'")
        return self


class BazosConfig(BaseModel):
    enabled: bool = True
    location: str = "56201"
    radius_km: int = 60
    pages: int = 2
    delay_min: float = 2.0
    delay_max: float = 5.0
    timeout: float = 20.0
    retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )
    searches: list[BazosSearch] = []


class MaxPriceTier(BaseModel):
    """Buy ceiling for one market price band: max = min(market * pct %, market - min_margin)."""

    up_to: int | None = None  # market price upper bound, inclusive; None = the last band
    pct: int
    min_margin: int


class PricingConfig(BaseModel):
    prices_path: Path = PROJECT_ROOT / "knowledge" / "prices.json"
    components_path: Path = PROJECT_ROOT / "knowledge" / "components.json"  # used parts, for ready-built PCs
    max_price_tiers: list[MaxPriceTier] = [
        MaxPriceTier(up_to=4999, pct=70, min_margin=1000),
        MaxPriceTier(up_to=12000, pct=78, min_margin=1200),
        MaxPriceTier(pct=85, min_margin=2000),
    ]
    low_liquidity_pct_drop: int = 4  # liquidity = 1: the tier's pct minus this
    min_profit: int = 1000  # expected profit below this: "low_profit", no candidate
    min_profit_by_category: dict[str, int] = {"macbook": 2000, "gpu": 2000, "pc": 2000}  # also the ceiling margin
    offer_cap_pct: int = 92  # first offer <= 92 % of the asking price ...
    offer_below_max_pct: int = 5  # ... and <= max price - 5 %, rounded down to 100
    max_price_pct: int = 90  # candidate: price <= market * 90 %
    min_price_pct: int = 40  # below that: "too_cheap" (scam or broken), not a candidate
    stale_days: int = 14  # older listings get the "stale" flag and a score penalty
    discount_cap_pct: int = 45  # a bigger discount scores no higher (it is more likely a catch)
    bundle_accessory_pct: int = 50  # extra controllers in a console bundle add this % of their price
    home_cities: list[str] = []  # score bonus +10
    near_cities: list[str] = []  # score bonus +6

    @field_validator("max_price_tiers")
    @classmethod
    def _ordered_tiers(cls, tiers: list[MaxPriceTier]) -> list[MaxPriceTier]:
        bounds = [t.up_to for t in tiers]
        if not tiers or bounds[-1] is not None or None in bounds[:-1] or bounds[:-1] != sorted(set(bounds[:-1])):
            raise ValueError("max_price_tiers: ascending up_to, the last tier without up_to")
        return tiers

    def category_min_profit(self, category: str) -> int:
        return self.min_profit_by_category.get(category, self.min_profit)


class GeminiConfig(BaseModel):
    """Non-secret Gemini options; model ids and rate limits live in .env (GEMINI_*)."""

    enabled: bool = True
    knowledge_path: Path = PROJECT_ROOT / "knowledge" / "KNOWLEDGE.md"  # system instruction
    timeout: float = 60.0  # seconds per request
    retries: int = 4  # on 429 (per-minute) / 5xx / network errors
    backoff_base: float = 5.0  # seconds; doubled per attempt unless the API says how long to wait


class NotifyConfig(BaseModel):
    """Which candidates go to Telegram and when the scheduler runs (times in Europe/Prague)."""

    max_age_days: int = 3  # only listings posted in the last 3 days ...
    price_drop_days: int = 3  # ... or with a price drop in the last 3 days; stale ones only in /top
    watch_drop_pct: int = 5  # "🔁 Снизится — сообщи": alert when the price drops by >= 5 %
    max_alerts_per_run: int = 8  # Gemini reviews per run; the rest waits for the next run
    pc_specs_per_run: int = 5  # ready-built PCs whose hardware Gemini reads per run (cached per listing)
    interval_min: int = 10  # Bazoš scan interval ...
    jitter_s: int = 90  # ... plus a random 0-90 s
    quiet_start: str = "00:30"  # quiet hours: scan every quiet_interval_min, alerts wait for a morning digest
    quiet_end: str = "06:30"
    quiet_interval_min: int = 30
    gone_checks_per_run: int = 5  # "Написал" listings missing from the scan: detail pages checked per run
    gone_check_every_min: int = 60  # each such listing at most once per hour
    health_after_min: int = 30  # no successful scan for this long (2 intervals in quiet hours) -> Telegram
    startup_message: bool = True  # "🤖 Бот запущен" with the code version after every (re)start

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def _hh_mm(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{value!r}: use HH:MM")
        return value


class LearnConfig(BaseModel):
    """Self-learning price list (scanner.learn): observations, probable sales, the weekly report."""

    window_days: int = 30  # observations seen in the last 30 days count
    min_observations: int = 5  # an item needs this many listings to get a proposal
    min_sold: int = 3  # with this many probable sales their median is the target ...
    asking_to_sold_pct: int = 95  # ... otherwise median asking price x 95 %
    max_step_pct: int = 8  # at most +-8 % per report
    min_change_pct: int = 2  # smaller differences: no proposal
    sold_checks_per_run: int = 3  # detail pages of listings that left the search results, per run
    unseen_days: int = 1  # not in the results for a day -> check whether it was deleted (410)
    recheck_days: int = 3  # still online: look again after 3 days
    give_up_days: int = 30  # stop checking listings first seen longer ago
    new_model_min_listings: int = 3  # models missing from the price list, suggested from this many listings
    report_day: str = "sun"  # weekly report, Prague time
    report_time: str = "19:00"

    @field_validator("report_time")
    @classmethod
    def _hh_mm(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{value!r}: use HH:MM")
        return value


class AppConfig(BaseModel):
    bazos: BazosConfig = BazosConfig()
    pricing: PricingConfig = PricingConfig()
    gemini: GeminiConfig = GeminiConfig()
    notify: NotifyConfig = NotifyConfig()
    learn: LearnConfig = LearnConfig()


def load_config(path: Path) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f) or {})
