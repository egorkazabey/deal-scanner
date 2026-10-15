"""Rule-based deal evaluation (no LLM).

1. `normalize()` a title (case, diacritics, "16pro" -> "16 pro", units, synonyms).
2. `identify()` it: category + canonical model + attributes (storage, RAM/SSD, size, ...).
3. `PriceList.match()` picks the price list item; missing memory -> cheapest variant, confidence "low".
4. `evaluate()` computes market / target buy / discount / profit, flags (stop words, accessory,
   too_cheap, scam_suspect, shop) and a 0-100 score. `evaluate_many()` also catches duplicate batches.

Price list items are parsed with the same `identify()` as listing titles, so a new item in
data/prices.json is matched automatically as long as its model is one the regexes below know.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from scanner.config import PricingConfig
from scanner.models import Listing

# --- normalization ---------------------------------------------------------------------------

_CYRILLIC = [
    (r"гб", " gb "),
    (r"тб", " tb "),
    (r"мм", " mm "),
    (r"без\s+anc", " noanc "),
    (r"нерж\w*\.?\s*(?:сталь)?|сталь", " steel "),
    (r"алюмин\w*", " alu "),
    (r"геймпад|контроллер", " controller "),
]

# Words that are glued to a following number in titles ("iphone16", "rtx3060", "se2").
_GLUED_PREFIXES = (
    "iphone|ipad|airpods|watch|series|ultra|se|switch|xbox|rtx|gtx|rx|pro|max|air|mini|plus|s"
)

_SYNONYMS: list[tuple[str, str]] = [
    (r"\bi ?phone\b|\bifon\w*|\bajfo?u?n\w*", "iphone"),
    (r"\bpromax\b", "pro max"),
    (r"\bplay ?station\b", "ps"),
    (r"\bps ?([45])\b", r"ps\1"),
    (r"\bair ?pods\b", "airpods"),
    (r"\bmac ?book\b", "macbook"),
    (r"\bapple ?watch\b|\biwatch\b|\bhodinky apple\b", "apple watch"),
    (r"\bx ?box\b", "xbox"),
    (r"\bjoy ?-?cons?\b", "joycon"),
    (r"\b(\d{1,2}) ?(?:\.|st|nd|rd|th)? ?(?:gen|generace|generaci|generation)\b\.?", r"\1 gen"),
    (r"\b(?:digital(?:ni)?(?: edition)?|digi|bez (?:mechaniky|mechanikou|disku|jednotky))\b", "digital"),
    (r"\b(?:s mechanikou|s diskem|disc(?: edition)?|blu ?-?ray)\b", "disc"),
    (r"\b(\d+) ?tera\w*", r"\1 tb"),
    (r"\b(?:ocel\w*|nerez\w*|stainless|steel)\b", "steel"),
    (r"\b(?:hlinik\w*|alumin\w*|alu)\b", "alu"),
    (r"\bbez (?:anc|potlaceni hluku)\b", "noanc"),
    (r"\b(?:anc|s (?:aktivnim )?potlacenim hluku|aktivni potlaceni hluku|noise cancel\w*)\b", "anc"),
    (r"\b(?:usb ?-? ?c|type ?-? ?c)\b", "usbc"),
    (r"\b(?:ovladac\w*|gamepad\w*|controller\w*|joystick\w*)\b", "controller"),
]
_SYNONYMS_RE = [(re.compile(p), r) for p, r in _SYNONYMS]


def normalize(text: str) -> str:
    """Lowercase ASCII-ish form used by all matching rules."""
    t = text.lower()
    for pattern, repl in _CYRILLIC:
        t = re.sub(pattern, repl, t)
    t = re.sub(r"(\d)\s*(?:[\"″”“]|''|′′|palc\w*|inch\w*)", r"\1 inch ", t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)
    t = re.sub(r"[–—]", "-", t)
    t = re.sub(r"[^a-z0-9а-я.%/+\- ]", " ", t)
    t = re.sub(r"(\d)([a-zа-я])", r"\1 \2", t)
    t = re.sub(rf"\b({_GLUED_PREFIXES})(\d)", r"\1 \2", t)
    for pattern, repl in _SYNONYMS_RE:
        t = pattern.sub(repl, t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)  # keep decimal points only ("12.9")
    return re.sub(r"\s+", " ", t).strip()


# --- identification --------------------------------------------------------------------------

# Attributes whose absence in a title means "cheapest variant, low confidence".
MEMORY_ATTRS = {"storage", "ram", "ssd", "vram"}
# Numeric attributes where a bigger listing value than any price list variant is matched to the
# biggest known variant (conservative: market price is underestimated, never overestimated).
ORDERED_ATTRS = {"storage", "ram", "ssd", "size"}


@dataclass
class Identity:
    category: str
    model: str
    attrs: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)  # model-level guesses ("PS4 -> Slim")
    pos: int = 0  # where the model is mentioned in the normalized text


_SIZE_RE = re.compile(r"\b(38|40|41|42|44|45|46|49)\b(?= ?(?:/ ?\d{2} ?)*mm\b)")
_MEM_RE = re.compile(r"\b(\d{1,4}) ?(gb|g|tb)\b")
_YEAR_RE = re.compile(r"\b(20[12]\d)\b")
_CONTROLLER_RE = re.compile(r"\b(?:controller|dualsense|dualshock|joycon)\b")
_CONSOLE_SIGNS_RE = re.compile(
    r"\bkonzol\w*|\bconsole\b|\+|\b(?:s|a|vcetne|\d x?|dva|dve) controller|\d ?(?:gb|tb)\b"
    r"|\bslim\b|\bdigital\b|\bdisc\b|\boled\b|\blite\b"
)


def _memory(text: str, allowed: set[int], bare: bool = False) -> int | None:
    """First storage-like value in GB ('256 gb', '1 tb'; bare '256' if `bare`)."""
    for m in _MEM_RE.finditer(text):
        value = int(m.group(1))
        if m.group(2) == "tb":
            value *= 1024
        if value in allowed:
            return value
    if bare:
        m = re.search(r"\b(64|128|256|512)\b(?! ?(?:%|kc|mm|inch))", text)
        if m:
            return int(m.group(1))
    return None


def _year(text: str, mapping: dict[int, int]) -> int | None:
    for m in _YEAR_RE.finditer(text):
        if int(m.group(1)) in mapping:
            return mapping[int(m.group(1))]
    return None


def _controller_only(t: str) -> bool:
    return bool(_CONTROLLER_RE.search(t)) and not _CONSOLE_SIGNS_RE.search(t)


_PHONE_STORAGE = {64, 128, 256, 512, 1024, 2048}


def _iphone(t: str, item: bool) -> Identity | None:
    m = re.search(
        r"\biphone (?:(\d{1,2})(?: (e)(?![\w-]))?|(air))(?: (pro max|pro|plus|mini))?\b", t
    )
    if not m:
        return None
    if m.group(3):
        model = "iphone air"
    else:
        n = int(m.group(1))
        if not 11 <= n <= 18:
            return None
        model = f"iphone {n}{'e' if m.group(2) else ''}" + (f" {m.group(4)}" if m.group(4) else "")
    storage = _memory(t, _PHONE_STORAGE) or _memory(t[m.end():], _PHONE_STORAGE, bare=True)
    return Identity("iphone", model, {"storage": storage}, pos=m.start())


def _ipad(t: str, item: bool) -> Identity | None:
    m = re.search(r"\bipad(?: (pro|air|mini))?\b", t)
    if not m:
        return None
    rest = t[m.start():]
    line = m.group(1)
    attrs: dict[str, Any] = {"storage": _memory(rest, _PHONE_STORAGE | {32})}
    screen = re.search(r"\b(11|12\.9|13)\b(?! ?(?:gb|%|gen))", rest)
    if line == "pro":
        chip = re.search(r"\bm([1245])\b", rest)
        gen = int(chip.group(1)) if chip else _year(rest, {2021: 1, 2022: 2, 2024: 4, 2025: 5})
        model = f"ipad pro m{gen}" if gen else "ipad pro"
        attrs["screen"] = screen.group(1) if screen else None
    elif line == "air":
        g = re.search(r"\bair ([1-7])\b(?! inch)|\b([1-7]) gen\b", rest)
        chip = re.search(r"\bm([123])\b", rest)
        if g:
            gen = int(g.group(1) or g.group(2))
        elif chip:
            gen = int(chip.group(1)) + 4
        else:
            gen = _year(rest, {2020: 4, 2022: 5, 2024: 6, 2025: 7})
        model = f"ipad air {gen}" if gen else "ipad air"
        attrs["screen"] = screen.group(1) if screen else None
    elif line == "mini":
        model = "ipad mini"
    else:
        g = re.search(r"\bipad (\d{1,2})(?![.\d])|\b(\d{1,2}) gen\b", rest)
        if g:
            gen = int(g.group(1) or g.group(2))
        elif re.search(r"\ba ?16\b", rest):
            gen = 11
        else:
            gen = _year(rest, {2017: 5, 2018: 6, 2019: 7, 2020: 8, 2021: 9, 2022: 10, 2025: 11})
        model = f"ipad {gen}" if gen and 5 <= gen <= 11 else "ipad"
    return Identity("ipad", model, attrs, pos=m.start())


_RAM_SIZES = {8, 16, 18, 24, 32, 36, 48, 64, 96, 128}


def _ssd_gb(value: int, unit: str | None) -> int:
    return value * 1024 if unit == "tb" or value <= 8 else value


def _macbook_memory(t: str, item: bool) -> tuple[Any, Any]:
    if item:
        m = re.search(r"(\d+)(?: ?- ?(\d+))? ?/ ?(\d+)(?: ?(tb))?(?: ?- ?(\d+))?", t)
        if not m:
            return None, None
        ram = (int(m.group(1)), int(m.group(2))) if m.group(2) else int(m.group(1))
        ssd_lo = _ssd_gb(int(m.group(3)), m.group(4))
        ssd = (ssd_lo, _ssd_gb(int(m.group(5)), m.group(4))) if m.group(5) else ssd_lo
        return ram, ssd
    m = re.search(r"\b(\d{1,3}) ?(?:gb)? ?/ ?(\d{1,4}) ?(gb|tb)?\b", t)
    if m and int(m.group(1)) in _RAM_SIZES:
        ssd = _ssd_gb(int(m.group(2)), m.group(3))
        if ssd >= 128:
            return int(m.group(1)), ssd
    ram = ssd = None
    m = re.search(r"\b(\d{1,3}) ?gb (?:ram|ramk\w*|pamet\w*|unified|operacni)|\bram (\d{1,3})\b", t)
    if m:
        ram = int(m.group(1) or m.group(2))
    m = re.search(r"\b(\d{3,4}) ?gb (?:ssd|disk|uloziste)|\bssd (\d{3,4})\b|\b([1248]) ?tb\b", t)
    if m:
        ssd = int(m.group(1) or m.group(2)) if not m.group(3) else int(m.group(3)) * 1024
    for mm in _MEM_RE.finditer(t):
        value = int(mm.group(1))
        if mm.group(2) == "tb":
            ssd = ssd or value * 1024
        elif ram is None and value in _RAM_SIZES and value <= 64:
            ram = value
        elif ssd is None and value >= 128:
            ssd = value
    return ram, ssd


def _macbook(t: str, item: bool) -> Identity | None:
    m = re.search(r"\bmacbook(?: (air|pro))?\b", t)
    if not m:
        return None
    rest = t[m.start():]
    chip = re.search(r"\bm([1-5])(?: (pro|max|ultra))?\b", rest)
    if not m.group(1) or not chip:
        return Identity("macbook", "macbook", pos=m.start())  # Intel / unknown line: no price item
    model = f"macbook {m.group(1)} m{chip.group(1)}" + (f" {chip.group(2)}" if chip.group(2) else "")
    screen = re.search(
        r"\b(13|14|15|16)(?:\.\d)?(?: inch)?\b(?! ?(?:gb|tb|g\b|/|%|gen|core|jad))", rest
    )
    ram, ssd = _macbook_memory(rest, item)
    attrs = {"screen": int(screen.group(1)) if screen else None, "ram": ram, "ssd": ssd}
    return Identity("macbook", model, attrs, pos=m.start())


def _watch(t: str, item: bool) -> Identity | None:
    m = re.search(r"\bapple watch\b|\bwatch (?=series|se\b|ultra)", t)
    if not m:
        return None
    rest = t[m.start():]
    sizes = [int(s) for s in _SIZE_RE.findall(rest)]
    if "steel" in rest.split():
        material = "steel"
    elif re.search(r"\btitan\w*", rest):
        material = "titanium"
    elif "alu" in rest.split() or not item:
        material = "alu"  # listings rarely say "aluminium"; it is the default case
    else:
        material = None
    attrs: dict[str, Any] = {
        "size": (frozenset(sizes) if item else sizes[0]) if sizes else None,
        "material": material,
    }
    if u := re.search(r"\bultra(?: ([1-3])\b)?", rest):
        gen = int(u.group(1)) if u.group(1) else _year(rest, {2022: 1, 2023: 2, 2024: 2, 2025: 3})
        return Identity("apple_watch", "watch ultra", attrs | {"gen": gen}, pos=m.start())
    s = re.search(r"\b(?:series|s) (\d{1,2})\b(?! ?(?:mm|gb|%))|\bwatch (\d{1,2})\b(?! ?(?:mm|gb|%))", rest)
    if s:
        n = int(s.group(1) or s.group(2))
        if 3 <= n <= 12:
            return Identity("apple_watch", f"watch series {n}", attrs, pos=m.start())
    if se := re.search(r"\bwatch se\b(?: ([1-3])\b)?|\bse (?:([1-3]) gen|([1-3])\b)", rest):
        gen = se.group(1) or se.group(2) or se.group(3)
        if not gen and (g := re.search(r"\b([1-3]) gen\b", rest)):
            gen = g.group(1)
        gen = int(gen) if gen else _year(rest, {2020: 1, 2022: 2, 2025: 3})
        return Identity("apple_watch", "watch se", attrs | {"gen": gen}, pos=m.start())
    return Identity("apple_watch", "watch", attrs, pos=m.start())


_AIRPODS_PART_RE = re.compile(
    r"\b(?:leve|prave|levy|pravy|jedno|nahradni|samotne) (?:sluchatko|airpods|pouzdro)"
    r"|\bsluchatko (?:leve|prave|levy|pravy)\b|\b(?:nabijeci|samotne|nahradni|jen|pouze) pouzdro\b"
)


def _airpods(t: str, item: bool) -> Identity | None:
    m = re.search(r"\bairpods\b", t)
    if not m:
        return None
    rest = t[m.start():]
    if _AIRPODS_PART_RE.search(t):
        return Identity("airpods", "airpods part", pos=m.start())
    gen_word = re.search(r"\b([1-5]) gen\b", rest)
    if mx := re.match(r"airpods max(?: ([12])\b)?", rest):
        gen = int(mx.group(1)) if mx.group(1) else 1
        if "usbc" in rest.split() or re.search(r"\b2024\b", rest):
            connector = "usbc"
        elif "lightning" in rest.split():
            connector = "lightning"
        else:
            connector = None
        attrs = {"gen": gen, "connector": connector if gen == 1 else None}
        return Identity("airpods", "airpods max", attrs, pos=m.start())
    if pr := re.match(r"airpods pro(?: ([1-3])\b)?", rest):
        gen = pr.group(1) or (gen_word and gen_word.group(1))
        if not gen and "usbc" in rest.split():
            gen = 2
        gen = int(gen) if gen else _year(rest, {2019: 1, 2022: 2, 2023: 2, 2025: 3})
        return Identity("airpods", "airpods pro", {"gen": gen}, pos=m.start())
    b = re.match(r"airpods(?: ([1-5])\b)?", rest)
    gen = b.group(1) or (gen_word and gen_word.group(1))
    gen = int(gen) if gen else _year(rest, {2019: 2, 2021: 3, 2024: 4})
    anc = None
    if gen == 4:
        words = rest.split()
        anc = False if "noanc" in words else True if "anc" in words else None
    return Identity("airpods", "airpods", {"gen": gen, "anc": anc}, pos=m.start())


def _playstation(t: str, item: bool) -> Identity | None:
    words = t.split()
    if re.search(r"\b(?:ps|ps5|ps portal|playstation) portal\b|\bps portal\b", t):
        return Identity("playstation", "ps portal", pos=t.find("portal"))
    console = re.search(r"\bps([45])\b", t)
    if "dualsense" in words or (console and _controller_only(t)):
        pos = min(p for p in (t.find("dualsense"), t.find("controller")) if p >= 0)
        model = "dualsense" if "dualsense" in words or console.group(1) == "5" else "dualshock"
        return Identity("playstation", model, pos=pos)
    if not console:
        return None
    n = console.group(1)
    tail = t[console.start():]
    if re.match(r"ps[45] pro\b", tail):
        return Identity("playstation", f"ps{n} pro", pos=console.start())
    slim = "slim" in words or bool(re.search(r"\bcfi ?-? ?2\d{3}", t))
    if n == "4":
        ident = Identity("playstation", "ps4 slim", pos=console.start())
        if not slim:
            ident.assumptions.append("PS4 без Slim/Pro, принят Slim")
        return ident
    model = "ps5" + (" slim" if slim else "") + (" digital" if "digital" in words else "")
    return Identity("playstation", model, pos=console.start())


_XBOX_STORAGE = {500, 512, 1024, 2048}


def _xbox(t: str, item: bool) -> Identity | None:
    m = re.search(r"\bxbox\b", t)
    if not m:
        return None
    if _controller_only(t):
        return Identity("xbox", "xbox controller", pos=m.start())
    rest = t[m.start():]
    variants = [
        (r"\bseries ?x\b|\bxsx\b", "xbox series x"),
        (r"\bseries ?s\b|\bxss\b", "xbox series s"),
        (r"\bone ?x\b", "xbox one x"),
        (r"\bone ?s\b", "xbox one s"),
        (r"\bone\b", "xbox one"),
        (r"\b360\b", "xbox 360"),
    ]
    found = [(mm.start(), i, model) for i, (p, model) in enumerate(variants) if (mm := re.search(p, rest))]
    if not found:
        return Identity("xbox", "xbox", pos=m.start())
    model = min(found)[2]  # earliest mention; on a tie the more specific pattern ("one s" > "one")
    return Identity("xbox", model, {"storage": _memory(rest, _XBOX_STORAGE)}, pos=m.start())


def _nintendo(t: str, item: bool) -> Identity | None:
    m = re.search(r"\b(?:nintendo|switch)\b", t)
    if not m or not re.search(r"\bnintendo\b|\bjoycon\b|\bswitch (?:oled|lite|2)\b", t):
        return None
    if _controller_only(t):
        return Identity("nintendo", "switch controller", pos=m.start())
    if not re.search(r"\bswitch\b", t):
        return None
    if re.search(r"\bswitch 2\b(?! ?(?:x\b|ks|hry|her\b|hra|controller|joycon|gen))", t):
        model = "switch 2"
    elif re.search(r"\boled\b", t):
        model = "switch oled"
    elif re.search(r"\blite\b", t):
        model = "switch lite"
    else:
        if re.search(r"\bv ?2\b|\b201[89]\b|cervena krabice|hac ?-?001 ?-?01", t):
            rev = "v2"
        elif re.search(r"\bv ?1\b|\b2017\b|hac ?-?001\b", t):
            rev = "v1"
        else:
            rev = None
        return Identity("nintendo", "switch", {"rev": rev}, pos=m.start())
    return Identity("nintendo", model, pos=m.start())


_PC_RE = re.compile(
    r"\b(?:pc|pocitac\w*|sestav\w*|notebook\w*|laptop\w*|desktop|skrin\w*|herni (?:pocitac|stroj))\b"
)
_GPU_WORD_RE = re.compile(r"\bgraficka\b|\bgpu\b|\bkarta\b|\bkartu\b")
_GPU_CONTEXT_RE = re.compile(
    r"\b(?:geforce|nvidia|radeon|amd|graficka|gpu|evga|msi|asus|gigabyte|zotac|palit|gainward"
    r"|inno ?3|pny|kfa|galax|rog|strix|tuf|sapphire|xfx|powercolor|asrock)\b|\b\d{1,2} ?(?:gb|g)\b"
)
_VRAM = {2, 3, 4, 6, 8, 10, 11, 12, 16, 20, 24, 32}


def _gpu(t: str, item: bool) -> Identity | None:
    if _PC_RE.search(t) and not _GPU_WORD_RE.search(t):
        return None  # a whole PC / laptop with a GPU, not a graphics card
    vram = _memory(t, _VRAM)
    nv = re.search(r"\b(?:(rtx|gtx) )?([1-5]0[5-9]0)((?: (?:ti|super))*)\b", t)
    if nv and (nv.group(1) or _GPU_CONTEXT_RE.search(t)):
        model = f"rtx {nv.group(2)}{nv.group(3)}"
        if nv.group(2).startswith("10"):
            model = "g" + model[1:]
        return Identity("gpu", model, {"vram": vram}, pos=nv.start())
    amd = re.search(r"\b(?:rx|radeon(?: rx)?) (\d{3,4})(?: (xtx|xt|gre))?\b", t)
    if amd:
        model = f"rx {amd.group(1)}" + (f" {amd.group(2)}" if amd.group(2) else "")
        return Identity("gpu", model, {"vram": vram}, pos=amd.start())
    return None


_IDENTIFIERS = (_iphone, _ipad, _macbook, _watch, _airpods, _playstation, _xbox, _nintendo, _gpu)


def identify(text: str, *, item: bool = False) -> Identity | None:
    """Identify a normalized title. The earliest mentioned device wins ("MacBook + iPhone")."""
    found = [ident for f in _IDENTIFIERS if (ident := f(text, item))]
    return min(found, key=lambda i: i.pos) if found else None


# --- price list ------------------------------------------------------------------------------

# Price list model names the generic parser can't read (alternatives, ranges, Russian words).
_ITEM_ALIASES: dict[str, list[str]] = {
    "Apple Watch Series 8/9": ["Apple Watch Series 8", "Apple Watch Series 9"],
    "MacBook Pro 14–16″ M1 Max": ["MacBook Pro 14″ M1 Max", "MacBook Pro 16″ M1 Max"],
    "RX 6600 / 6600 XT": ["RX 6600", "RX 6600 XT"],
    'iPad Pro 11" M1/M2': ['iPad Pro 11" M1', 'iPad Pro 11" M2'],
    "Одно ухо / кейс отдельно": ["AirPods jedno sluchátko"],
    "DualSense (контроллер)": ["DualSense"],
    "Геймпад Xbox": ["Xbox controller"],
}


@dataclass
class PriceItem:
    category: str
    model: str
    variant: str
    market_price: int
    liquidity: int
    confidence: str  # "checked" | "estimate"
    identities: list[Identity] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.model} {self.variant}".strip()


@dataclass
class Match:
    item: PriceItem
    identity: Identity
    confidence: str  # "high" | "low"
    notes: list[str] = field(default_factory=list)


def _compare(attr: str, want: Any, have: Any) -> str | None:
    """'free' | 'exact' | 'assumed' | 'below' | None (mismatch). `want` is the item's value."""
    if want is None:
        return "free"
    if have is None:
        return "assumed"
    if isinstance(want, tuple):
        lo, hi = want
        values = None
    else:
        values = want if isinstance(want, frozenset) else frozenset([want])
        lo = hi = None
    if values is not None:
        if have in values:
            return "exact"
        if attr in ORDERED_ATTRS:
            lo, hi = min(values), max(values)
    elif lo <= have <= hi:
        return "exact"
    if attr in ORDERED_ATTRS and hi is not None and have > hi:
        return "below"
    return None


