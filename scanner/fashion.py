"""Clothes and sneakers (Vinted): price list knowledge/fashion.json, rule-based match, no LLM.

A listing matches an item when its brand (Vinted's brand field or the title) and one of the item's model
words are in it; the more specific item wins (`exclude` words keep "Dunk Low" from taking "SB Dunk").
`market_price` in fashion.json is the used price in "Velmi dobrý" condition and a common size; the match
adjusts it by the listing's Vinted condition (`pricing.fashion.condition_pct`) and lowers the liquidity for
uncommon sizes. Kids' shoe sizes do not match. The result is a synthetic `Match` (category "fashion"), so the
listing goes through the usual `evaluate()`: ceiling (`pricing.max_price_tiers_by_category.fashion`), offer,
score, Gemini.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from scanner.config import FashionRules
from scanner.models import Listing
from scanner.pricing import Identity, Match, PriceItem, normalize

CATEGORY = "fashion"

# Kids' things (other prices): in the title, or a size in years / a height below 158 cm.
_KIDS_RE = re.compile(r"\b(?:detsk\w*|dziec\w*|kids?|junior\w*|chlapeck\w*|divci|dievc\w*|baby)\b")
_KIDS_SIZE_RE = re.compile(r"\b(?:let|lat|rok\w*|years?|mesic\w*|miesi\w*)\b")
# A jacket / hoodie item never matches shoes, trousers, shorts or accessories of the same brand.
_NOT_GARMENT_RE = re.compile(
    r"\b(?:boty|buty|boots?|obuv|tenisk\w*|sneaker\w*|kozak\w*|snehul\w*|kalhot\w*|spodni\w*|pants|jeans"
    r"|dzins\w*|dzin\w*|kratas\w*|sortk\w*|shorts?|spodenk\w*|cepic\w*|czap\w*|beanie|cap|ksiltovk\w*"
    r"|rukavic\w*|rekawicz\w*|batoh|plecak|backpack|task\w*|torb\w*|sala|szalik|ponozk\w*|skarpet\w*)\b"
)


def _words(values: list[str]) -> re.Pattern[str] | None:
    """Normalized phrases as one regex; spaces and hyphens inside a phrase are optional ("xt-6" = "xt 6")."""
    parts = []
    for value in values:
        tokens = re.split(r"[\s\-]+", normalize(value))
        parts.append(r"[\s\-]?".join(re.escape(t) for t in tokens if t))
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b") if parts else None


@dataclass
class FashionItem:
    kind: str  # sneakers / clothing
    brand: str
    model: str
    brands: re.Pattern[str]
    models: re.Pattern[str]
    exclude: re.Pattern[str] | None
    market_price: int
    liquidity: int
    confidence: str

    @property
    def label(self) -> str:
        return f"{self.brand} {self.model}"


class FashionList:
    def __init__(self, items: list[FashionItem]):
        self.items = items

    @classmethod
    def load(cls, path: Path) -> FashionList:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = []
        for raw in data["items"]:
            items.append(FashionItem(
                kind=raw["kind"],
                brand=raw["brand"],
                model=raw["model"],
                brands=_words(raw.get("brands") or [raw["brand"]]),
                models=_words(raw["aliases"]),
                exclude=_words(raw.get("exclude") or []),
                market_price=int(raw["market_price"]),
                liquidity=int(raw.get("liquidity") or 2),
                confidence=raw.get("confidence") or "estimate",
            ))
        return cls(items)

    def find(self, listing: Listing) -> FashionItem | None:
        """The item named in the listing: brand + model word, the longest model word wins."""
        brand = normalize(listing.brand or "")
        title = normalize(listing.title)
        best, best_len = None, 0
        if _KIDS_RE.search(title):
            return None
        for item in self.items:
            if not (item.brands.search(brand) or item.brands.search(title)):
                continue
            if item.exclude and item.exclude.search(title):
                continue
            if item.kind == "clothing" and _NOT_GARMENT_RE.search(title):
                continue
            m = item.models.search(title)
            if m and len(m.group(0)) > best_len:
                best, best_len = item, len(m.group(0))
        return best

    def match(self, listing: Listing, rules: FashionRules) -> Match | None:
        item = self.find(listing)
        if item is None:
            return None
        notes: list[str] = []
        liquidity, low = item.liquidity, False
        size = (listing.size or "").strip()
        if _KIDS_SIZE_RE.search(normalize(size)) or ((cm := re.search(r"(\d{2,3}) ?cm", size)) and int(cm.group(1)) < 158):
            return None  # kids' clothes: other prices
        if item.kind == "sneakers":
            shoe = shoe_size(size)
            if shoe is not None and shoe < rules.min_shoe_size:
                return None  # kids' shoes: other prices
            if shoe is None:
                low = True
                notes.append("размер не указан")
            elif not rules.shoe_sizes[0] <= shoe <= rules.shoe_sizes[1]:
                liquidity -= 1
                notes.append(f"размер {size}: продаётся медленнее")
        elif (letter := re.search(r"\b(\d?X{0,3}[SL]|M)\b", size.upper())) and \
                letter.group(1) not in {s.upper() for s in rules.clothing_sizes}:
            liquidity -= 1
            notes.append(f"размер {size}: продаётся медленнее")
        pct = rules.condition_pct.get(listing.condition or "")
        if pct is None:
            pct, low = 100, True
            notes.append("состояние не указано → как «Velmi dobrý»")
        elif pct != 100:
            notes.append(f"состояние «{listing.condition}»: {pct} % цены")
        market = int(round(item.market_price * pct / 100, -1))
        price_item = PriceItem(CATEGORY, item.label, size, market, max(1, liquidity), item.confidence)
        return Match(price_item, Identity(CATEGORY, normalize(item.label)), "low" if low else "high", notes)


def shoe_size(size: str) -> float | None:
    """EU shoe size from Vinted's size text ("42", "42,5", "EU 42 / UK 8", "42 2/3")."""
    m = re.search(r"\b(\d{2})(?:[.,](\d))?", size)
    if not m:
        return None
    value = float(f"{m.group(1)}.{m.group(2) or 0}")
    return value if 15 <= value <= 52 else None
