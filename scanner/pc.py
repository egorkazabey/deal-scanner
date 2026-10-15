"""Ready-built PCs: specs (extracted by Gemini) → value from knowledge/components.json.

Gemini only reads the listing and fills `PcSpecs`; the price is computed here, by rules:
PC value = sum of used component prices × factor (1.0 / 0.9 / 0.8 by hardware class, see
`pc_factor()`), OEM builds −15 %, a bundled monitor is
added separately at its full price. GPU prices come from prices.json (category gpu). An unknown CPU
model, RAM or SSD size or monitor takes the nearest known one and makes the valuation "low".
Without a CPU or a GPU the PC is not valued ("нет данных о железе").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from scanner.pricing import Identity, Match, PriceItem, PriceList, normalize

NO_HARDWARE = "нет данных о железе"


class Drive(BaseModel):
    kind: Literal["nvme", "sata_ssd", "hdd"]
    size_gb: int = Field(description="Capacity in GB (1 TB = 1000)")


class PcSpecs(BaseModel):
    """Response schema for Gemini: what a PC listing says about its hardware."""

    is_pc: bool = Field(description="A complete desktop PC for sale (not a laptop, single part or accessory)")
    cpu: str = Field(description="CPU model as 'i5-12400F' or 'Ryzen 5 5600X'; '' if not stated")
    gpu: str = Field(description="Graphics card as 'RTX 3060 12GB' or 'RX 6700 XT'; '' if not stated or integrated only")
    ram_gb: int = Field(description="Total RAM in GB; 0 if not stated")
    ram_type: Literal["DDR3", "DDR4", "DDR5", "unknown"]
    ram_modules: int = Field(description="Number of RAM modules (e.g. 2x8GB -> 2); 0 if not stated")
    storage: list[Drive] = Field(description="All drives; empty if not stated")
    psu: str = Field(description="Power supply model as written, e.g. 'Seasonic Focus GX-650'; '' if not stated")
    psu_watts: int = Field(description="0 if not stated")
    psu_quality: Literal["brand", "noname", "unknown"] = Field(
        description="brand = Seasonic, Corsair, be quiet!, EVGA, MSI, Fractal, Cooler Master, Thermaltake, "
                    "Antec, SilverStone, Gigabyte, ASUS, NZXT, Enermax, FSP...; noname = unknown cheap brand; "
                    "unknown = PSU not described"
    )
    motherboard: str = Field(description="Motherboard model/chipset, e.g. 'B550'; '' if not stated")
    monitor_included: bool = Field(description="A monitor is sold together with the PC")
    monitor_size_inch: float = Field(description="0 if no monitor or not stated")
    monitor_resolution: Literal["1080p", "1440p", "4k", "unknown"] = Field(description="unknown if no monitor or not stated")
    monitor_hz: int = Field(description="0 if no monitor or not stated")
    is_oem: bool = Field(description="OEM prebuilt (HP, Dell, Lenovo, Acer, ... incl. HP Omen, Dell Alienware, Lenovo Legion)")
    condition_notes: list[str] = Field(description="Strictly in Russian, short: condition, warranty, extras")
    red_flags: list[str] = Field(description="Strictly in Russian, short: e.g. mining, missing parts, swap only")
    scam_risk: Literal["low", "medium", "high"]


PC_SYSTEM_PROMPT = """\
Ты помогаешь перекупщику б/у электроники в Чехии. Тебе дают объявление о продаже готового ПК
(заголовок и описание). Вытащи из текста железо строго по схеме JSON. Ничего не оценивай и не
выдумывай: если чего-то нет в тексте, оставь пустую строку или 0.
- cpu пиши как «i5-12400F», «i7-8700K», «Ryzen 5 5600X», «Ryzen 7 7800X3D».
- gpu пиши как «RTX 3060 12GB», «GTX 1660 Super», «RX 6700 XT»; встроенная графика = "".
- ram_gb: общий объём; ram_modules: число модулей (2x8GB → 2), если указано.
- storage: все диски; NVMe/M.2 = nvme, обычный SSD (SATA) = sata_ssd, HDD = hdd.
- psu_quality: brand, если производитель блока питания известный; noname, если безымянный или дешёвый;
  unknown, если блок не описан.
