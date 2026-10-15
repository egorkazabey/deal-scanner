"""Ready-built PCs: detection, component prices, valuation, pipeline, alert text."""

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from scanner.config import PROJECT_ROOT, AppConfig, GeminiConfig, Settings, load_config
from scanner.db import Database
from scanner.gemini import GeminiReviewer
from scanner.models import Listing
from scanner.notify import format_pc_alert
from scanner.pc import NO_HARDWARE, Components, Drive, PcSpecs, breakdown_lines, cpu_price, is_pc_build, value_pc
from scanner.pipeline import run_scan, value_pc_listing
from scanner.pricing import PriceList, evaluate, normalize
from test_gemini import FakeClient, response
from test_pipeline import FakeBazos

TODAY = date(2026, 10, 15)
START = datetime(2026, 10, 15, 14, 0)
CFG = load_config(PROJECT_ROOT / "config.yaml")
PRICES = PriceList.load(CFG.pricing.prices_path)
COMP = Components.load(CFG.pricing.components_path)
FIXTURES = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "pc_listings.json").read_text(encoding="utf-8"))


def specs(**kw) -> PcSpecs:
    base = dict(
        is_pc=True, cpu="i5-12400F", gpu="RTX 3070 8GB", ram_gb=16, ram_type="DDR4", ram_modules=2,
        storage=[Drive(kind="nvme", size_gb=1000)], psu="Seasonic Focus GX-750", psu_watts=750, psu_quality="brand",
        motherboard="B660", monitor_included=False, monitor_size_inch=0, monitor_resolution="unknown", monitor_hz=0,
        is_oem=False, condition_notes=["в хорошем состоянии"], red_flags=[], scam_risk="low",
    )
    return PcSpecs(**(base | kw))


def listing(**kw) -> Listing:
    base = dict(source="bazos", id="77", url="https://pc.bazos.cz/inzerat/77/herni-pc.php",
                title="Herní PC i5-12400F / RTX 3070 / 16GB", price=9000, location="Pardubice", posted_date=TODAY,
                description="Prodám herní PC, vše funkční.")
    return Listing(**(base | kw))


# --- detection -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, pc",
    [
        ("Herní PC i5-9400F / 16GB / GTX 1650 SUPER / NVMe + 1TB HDD", True),
        ("HERNÍ PC+MONITOR/ 4JÁDRO/8GB GRAFIKA/SSD", True),
        ("Herní Počítač", True),
        ("Svižný stolní PC–16GB RAM,512GB SSD+500GB HDD, Windows 11Pro", True),
        ("🔥 PC – CELÝ GAMING SETUP + MONITOR A STŮL 🎮", True),
        ("Prodám/výměna Vodou chlazené PC | i9-11900K | RTX 3080 AORUS", True),
        ("Herní notebook Asus TUF A15 Ryzen 7, RTX 3070, 144Hz", False),
        ("Tracer TRAJOY46524 volant pc/ps3/ps4/xbox one", False),
        ("Herní set volantu a pedálů HORI RWA pro PS4/PC – jako nový", False),
        ("Grafická karta do PC RTX 3060", False),
        ("Skříň do PC", False),
        ("Hry na PC", False),
    ],
)
def test_is_pc_build(title, pc):
    assert is_pc_build(normalize(title)) is pc


@pytest.mark.parametrize(
    "cpu, key",
    [
        ("Intel Core i5-12400F", "i5-12400F"),
        ("i5-12400", "i5-12400F"),          # no F: the same model class
        ("i7 8700K", "i7-8700K"),
        ("Ryzen 5 5600X", "R5 5600X"),
        ("RYZEN 5600", "R5 5600"),          # tier from the number
        ("Ryzen 7 7800X3D", "R7 7800X3D"),
        ("R 5800X", "R7 5700X"),            # not the X3D, it is a different price class
        ("Ryzen 7 7700X", "R5 7600"),       # nearest same generation without X3D
        ("I3-14100F", "i5-14400F"),         # same generation, the nearest tier
        ("i9-14900KF", "i9-14900K"),
        ("Ryzen 9 7950X3D", "R7 7800X3D"),  # X3D stays X3D
        ("Ryzen 7 9800X3D", "R7 9800X3D"),
        ("Ryzen 5 1600 AF", "R5 2600"),
    ],
)
def test_cpu_price_exact_and_nearest(cpu, key):
    assert cpu_price(cpu, COMP.cpu)[0] == key


