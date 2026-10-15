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

from scanner.config import AppConfig, Settings
from scanner.db import Database, Status, UpsertKind
from scanner.gemini import PRAGUE, GeminiReviewer, Review, Verdict, review_candidates
from scanner.notify import format_alert, format_gone, kc
from scanner.pricing import Evaluation, PriceList, evaluate_many, rank
from scanner.sources.bazos import BazosError

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


def notify_kind(ev: Evaluation, db: Database, cfg: AppConfig, now: datetime) -> str | None:
    """'deal' / 'drop' if the candidate may go to Telegram now, else None."""
    listing = ev.listing
    if not ev.is_candidate or listing.price is None or "stale" in ev.flags:
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
    run_id = db.start_run(started)
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

    if not dry:
        await _gone_alerts(db, cfg, client, started, quiet, report)


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
    db: Database, cfg: AppConfig, client: Any, started: datetime, quiet: bool, report: RunReport,
) -> None:
    rows = db.to_check_gone(
        seen_before=started, checked_before=started - timedelta(minutes=cfg.notify.gone_check_every_min),
        limit=cfg.notify.gone_checks_per_run,
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