def _fmt_attr(attr: str, value: Any) -> str:
    if isinstance(value, tuple):
        return f"{value[0]}–{value[1]}"
    if isinstance(value, frozenset):
        return "/".join(str(v) for v in sorted(value))
    return str(value)


class PriceList:
    def __init__(self, items: list[PriceItem]):
        self.items = items
        self._by_model: dict[tuple[str, str], list[tuple[PriceItem, Identity]]] = {}
        for item in items:
            for ident in item.identities:
                self._by_model.setdefault((ident.category, ident.model), []).append((item, ident))

    @classmethod
    def load(cls, path: Path) -> PriceList:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = []
        for raw in data["items"]:
            item = PriceItem(
                category=raw["category"],
                model=raw["model"],
                variant=raw.get("variant") or "",
                market_price=int(raw["market_price"]),
                liquidity=int(raw.get("liquidity") or 1),
                confidence=raw.get("confidence") or "estimate",
            )
            for name in _ITEM_ALIASES.get(item.model, [item.model]):
                ident = identify(normalize(f"{name} {item.variant}"), item=True)
                if ident and ident.category == item.category:
                    item.identities.append(ident)
            items.append(item)
        return cls(items)

    def unmatched_items(self) -> list[PriceItem]:
        """Price list items the parser can't read (they can never be matched)."""
        return [i for i in self.items if not i.identities]

    def match(self, title: str) -> Match | None:
        ident = identify(normalize(title))
        return self.match_identity(ident) if ident else None

    def match_identity(self, ident: Identity) -> Match | None:
        options = []
        for item, item_ident in self._by_model.get((ident.category, ident.model), []):
            results = {a: _compare(a, v, ident.attrs.get(a)) for a, v in item_ident.attrs.items()}
            if None in results.values():
                continue
            options.append((item, item_ident, results))
        if not options:
            return None

        best_exact = max(sum(r == "exact" for r in res.values()) for _, _, res in options)
        pool = [o for o in options if sum(r == "exact" for r in o[2].values()) == best_exact]
        any_assumed = any("assumed" in res.values() for _, _, res in pool)
        # Unknown attribute -> cheapest variant; listing above all variants -> closest one below.
        pick = min if any_assumed else max
        item, item_ident, results = pick(pool, key=lambda o: o[0].market_price)

        notes = list(ident.assumptions)
        low = bool(ident.assumptions)
        for attr, res in results.items():
            want = item_ident.attrs[attr]
            if res == "assumed":
                ambiguous = len({_fmt_attr(attr, o[1].attrs.get(attr)) for o in pool}) > 1
                if attr in MEMORY_ATTRS or ambiguous:
                    low = True
                    notes.append(f"{attr} не указан → {_fmt_attr(attr, want)}")
            elif res == "below":
                low = True
                notes.append(f"{attr} {ident.attrs[attr]} нет в прайсе → {_fmt_attr(attr, want)}")
        return Match(item, ident, "low" if low else "high", notes)


