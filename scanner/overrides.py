"""Settings changed from Telegram survive a redeploy.

The bot edits config.yaml / prices.json inside the container, and a redeploy brings back the repo's
versions. So every edit goes through this module: it changes the file (scanner.config_edit) and
records the change in the `overrides` table (on the persistent volume) together with the value it
replaced. On start `replay()` applies the records again. When the repo's current value is no longer
the one the edit replaced, someone changed it in the repo: the repo wins and the record is dropped.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from scanner import config_edit
from scanner.config import Settings, load_config
from scanner.db import Database

log = logging.getLogger(__name__)


def item_price(path: Path, label: str) -> tuple[int, int] | None:
    """(index, market_price) of the price list item with this exact "model variant" label."""
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    for i, raw in enumerate(items):
        if f"{raw['model']} {raw.get('variant') or ''}".strip() == label:
            return i, int(raw["market_price"])
    return None


# --- edits ------------------------------------------------------------------------------------


def set_radius(settings: Settings, db: Database, km: int) -> None:
    old = load_config(settings.config_path).bazos.radius_km
    config_edit.set_radius(settings.config_path, km)
    db.set_override("radius", "", old, km)


def set_tier_pct(settings: Settings, db: Database, tier: int, pct: int) -> None:
    tiers = load_config(settings.config_path).pricing.max_price_tiers
    old = tiers[tier - 1].pct if 1 <= tier <= len(tiers) else None
    config_edit.set_tier_pct(settings.config_path, tier, pct)
    db.set_override("tier", str(tier), old, pct)


def add_search(settings: Settings, db: Database, section: str, query: str, lo: int | None, hi: int | None) -> None:
    config_edit.add_search(settings.config_path, section, query, lo, hi)
    db.set_override("search", query, None, {"add": True, "section": section, "min": lo, "max": hi})


def remove_search(settings: Settings, db: Database, name: str) -> str:
    """`name` may be a list number; returns the removed search's name."""
    searches = load_config(settings.config_path).bazos.searches
    if name.isdigit() and 1 <= int(name) <= len(searches) and not any(s.name == name for s in searches):
        name = searches[int(name) - 1].name
    config_edit.remove_search(settings.config_path, name)
    db.set_override("search", name, None, {"add": False})
    return name


def set_market_price(settings: Settings, db: Database, index: int, price: int) -> tuple[str, int]:
    prices_path = load_config(settings.config_path).pricing.prices_path
    label, old = config_edit.set_market_price(prices_path, index, price)
    db.set_override("price", label, old, price)
    db.log_price_change(label, old, price)
    return label, old


# --- replay on start --------------------------------------------------------------------------


def replay(settings: Settings, db: Database) -> list[str]:
    """Apply recorded edits again; returns human-readable notes (Russian) of what happened."""
    notes = []
    for row in db.overrides():
        kind, key = row["kind"], row["key"]
        base, value = json.loads(row["base"]), json.loads(row["value"])
        try:
            note = _replay_one(settings, kind, key, base, value)
        except (config_edit.ConfigEditError, IndexError, KeyError, ValueError) as exc:
            note = f"не применено ({kind} {key}): {exc}"
            db.delete_override(kind, key)
        if note is None:  # the repo already has it, or the repo changed that value: nothing to keep
            db.delete_override(kind, key)
        elif note:
            notes.append(note)
    if notes:
        log.info("overrides replayed: %s", "; ".join(notes))
    return notes


def _replay_one(settings: Settings, kind: str, key: str, base, value) -> str | None:
    """'' = still applied (nothing to say), None = drop the record, text = applied now."""
    cfg = load_config(settings.config_path)
    if kind == "radius":
        current = cfg.bazos.radius_km
        if current == value:
            return ""
        if current != base:
            return None
        config_edit.set_radius(settings.config_path, value)
        return f"радиус {value} км"
    if kind == "tier":
        tier = int(key)
        current = cfg.pricing.max_price_tiers[tier - 1].pct
        if current == value:
            return ""
        if current != base:
            return None
        config_edit.set_tier_pct(settings.config_path, tier, value)
        return f"ступень {tier} → {value} %"
    if kind == "search":
        present = any(s.name == key for s in cfg.bazos.searches)
        if value["add"] and not present:
            config_edit.add_search(settings.config_path, value["section"], key, value["min"], value["max"])
            return f"поиск «{key}» добавлен"
        if not value["add"] and present:
            config_edit.remove_search(settings.config_path, key)
            return f"поиск «{key}» удалён"
        return ""
    if kind == "price":
        hit = item_price(cfg.pricing.prices_path, key)
        if hit is None:
            return None
        index, current = hit
        if current == value:
            return ""
        if current != base:
            return None
        config_edit.set_market_price(cfg.pricing.prices_path, index, value)
        return f"{key}: {value} Kč"
    return None