def test_cpu_unknown_family():
    assert cpu_price("Xeon E5-2680 v4", COMP.cpu) is None


# --- valuation -------------------------------------------------------------------------------


def test_value_sum_factor_and_ceiling():
    v = value_pc(specs(), COMP, PRICES)
    assert [(p.kind, p.price) for p in v.parts] == [
        ("CPU", 2500), ("GPU", 7500), ("RAM", 1000), ("SSD", 1000), ("MB", 1800), ("PSU", 1100), ("корпус", 1100)]
    assert (v.parts_sum, v.factor, v.pc_value, v.market) == (16000, 1.0, 16000, 16000)
    assert v.flags == [] and not v.low_confidence
    ev = value_pc_listing(evaluate(listing(), PRICES, CFG.pricing, TODAY), specs(), COMP, PRICES, CFG, TODAY).evaluation
    assert ev.is_candidate and ev.match.item.liquidity == 2
    # > 12 000: min(16 000 × 0.85 = 13 600, 16 000 − 2 000 = 14 000); offer min(9 000 × 0.92, 13 600 × 0.95)
    assert (ev.max_price, ev.offer_price) == (13600, 8200)


def test_oem_monitor_and_flags():
    v = value_pc(specs(is_oem=True, monitor_included=True, monitor_size_inch=27, monitor_resolution="1440p",
                       monitor_hz=144, psu_quality="noname", psu="Eurocase 600W", ram_modules=1), COMP, PRICES)
    assert v.parts_sum == 15200  # noname PSU 300 instead of 1 100
    assert v.pc_value == int(15200 * 1.0 * 0.85) == 12920
    assert v.monitor.price == 3000 and v.monitor.exact and v.market == 15920
    assert "noname блок питания при видеокарте уровня RTX 3070+" in v.flags
    assert "одноканал (один модуль RAM)" in v.flags
    assert "Монитор 27\" 1440p 144 Hz: +3 000 (полная цена)" in breakdown_lines(v)
    assert ("Сумма 15 200 × 1.0 (GPU от уровня RX 6600, CPU от Intel 10 gen / Ryzen 3000) × OEM −15 % = 12 920"
            in breakdown_lines(v))


def test_weak_gpu_with_noname_psu_is_fine():
    v = value_pc(specs(gpu="RTX 3060 12GB", psu_quality="noname"), COMP, PRICES)
    assert not any("noname" in f for f in v.flags)


@pytest.mark.parametrize("missing", [{"cpu": ""}, {"gpu": ""}, {"gpu": "Intel Arc A770"}, {"cpu": "Xeon E5-2680"}])
def test_no_cpu_or_gpu_is_not_valued(missing):
    v = value_pc(specs(**missing), COMP, PRICES)
    assert v.market is None and v.flags[0].startswith(NO_HARDWARE)
    result = value_pc_listing(evaluate(listing(), PRICES, CFG.pricing, TODAY), specs(**missing), COMP, PRICES, CFG, TODAY)
    assert result.skipped.startswith(NO_HARDWARE) and not result.evaluation.is_candidate


def test_nearest_sizes_are_low_confidence():
    v = value_pc(specs(ram_gb=48, ram_type="DDR5", storage=[Drive(kind="sata_ssd", size_gb=480)]), COMP, PRICES)
    ram = next(p for p in v.parts if p.kind == "RAM")
    ssd = next(p for p in v.parts if p.kind == "SSD")
    assert (ram.price, ram.priced_as) == (3000, "≈ 32 GB DDR5")  # ties go to the smaller size
    assert (ssd.price, ssd.priced_as) == (500, "≈ 512 GB NVMe")
    assert v.low_confidence and v.flags[-1] == "оценка неточная: RAM, SSD"


