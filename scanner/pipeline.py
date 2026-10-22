"""One scan: Bazoš → SQLite → rules → Gemini → alerts stored in the `alerts` table.

Delivery is separate (`scanner.bot`): alerts are created here with sent_at = NULL and sent right
away, or collected into the morning digest when created in quiet hours.

What becomes an alert:
- deal: a candidate posted in the last `notify.max_age_days` days, not alerted at this price yet;
- drop: a candidate whose price fell in the last `notify.price_drop_days` days (any age but stale);
- watch: a listing with "🔁 Снизится — сообщи" whose price fell >= `notify.watch_drop_pct` %;
- gone: a "✍️ Написал" listing whose detail page answers 404/410.
Stale listings (older than `pricing.stale_days`) never go out: they are in /top only.
Hidden and contacted listings get no deal/drop alerts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from scanner import learn
from scanner.config import AppConfig, Settings
from scanner.db import Database, Status, UpsertKind
from scanner.gemini import PRAGUE, GeminiReviewer, NoAI, Review, Verdict, review_candidates
from scanner.notify import format_alert, format_gone, format_pc_alert, kc
from scanner.pc import Components, PcSpecs, PcValuation, is_pc_build, pc_match, value_pc
from scanner.pricing import Evaluation, PriceList, evaluate, evaluate_many, normalize, rank
from scanner.sources.bazos import BazosError

HTTP_SOURCES = {"bazos", "sbazar"}  # sources whose detail pages answer 404/410 once a listing is deleted

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    listings: int = 0
    new: int = 0
    price_changed: int = 0
    candidates: int = 0
    alerts: list[int] = field(default_factory=list)  # alert ids
    errors: list[str] = field(default_factory=list)
    evaluations: list[Evaluation] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    labels: dict[tuple[str, str], str] = field(default_factory=dict)  # NEW / "old price →" for the CLI
    previews: list[str] = field(default_factory=list)  # alert texts of a dry run (not stored, not sent)
    pcs: list[PcResult] = field(default_factory=list)  # ready-built PCs looked at in this run


@dataclass
class PcResult:
    evaluation: Evaluation  # with the synthetic "Herní PC" match when valued
    specs: PcSpecs | None = None
    valuation: PcValuation | None = None
    skipped: str | None = None  # why it is not valued / not alerted


def notify_kind(
    ev: Evaluation, db: Database, cfg: AppConfig, now: datetime, *, require_candidate: bool = True
) -> str | None:
    """'deal' / 'drop' if the candidate may go to Telegram now, else None.

    require_candidate=False: a ready-built PC before its valuation (only age, status and history count).
    """
    listing = ev.listing
    if listing.price is None or "stale" in ev.flags or ev.stop:
        return None
    if require_candidate and not ev.is_candidate:
        return None
    row = db.get(listing.source, listing.id)
    if row and row["status"] in (Status.HIDDEN, Status.CONTACTED):
        return None
    if db.was_alerted(listing.source, listing.id, listing.price):
        return None
    review = db.get_review(listing.source, listing.id, listing.price)
    if review and not review["notify"]:
        return None  # Gemini already said skip at this price
    drop = db.last_price_drop(listing.source, listing.id)
    if drop and now - drop[2] <= timedelta(days=cfg.notify.price_drop_days):
        return "drop"
    posted = listing.posted_date or (listing.first_seen.date() if listing.first_seen else None)
    if posted and (now.date() - posted).days <= cfg.notify.max_age_days:
        return "deal"
    return None


async def run_scan(
    settings: Settings,
    cfg: AppConfig,
    db: Database,
    client: Any,  # BazosClient: scan(), fetch_detail(), get()
    *,
    ai: bool = True,
    max_alerts: int | None = None,
    quiet: bool = False,
    dry: bool = False,  # no alerts stored (so none sent later), no watch / gone side effects
    now: Callable[[], datetime] = datetime.now,
    reviewer_factory: Callable[..., GeminiReviewer] = GeminiReviewer,
) -> RunReport:
    started = now()
    run_id = db.start_run(started, getattr(client, "source", "bazos"))
    report = RunReport()
    try:
        await _scan(settings, cfg, db, client, report, ai, max_alerts, quiet, dry, started, reviewer_factory)
    except Exception as exc:  # noqa: BLE001 - recorded for /status, then re-raised
        report.errors.append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        db.finish_run(
            run_id, now(), listings=report.listings, new=report.new, price_changed=report.price_changed,
            candidates=report.candidates, alerts=len(report.alerts), errors="\n".join(report.errors),
        )
    return report


async def _scan(
    settings: Settings, cfg: AppConfig, db: Database, client: Any, report: RunReport,
    ai: bool, max_alerts: int | None, quiet: bool, dry: bool, started: datetime,
    reviewer_factory: Callable[..., GeminiReviewer],
) -> None:
    prices = PriceList.load(cfg.pricing.prices_path)
    today = started.astimezone(PRAGUE).date() if started.tzinfo else started.date()
    result = await client.scan()
    report.listings, report.errors = len(result.listings), list(result.errors)

    drops: dict[tuple[str, str], int] = {}
    for listing in result.listings:
        upsert = db.upsert(listing, started)
        key = (listing.source, listing.id)
        if upsert.kind is UpsertKind.NEW:
            report.new += 1
            report.labels[key] = "NEW"
        elif upsert.kind is UpsertKind.PRICE_CHANGED:
            report.price_changed += 1
            report.labels[key] = f"{kc(upsert.old_price)} →"
        # Keep the description loaded earlier, so the rules see it and no detail is refetched.
        if listing.description is None and (row := db.get(*key)):
            listing.description = row["description"]
        if drop := db.last_price_drop(*key):
            drops[key] = drop[0]

    evaluations = evaluate_many(result.listings, prices, cfg.pricing, today)
    report.evaluations = evaluations
    learn.record(db, evaluations, started)  # observations for the weekly price report
    report.candidates = sum(e.is_candidate for e in evaluations)
    by_key = {(e.listing.source, e.listing.id): e for e in evaluations}

    alerted: set[tuple[str, str]] = set()
    if not dry:
        _watch_alerts(db, cfg, by_key, today, quiet, report, alerted, started)

    picked: list[tuple[Evaluation, str]] = []
    for ev in rank(evaluations):
        key = (ev.listing.source, ev.listing.id)
        if key not in alerted and (kind := notify_kind(ev, db, cfg, started)):
            picked.append((ev, kind))
    limit = cfg.notify.max_alerts_per_run if max_alerts is None else max_alerts
    picked = picked[:limit]

    if picked:
        reviewer = reviewer_factory(settings, cfg.gemini, cfg.pricing, prices, db)
        if not ai:
            reviewer.disabled_reason = "ИИ выключен для этого прогона"
        kinds = {(ev.listing.source, ev.listing.id): kind for ev, kind in picked}
        reviews = await review_candidates([ev for ev, _ in picked], reviewer, db, prices, cfg.pricing,
                                          client.fetch_detail)
        report.reviews = reviews
        for rv in reviews:
            listing = rv.evaluation.listing
            key = (listing.source, listing.id)
            if not rv.notify:
                continue
            kind = kinds[key]
            text, summary = format_alert(kind, rv, cfg.pricing, today, drops.get(key) if kind == "drop" else None)
            if dry:
                report.previews.append(text)
                continue
            report.alerts.append(db.add_alert(listing.source, listing.id, listing.price, kind, text, summary,
                                              quiet, started))

    await _pc_alerts(settings, cfg, db, client, evaluations, prices, today, quiet, dry, ai, started, report,
                     drops, reviewer_factory)

    source = getattr(client, "source", "bazos")
    if not dry and source in HTTP_SOURCES:  # detail checks over plain HTTP: not FB
        await _gone_alerts(db, cfg, client, source, started, quiet, report)
        await learn.check_sold(db, client, cfg.learn, started)


def value_pc_listing(
    ev: Evaluation, specs: PcSpecs, comp: Components, prices: PriceList, cfg: AppConfig, today: date,
) -> PcResult:
    """Rules for a PC whose hardware is known: components -> market -> the usual evaluate()."""
    if not specs.is_pc:
        return PcResult(ev, specs, skipped="не готовый ПК (по Gemini)")
    valuation = value_pc(specs, comp, prices)
    if valuation.market is None:
        return PcResult(ev, specs, valuation, skipped="; ".join(valuation.flags))
    pc_ev = evaluate(ev.listing, prices, cfg.pricing, today, match=pc_match(specs, valuation, comp))
    pc_ev.flags += valuation.flags
    if specs.scam_risk == "high":
        pc_ev.is_candidate, pc_ev.score = False, 0
        return PcResult(pc_ev, specs, valuation, skipped="Gemini: высокий риск мошенничества")
    if not pc_ev.is_candidate:
        reasons = [f for f in pc_ev.flags if f in ("low_profit", "too_cheap", "scam_suspect", "shop")]
        return PcResult(pc_ev, specs, valuation, skipped=", ".join(reasons) or "цена выше 90 % оценки")
    return PcResult(pc_ev, specs, valuation)


def cached_pc_results(
    evaluations: list[Evaluation], db: Database, prices: PriceList, cfg: AppConfig, today: date,
) -> list[PcResult]:
    """PCs whose hardware is already in the DB, valued again without Gemini (for /top)."""
    comp = Components.load(cfg.pricing.components_path)
    out = []
    for ev in evaluations:
        if ev.match is None and is_pc_build(normalize(ev.listing.title)):
            row = db.get_pc_specs(ev.listing.source, ev.listing.id)
            if row:
                out.append(value_pc_listing(ev, PcSpecs.model_validate_json(row["response"]), comp, prices, cfg, today))
    return out


async def _pc_alerts(
    settings: Settings, cfg: AppConfig, db: Database, client: Any, evaluations: list[Evaluation],
    prices: PriceList, today: date, quiet: bool, dry: bool, ai: bool, started: datetime, report: RunReport,
    drops: dict[tuple[str, str], int], reviewer_factory: Callable[..., GeminiReviewer],
) -> None:
    """Ready-built PCs: Gemini reads the hardware (at most notify.pc_specs_per_run new ones), rules price it."""
    pcs = [ev for ev in evaluations if ev.match is None and ev.listing.price and is_pc_build(normalize(ev.listing.title))]
    if not pcs:
        return
    comp = Components.load(cfg.pricing.components_path)
    reviewer = reviewer_factory(settings, cfg.gemini, cfg.pricing, prices, db)
    if not ai:
        reviewer.disabled_reason = "ИИ выключен для этого прогона"
    budget = cfg.notify.pc_specs_per_run
    for ev in pcs:
        listing = ev.listing
        key = (listing.source, listing.id)
        kind = notify_kind(ev, db, cfg, started, require_candidate=False)
        if kind is None:
            continue
        if not db.get_pc_specs(*key):
            if budget <= 0:
                report.pcs.append(PcResult(ev, skipped="лимит Gemini для ПК на прогон, в следующий раз"))
                continue
            budget -= 1
            if listing.description is None:
                try:
                    detail = await client.fetch_detail(listing.url)
                except Exception as exc:  # noqa: BLE001 - one broken detail page must not stop the run
                    log.warning("detail %s failed: %s", listing.url, exc)
                    continue
                listing.description = detail.description or ""
                db.set_description(*key, listing.description)
        try:
            specs = await reviewer.extract_pc(listing)
        except NoAI as exc:
            report.pcs.append(PcResult(ev, skipped=f"без ИИ железо не прочитать: {exc}"))
            continue
        result = value_pc_listing(evaluate(listing, prices, cfg.pricing, today), specs, comp, prices, cfg, today)
        report.pcs.append(result)
        if result.skipped:
            continue
        text, summary = format_pc_alert(kind, result.evaluation, specs, result.valuation, today,
                                        drops.get(key) if kind == "drop" else None)
        if dry:
            report.previews.append(text)
            continue
        report.alerts.append(db.add_alert(*key, listing.price, kind, text, summary, quiet, started))


def _watch_alerts(
    db: Database, cfg: AppConfig, by_key: dict[tuple[str, str], Evaluation], today: date, quiet: bool,
    report: RunReport, alerted: set[tuple[str, str]], now: datetime,
) -> None:
    for key, base in db.watches().items():
        ev = by_key.get(key)
        if ev is None or ev.listing.price is None or ev.match is None:
            continue
        price = ev.listing.price
        if price > base * (100 - cfg.notify.watch_drop_pct) / 100:
            continue
        row = db.get(*key)
        if row and row["status"] == Status.HIDDEN:
            continue
        cached = db.get_review(*key, price)
        review = Review(ev, no_ai_reason="наблюдение за ценой, Gemini не спрашивали")
        if cached:
            review = Review(ev, Verdict.model_validate_json(cached["response"]), cached["model"], cached=True)
        text, summary = format_alert("watch", review, cfg.pricing, today, base)
        report.alerts.append(db.add_alert(*key, price, "watch", text, summary, quiet, now))
        db.set_watch(*key, price, now)  # next alert after another drop from here
        alerted.add(key)


async def _gone_alerts(
    db: Database, cfg: AppConfig, client: Any, source: str, started: datetime, quiet: bool, report: RunReport,
) -> None:
    rows = db.to_check_gone(
        seen_before=started, checked_before=started - timedelta(minutes=cfg.notify.gone_check_every_min),
        limit=cfg.notify.gone_checks_per_run, source=source,
    )
    for row in rows:
        try:
            await client.get(row["url"])
        except BazosError as exc:
            if exc.status in (404, 410):
                db.mark_checked(row["source"], row["id"], gone=True, now=started)
                text, summary = format_gone(row["title"], row["price"], row["url"])
                report.alerts.append(db.add_alert(row["source"], row["id"], row["price"], "gone", text, summary,
                                                  quiet, started))
                continue
            log.warning("gone check %s: %s", row["url"], exc)
        db.mark_checked(row["source"], row["id"], gone=False, now=started)