# --- flags -----------------------------------------------------------------------------------

_ACC_WORDS = (
    r"(?:obal\w*|kryt\w*|sklo|skla|tvrzen\w* sklo|folie|pouzdr\w*|remin\w*|pasek|pasky|nabijec\w*"
    r"|kabel\w*|adapter\w*|krabic\w*|hra|hry|hru|her|drzak\w*|stojan\w*|dock\w*|nahradni dil\w*"
    r"|baterie|displej\w*|lcd|stylus|pencil|klavesnic\w*|keyboard|chladic\w*|ventilator\w*)"
)
_ACC_START_RE = re.compile(
    r"^(?:(?:prodam|prodej|nov\w*|original\w*|apple|luxusni|silikon\w*|kozen\w*|ochrann\w*"
    rf"|magnet\w*|magic|smart|\d+ ?x|sada|set)\s+)*{_ACC_WORDS}\b"
)
_ACC_FOR_RE = re.compile(rf"\b{_ACC_WORDS} (?:na|pro|k|ke|od)\b")

_STOP_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("koupím", re.compile(r"\b(?:koupim|sh?anim|hledam|poptavam|vykupuji|vykup)\b")),
    ("výměna", re.compile(
        r"\bvymen\w* za\b|\bvymenim\b|\bvymenu\b(?! (?:baterie|displeje|skla|krytu))"
        r"|\b(?:mozna|i|nebo) vymena\b")),
    ("na díly", re.compile(r"\bna (?:dily|nahradni dily|nd)\b|\bnefunkc\w*")),
    ("blokovaný", re.compile(
        r"\b(?:za)?blokovan\w*|\bmdm\b|\baktivacni zamek\b|\bactivation lock\b|\bzamcen\w*")),
    ("prodáno", re.compile(r"\bprodan[oaey]\b|\brezerv\w*")),
]
_ICLOUD_RE = re.compile(r"\bicloud\w*")
_ICLOUD_OK_RE = re.compile(r"odhlas|vypnut|odpojen|\bbez\b|cist|free|\bneni\b|\bok\b|odemc")