@pytest.mark.parametrize(
    "change, factor",
    [
        ({}, 1.0),                                                  # RTX 3070 + i5-12400F
        ({"cpu": "i5-9400F"}, 0.9),                                 # 9th gen: not 10+, not before 8th
        ({"cpu": "Ryzen 5 2600"}, 0.9),                             # Ryzen 2000: not 3000+
        ({"cpu": "Ryzen 5 3600"}, 1.0),
        ({"gpu": "RTX 3050 8GB"}, 0.9),                             # below RX 6600
        ({"gpu": "RX 6600"}, 1.0),                                  # the threshold itself
        ({"ram_type": "DDR5"}, 1.0),
        ({"ram_type": "DDR3"}, 0.8),
        ({"gpu": "GTX 1080 Ti"}, 0.8),                              # GTX 10xx
        ({"gpu": "RX 580 8GB"}, 0.8),                               # Polaris
        ({"cpu": "i7-4790"}, 0.8),                                  # before 8th gen
        ({"cpu": "i7-8700K", "gpu": "GTX 1660 Super"}, 0.9),        # 8th gen counts as mid
    ],
)
def test_pc_factor_classes(change, factor):
    assert value_pc(specs(**change), COMP, PRICES).factor == factor


def test_old_hardware_reason():
    v = value_pc(specs(cpu="i7-4790", gpu="GTX 1070", ram_type="DDR3"), COMP, PRICES)
    assert (v.factor, v.factor_reason) == (0.8, "старое железо: DDR3, GTX 1070, CPU 4 gen")


# --- three real PCs from Bazoš (tests/fixtures/pc_listings.json, specs as Gemini read them) -----


def _real(i):
    o = FIXTURES[i]
    l = Listing("bazos", o["id"], o["url"], o["title"], o["price"], o["location"],
                date.fromisoformat(o["posted_date"]), o["description"])
    return value_pc_listing(evaluate(l, PRICES, CFG.pricing, TODAY), PcSpecs(**o["specs"]), COMP, PRICES, CFG, TODAY)


def test_real_ryzen_5600x_rtx_2080():
    r = _real(0)  # 15 000 Kč, everything named, EVGA 750W
    assert r.valuation.parts_sum == 2200 + 5500 + 1000 + 1000 + 1800 + 1100 + 1100 == 13700
    assert (r.valuation.factor, r.valuation.pc_value) == (1.0, 13700) and not r.valuation.low_confidence
    # > 12 000: min(13 700 × 0.85 = 11 645, 13 700 − 2 000 = 11 700) -> 11 600; offer 11 600 × 0.95 -> 11 000
    assert (r.evaluation.max_price, r.evaluation.offer_price) == (11600, 11000)
    assert r.skipped == "low_profit"  # asking 15 000 > value 13 700


def test_real_i5_12400_rtx_3060_with_monitor():
    r = _real(1)  # 17 500 Kč with an MSI 180 Hz Full HD monitor, PSU and RAM type not stated
    v = r.valuation
    assert [p.priced_as for p in v.parts if p.priced_as] == ["≈ i5-12400F", "≈ 16 GB DDR4", "≈ noname"]
    assert (v.parts_sum, v.factor, v.pc_value) == (13700, 1.0, 13700)  # RTX 3060 + 12th gen
    assert (v.monitor.price, v.market) == (1500, 15200)  # monitor at full price, on top
    # > 12 000: min(15 200 × 0.85 = 12 920, 15 200 − 2 000 = 13 200) -> 12 900; offer 12 900 × 0.95 -> 12 200
    assert (r.evaluation.max_price, r.evaluation.offer_price) == (12900, 12200)
    assert "оценка неточная: CPU, RAM, PSU, монитор" in v.flags


def test_real_i5_9400f_gtx_1650_super():
    r = _real(2)  # 6 900 Kč, 256 GB NVMe + 1 TB HDD, Gigabyte P450B
    v = r.valuation
    assert [(p.kind, p.price) for p in v.parts] == [
        ("CPU", 900), ("GPU", 1500), ("RAM", 1000), ("SSD", 500), ("HDD", 300), ("MB", 1800), ("PSU", 300),
        ("корпус", 1100)]
    assert next(p for p in v.parts if p.kind == "GPU").priced_as == "≈ GTX 1650"  # Super not priced: base model
    assert (v.parts_sum, v.factor, v.pc_value) == (7400, 0.9, 6660)  # GTX 1650 + 9th gen on DDR4: mid
    assert v.factor_reason == "GPU слабее RX 6600, CPU 9 gen"
    # 5 000 – 12 000: min(6 660 × 0.78 = 5 194, 6 660 − 2 000 = 4 660) -> 4 600; offer 4 600 × 0.95 -> 4 300
    assert (r.evaluation.max_price, r.evaluation.offer_price) == (4600, 4300)


