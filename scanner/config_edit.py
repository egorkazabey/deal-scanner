"""Edits of config.yaml and prices.json from bot commands.

config.yaml is edited line by line, so its comments and alignment stay; every result is validated
with `AppConfig` before it is written. prices.json keeps its format (indent 1, UTF-8).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from scanner.config import AppConfig
from scanner.pricing import normalize


class ConfigEditError(ValueError):
    pass


def _write_validated(path: Path, text: str) -> AppConfig:
    try:
        cfg = AppConfig.model_validate(yaml.safe_load(text) or {})
    except Exception as exc:  # noqa: BLE001 - pydantic / yaml errors go back to the user
        raise ConfigEditError(f"config.yaml не прошёл проверку: {exc}") from exc
    path.write_text(text, encoding="utf-8")
    return cfg


def set_radius(path: Path, km: int) -> AppConfig:
    if not 1 <= km <= 500:
        raise ConfigEditError("радиус: от 1 до 500 км")
    text, n = re.subn(r"^(\s*radius_km:\s*)\d+", rf"\g<1>{km}", path.read_text(encoding="utf-8"),
                      count=1, flags=re.M)
    if not n:
        raise ConfigEditError("в config.yaml нет bazos.radius_km")
    return _write_validated(path, text)


_TIER_RE = re.compile(r"^(\s*- \{(?:up_to:\s*\d+,\s*)?pct:\s*)(\d+)", re.M)


def set_tier_pct(path: Path, tier: int, pct: int) -> AppConfig:
    """Tier 1..N of pricing.max_price_tiers gets a new pct."""
    if not 30 <= pct <= 95:
        raise ConfigEditError("процент: от 30 до 95")
    text = path.read_text(encoding="utf-8")
    start = text.find("max_price_tiers:")
    if start < 0:
        raise ConfigEditError("в config.yaml нет pricing.max_price_tiers")
    matches = list(_TIER_RE.finditer(text, start))
    tiers = AppConfig.model_validate(yaml.safe_load(text) or {}).pricing.max_price_tiers
    if not 1 <= tier <= len(tiers) or len(matches) < len(tiers):
        raise ConfigEditError(f"ступень: от 1 до {len(tiers)}")
    m = matches[tier - 1]
    text = text[: m.start(2)] + str(pct) + text[m.end(2):]
    return _write_validated(path, text)


def _search_lines(text: str) -> tuple[list[str], list[int], dict[int, dict]]:
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(r"^\s*searches:\s*$", l)), None)
    if start is None:
        raise ConfigEditError("в config.yaml нет bazos.searches")
    idx, parsed = [], {}
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("- {"):
            idx.append(i)
            parsed[i] = yaml.safe_load(stripped[2:])
        elif stripped and not stripped.startswith("#"):
            break
    return lines, idx, parsed


def add_search(path: Path, section: str, query: str, price_min: int | None, price_max: int | None) -> AppConfig:
    text = path.read_text(encoding="utf-8")
    lines, idx, parsed = _search_lines(text)
    if any(p.get("name") == query for p in parsed.values()):
        raise ConfigEditError(f"поиск «{query}» уже есть")
    fields = [f"name: {json.dumps(query, ensure_ascii=False)}", f"section: {section}",
              f"query: {json.dumps(query, ensure_ascii=False)}"]
    if price_min is not None:
        fields.append(f"price_min: {price_min}")
    if price_max is not None:
        fields.append(f"price_max: {price_max}")
    indent = re.match(r"^\s*", lines[idx[-1]]).group(0) if idx else "    "
    # After the last search of the same section, else at the end of the list.
    same = [i for i in idx if parsed[i].get("section") == section]
    at = (same or idx)[-1] + 1 if idx else len(lines)
    lines.insert(at, f"{indent}- {{{', '.join(fields)}}}")
    return _write_validated(path, "\n".join(lines))


def remove_search(path: Path, name: str) -> AppConfig:
    text = path.read_text(encoding="utf-8")
    lines, idx, parsed = _search_lines(text)
    hit = [i for i in idx if parsed[i].get("name") == name]
    if not hit and name.isdigit() and 1 <= int(name) <= len(idx):
        hit = [idx[int(name) - 1]]
    if not hit:
        raise ConfigEditError(f"поиска «{name}» нет (/search list)")
    del lines[hit[0]]
    return _write_validated(path, "\n".join(lines))


# --- prices.json -----------------------------------------------------------------------------


@dataclass
class PriceHit:
    index: int  # position in prices.json "items"
    label: str
    market_price: int


def find_price_items(path: Path, query: str) -> list[PriceHit]:
    """Items whose "model variant" contains every word of the query (case and diacritics ignored)."""
    words = normalize(query).split()
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    hits = []
    for i, raw in enumerate(items):
        label = f"{raw['model']} {raw.get('variant') or ''}".strip()
        hay = normalize(label)
        if words and all(w in hay for w in words):
            hits.append(PriceHit(i, label, int(raw["market_price"])))
    # An exact label match wins over longer ones ("iPhone 15" vs "iPhone 15 Pro").
    exact = [h for h in hits if normalize(h.label) == " ".join(words)]
    return exact or hits


def set_market_price(path: Path, index: int, price: int) -> tuple[str, int]:
    """Returns (label, old price). Only market_price changes; the file keeps its format."""
    if not 100 <= price <= 500_000:
        raise ConfigEditError("цена: от 100 до 500 000 Kč")
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data["items"][index]
    old = int(raw["market_price"])
    raw["market_price"] = price
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return f"{raw['model']} {raw.get('variant') or ''}".strip(), old