_SHOP_RE = re.compile(
    r"\bico\b|\bprovozovn\w*|\bodpocet dph\b|\bdph\b|\b(?:12|24) ?m(?:es\w*)? ?zaruk\w*"
    r"|\bzaruk\w* (?:12|24) ?m(?:es\w*)?\b"
)

# Scam phrases: strong ones are enough alone, weak ones only together with price < 55 % of market.
_SCAM_STRONG_RE = re.compile(
    r"\bposl\w* (?:mi )?(?:zpravu |sms |dotaz )?na (?:e-?mail|mail)\b"
    r"|\b(?:jen|pouze|only) (?:pres )?(?:e-?mail|mail|whatsapp)\b"
    r"|\bkontakt\w* (?:jen|pouze) (?:pres )?(?:e-?mail|mail|whatsapp)\b"
)
_SCAM_WEAK_RE = re.compile(
    r"\b(?:pouze|jen) (?:zaslani|postou|poslu)\b|\bplatb\w* predem\b|\bzaplat\w* predem\b"
    r"|\bwhatsapp\b|\bkuryr\w*|\bodkaz na (?:dopravu|doruceni)\b"
)


@dataclass(frozen=True)
class ScamRule:
    reason: str
    models: tuple[str, ...]
    price: tuple[int, int]
    source: str | None = None
    city: str | None = None  # normalized substring of listing.location