# --- pipeline ----------------------------------------------------------------------------------


def _factory(client):
    def make(settings, cfg, pricing, prices, db):
        return GeminiReviewer(Settings(_env_file=None, gemini_api_key="x"), cfg, pricing, prices, db, client=client)
    return make


def _scan(db, listings, client, at=START, cfg=None, **kw):
    return asyncio.run(run_scan(Settings(_env_file=None, gemini_api_key=None), cfg or CFG, db, FakeBazos(listings),
                                now=lambda: at, reviewer_factory=_factory(client), **kw))


def test_pipeline_pc_alert_once_and_cached():
    db = Database(":memory:")
    client = FakeClient([response(specs())])
    report = _scan(db, [listing()], client)
    [alert] = db.pending_alerts()
    assert alert["text"].startswith("🖥 🟢 <b>ПК i5-12400F / RTX 3070 8GB / 16 GB — 9 000 Kč</b>")
    assert "Сумма 16 000 × 1.0 (GPU от уровня RX 6600, CPU от Intel 10 gen / Ryzen 3000) = 16 000" in alert["text"]
    assert "Рынок ~16 000 · предложить 8 200 · максимум 13 600" in alert["text"]
    assert "Byla grafická karta použitá na těžbu?" in alert["text"]
    assert len(client.calls) == 1 and report.pcs[0].skipped is None
    _scan(db, [listing()], client, at=START + timedelta(minutes=10))
    assert len(client.calls) == 1 and len(db.pending_alerts()) == 1  # cached specs, already alerted


def test_pipeline_old_pc_is_not_read():
    db = Database(":memory:")
    client = FakeClient([])
    report = _scan(db, [listing(posted_date=TODAY - timedelta(days=6))], client)
    assert client.calls == [] and report.pcs == [] and report.alerts == []


def test_pipeline_pc_budget_per_run():
    db = Database(":memory:")
    pcs = [listing(id=str(i), url=f"https://x/{i}") for i in range(3)]
    client = FakeClient([response(specs()), response(specs())])
    cfg = AppConfig(pricing=CFG.pricing, notify=CFG.notify.model_copy(update={"pc_specs_per_run": 2}))
    report = _scan(db, pcs, client, cfg=cfg)
    assert len(client.calls) == 2 and len(report.alerts) == 2
    assert report.pcs[-1].skipped.startswith("лимит Gemini для ПК")


def test_pipeline_pc_without_ai_is_skipped():
    db = Database(":memory:")
    report = _scan(db, [listing()], FakeClient([]), ai=False)
    assert report.alerts == [] and report.pcs[0].skipped.startswith("без ИИ")


def test_pipeline_not_a_pc_and_high_scam():
    db = Database(":memory:")
    client = FakeClient([response(specs(is_pc=False)), response(specs(scam_risk="high"))])
    report = _scan(db, [listing(), listing(id="78", url="https://x/78")], client)
    assert report.alerts == []
    assert [r.skipped for r in report.pcs] == ["не готовый ПК (по Gemini)", "Gemini: высокий риск мошенничества"]


def test_format_pc_alert_flags():
    s = specs(is_oem=True, ram_modules=1, red_flags=["майнинг"])
    r = value_pc_listing(evaluate(listing(price=7000), PRICES, CFG.pricing, TODAY), s, COMP, PRICES, CFG, TODAY)
    text, summary = format_pc_alert("deal", r.evaluation, s, r.valuation, TODAY)
    assert "× OEM −15 %" in text
    assert "⚠️ флаги: одноканал (один модуль RAM); майнинг; OEM-сборка (−15 %)" in text
    assert summary.startswith('🖥 🟢 <a href="https://pc.bazos.cz/inzerat/77/herni-pc.php">')
