"""Self-learning price list: observations, probable sales, weekly proposals.

- Every scan, each confidently matched listing (match "high", not scam / too cheap / accessory /
  stop word) is an observation of its price list item: one row per listing, last asking price.
- A listing that left the search results gets its detail page checked now and then; 404/410 means
  it was deleted, most likely sold at its last price ("ушло за").
- The weekly report proposes a new market_price per item with enough observations: the median sale
  price when there are enough sales, else the median asking price × asking_to_sold_pct, never more than
  ±max_step_pct from the current price. Nothing changes without the owner's button.
- Devices the parser knows but the price list does not have are counted as suggestions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from statistics import median
from typing import Any

from scanner import overrides
from scanner.config import AppConfig, LearnConfig, Settings, load_config
from scanner.db import Database
from scanner.notify import kc
from scanner.pricing import Evaluation, PriceList, identify, normalize
from scanner.sources.bazos import BazosError

log = logging.getLogger(__name__)

_SKIP_FLAGS = {"too_cheap", "scam_suspect", "accessory", "shop"}


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# --- collecting -------------------------------------------------------------------------------


def record(db: Database, evaluations: list[Evaluation], now: datetime) -> int:
    """Store observations and unknown models from one scan; returns the number of observations."""
    n = 0
    with db.conn:
        for ev in evaluations:
            l = ev.listing
            if l.price is None or ev.stop or _SKIP_FLAGS & set(ev.flags):
                continue
            if ev.match is not None:
                if ev.match.confidence != "high" or ev.match.item.category == "pc":
                    continue
                db.conn.execute(
                    """INSERT INTO price_observations (source, listing_id, item, price, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT (source, listing_id, item) DO UPDATE SET price = excluded.price,
                           last_seen = excluded.last_seen, gone_at = NULL""",
                    (l.source, l.id, ev.match.item.label, l.price, _ts(now), _ts(now)),
                )
                n += 1
            elif (ident := identify(normalize(l.title))) is not None:
                attrs = " ".join(f"{v}{'gb' if k == 'storage' else ''}" for k, v in sorted(ident.attrs.items()) if v)
                db.conn.execute(
                    """INSERT INTO unknown_models (source, listing_id, model, title, price, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT (source, listing_id) DO UPDATE SET model = excluded.model,
                           title = excluded.title, price = excluded.price, last_seen = excluded.last_seen""",
                    (l.source, l.id, f"{ident.model} {attrs}".strip(), l.title, l.price, _ts(now)),
                )
    return n


async def check_sold(db: Database, client: Any, cfg: LearnConfig, now: datetime) -> int:
    """Detail pages of listings that left the results: 404/410 = probable sale. Returns sales found."""
    rows = db.conn.execute(
        """SELECT o.source, o.listing_id, l.url FROM price_observations o
           JOIN listings l ON l.source = o.source AND l.id = o.listing_id
           WHERE o.gone_at IS NULL AND o.last_seen < ? AND o.first_seen >= ?
             AND (o.checked_at IS NULL OR o.checked_at < ?)
           GROUP BY o.source, o.listing_id ORDER BY MAX(o.last_seen) DESC LIMIT ?""",
        (_ts(now - timedelta(days=cfg.unseen_days)), _ts(now - timedelta(days=cfg.give_up_days)),
         _ts(now - timedelta(days=cfg.recheck_days)), cfg.sold_checks_per_run),
    ).fetchall()
    sold = 0
    for r in rows:
        gone = False
        try:
            await client.get(r["url"])
        except BazosError as exc:
            if exc.status not in (404, 410):
                log.warning("sold check %s: %s", r["url"], exc)
                continue
            gone = True
        with db.conn:
            db.conn.execute(
                "UPDATE price_observations SET checked_at = ?, gone_at = CASE WHEN ? THEN ? ELSE NULL END "
                "WHERE source = ? AND listing_id = ?",
                (_ts(now), int(gone), _ts(now), r["source"], r["listing_id"]),
            )
        sold += gone
    return sold


# --- the report -------------------------------------------------------------------------------


@dataclass
class Proposal:
    item: str
    confidence: str  # the item's "checked" / "estimate"
    market: int
    proposed: int
    asks: int  # observations
    ask_median: int
    sold: int
    sold_median: int | None
    basis: str  # "ушло за" / "объявления × 95 %"

    @property
    def change_pct(self) -> float:
        return (self.proposed - self.market) / self.market * 100


@dataclass
class NewModel:
    model: str
    listings: int
    median_price: int
    example: str


@dataclass
class Report:
    proposals: list[Proposal]
    stable: int  # items with enough data and no proposal
    new_models: list[NewModel]
    observed_items: int  # items with any observation in the window


def build_report(db: Database, prices: PriceList, cfg: LearnConfig, now: datetime) -> Report:
    since = _ts(now - timedelta(days=cfg.window_days))
    rows = db.conn.execute(
        "SELECT item, price, gone_at FROM price_observations WHERE last_seen >= ? OR gone_at >= ?", (since, since)
    ).fetchall()
    by_item: dict[str, list] = {}
    for r in rows:
        by_item.setdefault(r["item"], []).append(r)
    items = {i.label: i for i in prices.items}
    proposals, stable = [], 0
    for label, obs in by_item.items():
        item = items.get(label)
        if item is None or len(obs) < cfg.min_observations:
            continue
        asks = [r["price"] for r in obs]
        solds = [r["price"] for r in obs if r["gone_at"]]
        if len(solds) >= cfg.min_sold:
            target, basis = median(solds), "ушло за"
        else:
            target, basis = median(asks) * cfg.asking_to_sold_pct / 100, f"объявления × {cfg.asking_to_sold_pct} %"
        market = item.market_price
        step = market * cfg.max_step_pct / 100
        capped = min(max(target, market - step), market + step)
        # Round to 100 towards the current price, so the cap holds after rounding.
        proposed = int(capped // 100 * 100) if capped >= market else int(-(-capped // 100) * 100)
        p = Proposal(label, item.confidence, market, proposed, len(asks), int(median(asks)), len(solds),
                     int(median(solds)) if solds else None, basis)
        if abs(p.change_pct) < cfg.min_change_pct:
            stable += 1
        else:
            proposals.append(p)
    proposals.sort(key=lambda p: (p.confidence != "estimate", -abs(p.change_pct)))

    unknown = db.conn.execute(
        "SELECT model, COUNT(*) AS n, GROUP_CONCAT(price) AS prices, MAX(title) AS example FROM unknown_models "
        "WHERE last_seen >= ? GROUP BY model HAVING n >= ? ORDER BY n DESC LIMIT 10",
        (since, cfg.new_model_min_listings),
    ).fetchall()
    # A bare family ("macbook" without a chip, "xbox") can't become a price list item: skip it.
    new_models = [NewModel(r["model"], r["n"], int(median(int(x) for x in r["prices"].split(","))), r["example"])
                  for r in unknown if " " in r["model"]]
    return Report(proposals, stable, new_models, len(by_item))


def save_report(db: Database, report: Report) -> int:
    report_id = (db.conn.execute("SELECT MAX(report_id) FROM price_proposals").fetchone()[0] or 0) + 1
    with db.conn:
        db.conn.executemany(
            "INSERT INTO price_proposals (report_id, n, item, old_price, new_price) VALUES (?, ?, ?, ?, ?)",
            [(report_id, n, p.item, p.market, p.proposed) for n, p in enumerate(report.proposals, 1)],
        )
    return report_id


def format_report(report: Report, cfg: LearnConfig, limit: int = 20) -> str:
    out = ["<b>📈 Отчёт по ценам</b>"]
    if not report.proposals:
        out.append(f"Предложений нет: позиций с данными {report.observed_items}, без изменений {report.stable}. "
                   f"Для предложения нужно {cfg.min_observations}+ объявлений за {cfg.window_days} дн.")
    for n, p in enumerate(report.proposals[:limit], 1):
        sold = f" · ушло {kc(p.sold_median)} ({p.sold})" if p.sold else ""
        mark = " ⚠️estimate" if p.confidence == "estimate" else ""
        out.append(f"\n<b>{n}. {escape(p.item)}</b>{mark}\n"
                   f"      объявления {kc(p.ask_median)} ({p.asks}){sold}\n"
                   f"      {kc(p.market)} → <b>{kc(p.proposed)}</b> ({p.change_pct:+.0f} %, по: {p.basis})")
    if len(report.proposals) > limit:
        out.append(f"\n… и ещё {len(report.proposals) - limit}")
    if report.proposals and report.stable:
        out.append(f"\nБез изменений: {report.stable} позиций")
    if report.new_models:
        out.append("\n<b>🆕 Часто встречаются, но нет в прайсе</b>")
        out += [f"• {escape(m.model)}: {m.listings} объявл., медиана {kc(m.median_price)} Kč "
                f"(«{escape(m.example[:40])}»)" for m in report.new_models]
    return "\n".join(out)


def proposals(db: Database, report_id: int) -> list:
    return db.conn.execute(
        "SELECT * FROM price_proposals WHERE report_id = ? ORDER BY n", (report_id,)
    ).fetchall()


def toggle(db: Database, report_id: int, n: int) -> None:
    with db.conn:
        db.conn.execute(
            "UPDATE price_proposals SET selected = 1 - selected WHERE report_id = ? AND n = ? AND applied_at IS NULL",
            (report_id, n),
        )


def apply(settings: Settings, db: Database, report_id: int, only_selected: bool, now: datetime | None = None) -> list[str]:
    """Write the proposals into prices.json (through overrides, so they survive a redeploy)."""
    prices_path = load_config(settings.config_path).pricing.prices_path
    notes = []
    for p in proposals(db, report_id):
        if p["applied_at"] or (only_selected and not p["selected"]):
            continue
        hit = overrides.item_price(prices_path, p["item"])
        if hit is None:
            notes.append(f"{p['item']}: позиции больше нет")
            continue
        index, current = hit
        if current != p["old_price"]:
            notes.append(f"{p['item']}: цена уже {kc(current)}, пропущено")
            continue
        overrides.set_market_price(settings, db, index, p["new_price"])
        with db.conn:
            db.conn.execute("UPDATE price_proposals SET applied_at = ? WHERE report_id = ? AND n = ?",
                            (_ts(now or datetime.now()), report_id, p["n"]))
        notes.append(f"{p['item']}: {kc(p['old_price'])} → {kc(p['new_price'])}")
    return notes


def new_report(settings: Settings, db: Database, now: datetime | None = None) -> tuple[int, Report, AppConfig]:
    cfg = load_config(settings.config_path)
    report = build_report(db, PriceList.load(cfg.pricing.prices_path), cfg.learn, now or datetime.now())
    return save_report(db, report), report, cfg