# From KNOWLEDGE.md "Шаблоны мошенников".
SCAM_RULES = [
    ScamRule("FB: iPhone 15/16 Pro Max за 6 100–8 700",
             ("iphone 15 pro max", "iphone 16 pro max"), (6100, 8700), source="fb"),
    ScamRule("Jeseník: PS5 Pro за 8–9k", ("ps5 pro",), (7500, 9500), city="jesenik"),
    ScamRule("Jeseník: Switch 2 за ~4 500", ("switch 2",), (4000, 5000), city="jesenik"),
    ScamRule("Jeseník: iPhone 14 Pro за ~7 500", ("iphone 14 pro",), (7000, 8000), city="jesenik"),
    ScamRule("Xbox Series X за 4 500–6 000", ("xbox series x",), (4500, 6000)),
]


def stop_reasons(text: str) -> list[str]:
    reasons = [name for name, rx in _STOP_RULES if rx.search(text)]
    for m in _ICLOUD_RE.finditer(text):
        window = text[max(0, m.start() - 25): m.end() + 25]
        if not _ICLOUD_OK_RE.search(window):
            reasons.append("iCloud")
            break
    return reasons


# Other devices / brands that are never what we buy (wheels, headsets): accessory anywhere in the title.
_FOREIGN_RE = re.compile(r"\b(?:volant\w*|wheel|pedal\w*|hori|logitech|thrustmaster|headset\w*)\b")
# Accessory if mentioned before the device (or with no device): "Sluchátka k iPhone", "Nabíječka iPhone".
_LEADING_ACC_RE = re.compile(r"\bsluchat\w*|\bkamer\w*|\bcamera\b|\bwebcam\w*|\bnabijec\w*")
# Accessory if the title has no sign of a console in it: "Dock Nintendo Switch", "FIFA 24 hra PS5".
# "dockem/docku" (with/in a dock) mean the dock comes with the console, so only bare "dock" counts.
_CONSOLE_ACC_RE = re.compile(r"\bdock\b|\b(?:hra|hry|hru|her)\b|\bkamer\w*|\bcamera\b")
_PRICED_CONTROLLERS = {"dualsense", "xbox controller", "switch controller"}


