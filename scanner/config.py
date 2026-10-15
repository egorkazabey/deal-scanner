"""Settings from .env (secrets) and config.yaml (searches and source options)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None
    gemini_api_key: str | None = None
    gemini_model: str | None = None

    db_path: Path = PROJECT_ROOT / "data" / "scanner.db"
    config_path: Path = PROJECT_ROOT / "config.yaml"
    log_level: str = "INFO"


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


class AppConfig(BaseModel):
    bazos: BazosConfig = BazosConfig()


def load_config(path: Path) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f) or {})