- is_pc=false для ноутбука, отдельной комплектующей, аксессуара, «koupím».
- monitor_*: только если монитор продаётся вместе с ПК.
- condition_notes, red_flags строго по-русски, коротко. Майнинг, неполная сборка, «jen výměna» = red_flags.
- scam_risk: high, если контакт только через e-mail/WhatsApp, предоплата, цена в разы ниже рынка.
"""


@dataclass
class Components:
    factors: dict  # high / mid / low + the class thresholds, see pc_factor()
    oem_discount_pct: int
    liquidity: int
    cpu: dict[str, int]
    ram: dict[str, dict[int, int]]
    storage: dict[str, dict[int, int]]
    motherboard: int
    psu: dict[str, int]
    case_cooler: int
    monitors: list[dict]

    @classmethod
    def load(cls, path: Path) -> Components:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        sizes = lambda table: {k: {int(s): p for s, p in v.items()} for k, v in table.items()}  # noqa: E731
        return cls(
            factors=d["factors"], oem_discount_pct=d["oem_discount_pct"], liquidity=d["liquidity"],
            cpu=d["cpu"], ram=sizes(d["ram"]), storage=sizes(d["storage"]), motherboard=d["motherboard"],
            psu=d["psu"], case_cooler=d["case_cooler"], monitors=d["monitors"],
        )


@dataclass
class Part:
    kind: str  # CPU / GPU / RAM / SSD / HDD / MB / PSU / корпус
    label: str  # what the listing says
    price: int
    priced_as: str = ""  # the table entry used when it differs ("≈ i5-12400F")

    @property
    def exact(self) -> bool:
        return not self.priced_as


@dataclass
class PcValuation:
    parts: list[Part] = field(default_factory=list)
    parts_sum: int = 0
    factor: float = 0.8
    factor_reason: str = ""
    oem_discount_pct: int = 0  # applied, 0 if not OEM
    pc_value: int = 0  # parts_sum × factor (× OEM discount)
    monitor: Part | None = None
    market: int | None = None  # pc_value + monitor; None = not valued
    flags: list[str] = field(default_factory=list)
    gpu_item: PriceItem | None = None

    @property
    def low_confidence(self) -> bool:
        return any(not p.exact for p in self.parts) or (self.monitor is not None and not self.monitor.exact)


# --- detection ------------------------------------------------------------------------------

_PC_WORD_RE = re.compile(r"\b(?:pc|pocitac\w*|sestav[aeuy]|desktop)\b")
_NOT_PC_RE = re.compile(
    r"\b(?:notebook\w*|laptop\w*|volant\w*|pedal\w*|ovladac\w*|gamepad\w*|joystick\w*|klavesnic\w*|mys"
    r"|sluchatk\w*|reproduktor\w*|webkamer\w*|kabel\w*|hr[ay]|koupim|hledam)\b"
    r"|\b(?:pro|do|na) (?:ps\d ?/ ?)?pc\b|\bpc ?/ ?(?:ps|xbox)|\b(?:ps\d|xbox\w*) ?/ ?pc\b"
)
_PART_RE = re.compile(r"\b(?:graficka|karta|kartu|skrin\w*|zdroj|chladic\w*|zakladni deska)\b")


def is_pc_build(title: str) -> bool:
    """A normalized title that sells a whole desktop PC ("Herní PC i5 / RTX 3060"), not a part or accessory."""
    if not _PC_WORD_RE.search(title) or _NOT_PC_RE.search(title):
        return False
    # "Skříň do PC", "Zdroj pro PC" are parts; "PC + skříň RGB" with a CPU/GPU named is a build.
    return not _PART_RE.search(title) or bool(re.search(r"\b(?:i[3579]|ryzen|rtx|gtx|rx \d)", title))


# --- component prices -----------------------------------------------------------------------

_INTEL_RE = re.compile(r"\bi([3579]) ?-? ?(\d{4,5}) ?([a-z]{0,2})\b")
_AMD_RE = re.compile(r"\b(?:ryzen|r)(?: ?([3579]))? ?(\d{4})(?: ?(x ?3 ?d|xt|x|g|ge|af))?\b")


def _cpu_key(text: str) -> tuple[str, int, int, str] | None:
    """(brand, tier, number, suffix) of 'i5-12400F' / 'Ryzen 5 5600X' / 'R7 5800X3D' / 'Ryzen 5600'."""
    t = normalize(text)
    if m := _INTEL_RE.search(t):
        return "intel", int(m.group(1)), int(m.group(2)), m.group(3)
    if m := _AMD_RE.search(t):
        number = int(m.group(2))
        tier = int(m.group(1)) if m.group(1) else {1: 3, 2: 3, 3: 3, 4: 5, 5: 5, 6: 5, 7: 7, 8: 7, 9: 9}[number // 100 % 10]
        return "amd", tier, number, (m.group(3) or "").replace(" ", "")
    return None


def _generation(number: int) -> int:
    return number // 1000


def cpu_price(text: str, table: dict[str, int]) -> tuple[str, int] | None:
    """(table key, price): the exact model, else the nearest of the same brand by generation, tier, number."""
    want = _cpu_key(text)
    if want is None:
        return None
    brand, tier, number, suffix = want
    known = [(key, k) for key in table if (k := _cpu_key(key)) and k[0] == brand]
    if not known:
        return None
    # Generation first, then X3D (a price class of its own), then tier (i5 vs i7), then the model
    # within the generation (14100 is closer to 13400 than to 13600), then the suffix (F, K, X).
    key, _ = min(known, key=lambda kk: (
        abs(_generation(kk[1][2]) - _generation(number)), (kk[1][3] == "x3d") != (suffix == "x3d"),
        abs(kk[1][1] - tier) // 2, abs(kk[1][2] % 1000 - number % 1000), kk[1][3] != suffix,
    ))
    return key, table[key]


def _nearest_size(size: int, table: dict[int, int]) -> tuple[int, bool]:
    """(table size, exact). Within 10 % counts as exact (500 GB ≈ 512 GB); ties go to the smaller size."""
    best = min(table, key=lambda s: (abs(s - size), s))
    return best, abs(best - size) <= best * 0.1


def _gb(size: int) -> str:
    return f"{size // 1000} TB" if size >= 1000 and size % 1000 == 0 else f"{size} GB"


def gpu_match(text: str, prices: PriceList) -> tuple[Match | None, bool]:
    """(match, exact). 'RTX 3060 Ti' not priced -> 'RTX 3060' (a cheaper sibling) as an estimate."""
    t = normalize(text)
    m = prices.match(t)
    if m and m.item.category == "gpu":
        return m, True
    for suffix in (" super", " ti", " xt", " xtx"):
        if t.endswith(suffix) or f"{suffix} " in t:
            m = prices.match(t.replace(suffix, "", 1))
            if m and m.item.category == "gpu":
                return m, False
    return None, False


def value_pc(specs: PcSpecs, comp: Components, prices: PriceList) -> PcValuation:
    v = PcValuation()
    cpu = cpu_price(specs.cpu, comp.cpu) if specs.cpu else None
    gpu, gpu_exact = gpu_match(specs.gpu, prices) if specs.gpu else (None, False)
    if cpu is None or gpu is None:
        missing = [f"{name} «{text}» нет в таблицах" if text else f"{name} не указан"
                   for name, text, ok in (("CPU", specs.cpu, cpu), ("GPU", specs.gpu, gpu)) if not ok]
        v.flags.append(f"{NO_HARDWARE}: {', '.join(missing)}")
        return v

    key, price = cpu
    same = _cpu_key(key)[1:] == _cpu_key(specs.cpu)[1:]
    v.parts.append(Part("CPU", specs.cpu, price, "" if same else f"≈ {key}"))
    v.gpu_item = gpu.item
    v.parts.append(Part("GPU", specs.gpu, gpu.item.market_price,
                        "" if gpu_exact and gpu.confidence == "high" else f"≈ {gpu.item.label}"))

    if specs.ram_gb:
        ram_type = specs.ram_type if specs.ram_type in comp.ram else "DDR4"
        size, exact = _nearest_size(specs.ram_gb, comp.ram[ram_type])
        note = "" if exact and ram_type == specs.ram_type else f"≈ {size} GB {ram_type}"
        label = f"{specs.ram_gb} GB" + (f" {specs.ram_type}" if specs.ram_type != "unknown" else "")
        v.parts.append(Part("RAM", label, comp.ram[ram_type][size], note))
    else:
        v.parts.append(Part("RAM", "не указана", 0, "не указана"))
    for drive in specs.storage:
        table = comp.storage["hdd" if drive.kind == "hdd" else "nvme"]
        size, exact = _nearest_size(drive.size_gb, table)
        kind = {"nvme": "SSD", "sata_ssd": "SSD", "hdd": "HDD"}[drive.kind]
        note = "" if exact and drive.kind != "sata_ssd" else f"≈ {_gb(size)} {'NVMe' if kind == 'SSD' else 'HDD'}"
        v.parts.append(Part(kind, f"{_gb(drive.size_gb)}{' SATA' if drive.kind == 'sata_ssd' else ''}",
                            table[size], note))
    v.parts.append(Part("MB", specs.motherboard or "материнская плата", comp.motherboard))
    if specs.psu_quality == "brand" and specs.psu_watts < 650 and specs.psu_watts:
        v.parts.append(Part("PSU", specs.psu or f"{specs.psu_watts} W", comp.psu["noname"], "≈ noname: меньше 650 W"))
    elif specs.psu_quality == "brand":
        note = "" if specs.psu_watts else "≈ брендовый 650 W+"
        v.parts.append(Part("PSU", specs.psu or "брендовый", comp.psu["brand"], note))
    else:
        label = specs.psu or (f"{specs.psu_watts} W" if specs.psu_watts else "не указан")
        v.parts.append(Part("PSU", label, comp.psu["noname"], "" if specs.psu_quality == "noname" else "≈ noname"))
    v.parts.append(Part("корпус", "корпус + кулер", comp.case_cooler))

    v.parts_sum = sum(p.price for p in v.parts)
    v.factor, v.factor_reason = pc_factor(specs, gpu.item, comp, prices)
    value = v.parts_sum * v.factor
    if specs.is_oem:
        v.oem_discount_pct = comp.oem_discount_pct
        value *= 1 - comp.oem_discount_pct / 100
    v.pc_value = int(value)

    if specs.monitor_included:
        mon = _monitor(specs, comp.monitors)
        res = "" if specs.monitor_resolution == "unknown" else specs.monitor_resolution
        label = " ".join(x for x in (f'{specs.monitor_size_inch:g}"' if specs.monitor_size_inch else "",
                                     res, f"{specs.monitor_hz} Hz" if specs.monitor_hz else "") if x)
        exact = (mon["size"] == specs.monitor_size_inch and mon["resolution"] == specs.monitor_resolution
                 and mon["hz"] == specs.monitor_hz)
        v.monitor = Part("монитор", label or "модель не указана", mon["price"],
                         "" if exact else f'≈ {mon["size"]}" {mon["resolution"]} {mon["hz"]} Hz')
    v.market = v.pc_value + (v.monitor.price if v.monitor else 0)

    strong = prices.match("RTX 3070 8GB")
    if specs.psu_quality == "noname" and strong and gpu.item.market_price >= strong.item.market_price:
        v.flags.append("noname блок питания при видеокарте уровня RTX 3070+")
    if specs.ram_modules == 1:
        v.flags.append("одноканал (один модуль RAM)")
    if v.low_confidence:
        rough = [p.kind for p in v.parts + ([v.monitor] if v.monitor else []) if not p.exact]
        v.flags.append(f"оценка неточная: {', '.join(dict.fromkeys(rough))}")
    return v


def _old_gpu(model: str) -> bool:
    """GTX 10xx and older NVIDIA, RX 4xx / 5xx (Polaris) and older AMD."""
    t = normalize(model)
    nv = re.match(r"gtx (\d{3,4})\b", t)
    amd = re.match(r"rx (\d{3,4})\b", t)
    return bool(nv and int(nv.group(1)) < 1100 or amd and int(amd.group(1)) < 1000)


def pc_factor(specs: PcSpecs, gpu: PriceItem, comp: Components, prices: PriceList) -> tuple[float, str]:
    """How a whole PC sells relative to its parts: 1.0 modern gaming, 0.9 the rest, 0.8 old hardware.

    low: DDR3, a GTX 10xx-class GPU or older, or a CPU before Intel 8th gen. high: a GPU priced at least
    like the RX 6600 (≈ RTX 3060) and a CPU from Intel 10th gen / Ryzen 3000. Everything else is mid.
    """
    f = comp.factors
    brand, _, number, _ = _cpu_key(specs.cpu)
    gen = _generation(number)
    old = []
    if specs.ram_type == "DDR3":
        old.append("DDR3")
    if _old_gpu(gpu.model):
        old.append(gpu.model)
    if brand in f["low_cpu_before"] and gen < f["low_cpu_before"][brand]:
        old.append(f"CPU {gen} gen")
    if old:
        return f["low"], "старое железо: " + ", ".join(old)
    ref = prices.match(f["high_gpu_from"])
    strong_gpu = ref is not None and gpu.market_price >= ref.item.market_price
    new_cpu = gen >= f["high_cpu_from"].get(brand, 99)
    if strong_gpu and new_cpu:
        return f["high"], f"GPU от уровня {f['high_gpu_from']}, CPU от Intel 10 gen / Ryzen 3000"
    why = [] if strong_gpu else [f"GPU слабее {f['high_gpu_from']}"]
    why += [] if new_cpu else [f"CPU {gen} gen"]
    return f["mid"], ", ".join(why)


def _monitor(specs: PcSpecs, monitors: list[dict]) -> dict:
    return min(monitors, key=lambda m: (
        specs.monitor_resolution != "unknown" and m["resolution"] != specs.monitor_resolution,
        abs(m["size"] - specs.monitor_size_inch) if specs.monitor_size_inch else 0,
        abs(m["hz"] - specs.monitor_hz) if specs.monitor_hz else 0,
        m["price"],
    ))


def pc_match(specs: PcSpecs, valuation: PcValuation, comp: Components) -> Match:
    """A synthetic price list match, so the PC goes through evaluate() like any other device."""
    variant = " / ".join(x for x in (specs.cpu, specs.gpu, f"{specs.ram_gb} GB" if specs.ram_gb else "") if x)
    item = PriceItem("pc", "Herní PC", variant, valuation.market or 0, comp.liquidity, "estimate")
    return Match(item, Identity("pc", "pc"), "low" if valuation.low_confidence else "high")


def breakdown_lines(v: PcValuation) -> list[str]:
    """Human-readable calculation (Russian), for Telegram and the CLI."""
    kc = lambda x: f"{x:,}".replace(",", " ")  # noqa: E731
    lines = [f"{p.kind} {p.label}: {kc(p.price)}" + (f" ({p.priced_as})" if p.priced_as else "") for p in v.parts]
    calc = f"Сумма {kc(v.parts_sum)} × {v.factor:.1f} ({v.factor_reason})"
    if v.oem_discount_pct:
        calc += f" × OEM −{v.oem_discount_pct} %"
    lines.append(f"{calc} = {kc(v.pc_value)}")
    if v.monitor:
        lines.append(f"Монитор {v.monitor.label}: +{kc(v.monitor.price)} (полная цена"
                     + (f", {v.monitor.priced_as})" if v.monitor.priced_as else ")"))
    return lines