_EXCHANGE_RE = dict(_STOP_RULES)["výměna"]
_SALE_RE = re.compile(r"\bprod\w*|\bnebo\b")


def exchange_only(title: str) -> bool:
    """A swap offer in the (normalized) title with no sale ("výměna za Android", "vyměním")."""
    return bool(_EXCHANGE_RE.search(title)) and not _SALE_RE.search(title)


def is_accessory(text: str, ident: Identity | None) -> bool:
    if ident and ident.model == "airpods part":  # a charging case / single earbud is a part we price
        return bool(re.search(r"\b(?:obal\w*|kryt\w*|pouzdr\w*) na\b", text))
    if _ACC_START_RE.search(text) or _FOREIGN_RE.search(text):
        return True
    m = _ACC_FOR_RE.search(text)
    if m and (ident is None or m.start() <= ident.pos):
        return True
    m = _LEADING_ACC_RE.search(text)
    if m and (ident is None or (m.start() <= ident.pos and ident.category != "airpods")):
        return True
    if ident is None or ident.category in {"playstation", "xbox", "nintendo"}:
        if _CONSOLE_ACC_RE.search(text) and not _CONSOLE_SIGNS_RE.search(text):
            return True
        # A lone controller is an accessory unless the price list has it ("DualSense" is priced).
        if _controller_only(text) and (ident is None or ident.model not in _PRICED_CONTROLLERS):
            return True
    return False


# --- evaluation ------------------------------------------------------------------------------

MIN_PROFIT = {"macbook": 2000, "gpu": 2000, "pc": 2000}  # everything else: 1000 (KNOWLEDGE.md)


@dataclass
class Evaluation:
    listing: Listing
    match: Match | None = None
    market_price: int | None = None
    target_buy: int | None = None
    discount_pct: float | None = None  # 1 - price/market
    expected_profit: int | None = None  # market - price
    flags: list[str] = field(default_factory=list)
    scam_reasons: list[str] = field(default_factory=list)
    stop: list[str] = field(default_factory=list)
    is_candidate: bool = False
    score: int = 0

    @property
    def too_cheap(self) -> bool:
        return "too_cheap" in self.flags

    @property
    def scam_suspect(self) -> bool:
        return "scam_suspect" in self.flags

    @property
    def match_confidence(self) -> str | None:
        return self.match.confidence if self.match else None


def _city(location: str | None) -> str:
    return normalize(location or "")


def evaluate(
    listing: Listing, prices: PriceList, cfg: PricingConfig, today: date | None = None
) -> Evaluation:
    ev = Evaluation(listing)
    title = normalize(listing.title)
    full = title + (" " + normalize(listing.description) if listing.description else "")
    ident = identify(title)

    ev.stop = stop_reasons(full)
    if "výměna" in ev.stop and not exchange_only(title):
        ev.stop.remove("výměna")  # sale with a possible swap ("prodám nebo výměna", "důvodem je výměna")
        ev.flags.append("возможен обмен")
    if is_accessory(title, ident):
        ev.flags.append("accessory")
    if _SHOP_RE.search(full):
        ev.flags.append("shop")

    today = today or date.today()
    if listing.posted_date and (today - listing.posted_date).days > cfg.stale_days:
        ev.flags.append("stale")

    ev.match = prices.match_identity(ident) if ident else None
    if ev.match is None:
        return ev
    item = ev.match.item
    market = item.market_price + bundle_bonus(title, ev.match, prices, cfg)
    pct = cfg.buy_pct_low_liquidity if item.liquidity <= 1 else cfg.buy_pct
    ev.market_price = market
    ev.target_buy = int(round(market * pct / 100, -2))
    if listing.price is None:
        ev.flags.append("no_price")
        return ev

    price = listing.price
    ev.discount_pct = 1 - price / market
    ev.expected_profit = market - price
    if price < market * cfg.min_price_pct / 100:
        ev.flags.append("too_cheap")

    city = _city(listing.location)
    for rule in SCAM_RULES:
        if (
            ident.model in rule.models
            and rule.price[0] <= price <= rule.price[1]
            and (rule.source is None or rule.source == listing.source)
            and (rule.city is None or rule.city in city)
        ):
            ev.scam_reasons.append(rule.reason)
    if _SCAM_STRONG_RE.search(full):
        ev.scam_reasons.append("контакт только через e-mail/WhatsApp")
    if price < market * 0.55 and (m := _SCAM_WEAK_RE.search(full)):
        ev.scam_reasons.append(f"< 55 % рынка + «{m.group(0)}»")
    if ev.scam_reasons:
        ev.flags.append("scam_suspect")
    if ev.expected_profit < MIN_PROFIT.get(item.category, 1000):
        ev.flags.append("low_profit")

    ev.is_candidate = (
        not ev.stop
        and "accessory" not in ev.flags
        and "low_profit" not in ev.flags
        and not ev.too_cheap
        and not ev.scam_suspect
        and price <= market * cfg.max_price_pct / 100
    )
    ev.score = score(ev, cfg, today) if ev.is_candidate else 0
    return ev


def score(ev: Evaluation, cfg: PricingConfig, today: date | None = None) -> int:
    """0-100: discount 45, liquidity 15, profit 20, distance 10, freshness 10, minus penalties."""
    assert ev.match and ev.discount_pct is not None and ev.expected_profit is not None
    item = ev.match.item
    cap = cfg.discount_cap_pct / 100
    pts = 45 * _clamp((min(ev.discount_pct, cap) - 0.10) / (cap - 0.10))
    pts += 15 * _clamp((item.liquidity - 1) / 2)
    min_profit = MIN_PROFIT.get(item.category, 1000)
    pts += 20 * _clamp(ev.expected_profit / (min_profit * 4))

    city = _city(ev.listing.location)
    if any(normalize(c) == city for c in cfg.home_cities):
        pts += 10
    elif any(normalize(c) == city for c in cfg.near_cities):
        pts += 6

    posted = ev.listing.posted_date
    if posted and not ev.listing.is_top:
        age = ((today or date.today()) - posted).days
        pts += 10 if age <= 0 else 7 if age == 1 else 4 if age <= 3 else 2 if age <= 7 else 0

    if ev.expected_profit < min_profit * 2:
        pts -= 5  # barely worth the trip
    if "stale" in ev.flags:
        pts -= 10
    if "shop" in ev.flags:
        pts -= 15
    if ev.match.confidence == "low":
        pts *= 0.8
    if item.confidence == "estimate":
        pts *= 0.9
    return int(round(max(0, min(100, pts))))


_CONTROLLER_ITEM = {"playstation": "dualsense", "xbox": "xbox controller"}
_NUMBER_WORDS = {"dva": 2, "dve": 2, "tri": 3, "ctyri": 4}


def bundle_bonus(title: str, match: Match, prices: PriceList, cfg: PricingConfig) -> int:
    """Extra controllers in a console bundle ("PS5 + 2 ovladače") add a share of their price.

    A console comes with one controller, so only the ones above it count. Games and other extras have
    no price list item and add nothing.
    """
    ident = match.identity
    controller_model = _CONTROLLER_ITEM.get(ident.category)
    if not controller_model or ident.model in _PRICED_CONTROLLERS or ident.model.endswith("controller"):
        return 0
    m = re.search(r"\b(\d|dva|dve|tri|ctyri) ?x? controller", title)
    if m:
        count = int(m.group(1)) if m.group(1).isdigit() else _NUMBER_WORDS[m.group(1)]
    elif re.search(r"\b(?:druhy|dalsi|navic|extra|2 ?\.) controller|\bcontroller (?:navic|extra)\b", title):
        count = 2
    else:
        return 0
    extra = min(count, 4) - 1
    controller = prices.match_identity(Identity(ident.category, controller_model))
    if extra <= 0 or not controller:
        return 0
    bonus = int(round(extra * controller.item.market_price * cfg.bundle_accessory_pct / 100, -2))
    match.notes.append(f"комплект: +{extra} ovladač × {cfg.bundle_accessory_pct} % = +{bonus} Kč")
    return bonus


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _dup_key(listing: Listing) -> tuple[str, int | None]:
    return normalize(listing.title), listing.price


def evaluate_many(
    listings: list[Listing], prices: PriceList, cfg: PricingConfig, today: date | None = None
) -> list[Evaluation]:
    """Evaluate a batch; 3+ identical listings (same title and price) are a scam pattern."""
    dups = Counter(_dup_key(l) for l in listings)
    result = []
    for listing in listings:
        ev = evaluate(listing, prices, cfg, today)
        if ev.match and dups[_dup_key(listing)] >= 3 and "scam_suspect" not in ev.flags:
            ev.scam_reasons.append(f"{dups[_dup_key(listing)]} одинаковых объявления")
            ev.flags.append("scam_suspect")
            ev.is_candidate, ev.score = False, 0
        result.append(ev)
    return result


def rank(evaluations: list[Evaluation]) -> list[Evaluation]:
    return sorted((e for e in evaluations if e.is_candidate), key=lambda e: e.score, reverse=True)
