"""CLI entry point: `python -m scanner <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from scanner.config import PROJECT_ROOT, Settings, load_config
from scanner.db import Database, UpsertKind
from scanner.gemini import GeminiReviewer, Review, Verdict, review_candidates
from scanner.pricing import Evaluation, PriceList, evaluate_many, rank
from scanner.sources.bazos import BazosClient, build_search_url, parse_search_page

log = logging.getLogger("scanner")
console = Console()

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


async def run_once(settings: Settings, dry: bool, show_all: bool, ai: bool, ai_limit: int | None) -> None:
    cfg = load_config(settings.config_path)
    prices = PriceList.load(cfg.pricing.prices_path)
    db = Database(settings.db_path)
    try:
        async with BazosClient(cfg.bazos) as client:
            result = await client.scan()

            labels: dict[tuple[str, str], str] = {}
            counts = {kind: 0 for kind in UpsertKind}
            for listing in result.listings:
                upsert = db.upsert(listing)
                counts[upsert.kind] += 1
                if upsert.kind is UpsertKind.NEW:
                    labels[(listing.source, listing.id)] = "NEW"
                elif upsert.kind is UpsertKind.PRICE_CHANGED:
                    labels[(listing.source, listing.id)] = f"{_fmt_price(upsert.old_price)} →"
                # Keep the description loaded earlier, so the rules see it and no detail is refetched.
                if listing.description is None and (row := db.get(listing.source, listing.id)):
                    listing.description = row["description"]

            evaluations = evaluate_many(result.listings, prices, cfg.pricing)
            _print_evaluations(evaluations, labels, show_all)
            console.print(
                f"Fetched {len(result.listings)} unique listings: "
                f"[green]{counts[UpsertKind.NEW]} new[/], "
                f"[yellow]{counts[UpsertKind.PRICE_CHANGED]} price changed[/], "
                f"{counts[UpsertKind.UNCHANGED]} unchanged."
            )
            for err in result.errors:
                console.print(f"[red]error:[/] {err}")

            if ai:
                candidates = rank(evaluations)[:ai_limit]
                reviewer = GeminiReviewer(settings, cfg.gemini, cfg.pricing, prices, db)
                reviews = await review_candidates(
                    candidates, reviewer, db, prices, cfg.pricing, client.fetch_detail
                )
                _print_reviews(reviews, reviewer, as_json=False)
    finally:
        db.close()
    if not dry:
        log.info("Notifications are not implemented yet (Telegram comes in step 4)")


async def review_db(
    settings: Settings, days: float, limit: int, ids: list[str], as_json: bool, refresh: bool
) -> None:
    """Gemini for the given listing ids plus the top `limit` other candidates already in the DB."""
    cfg = load_config(settings.config_path)
    prices = PriceList.load(cfg.pricing.prices_path)
    db = Database(settings.db_path)
    try:
        listings = db.listings(seen_since=datetime.now() - timedelta(days=days))
        evaluations = evaluate_many(listings, prices, cfg.pricing)
        by_id = {e.listing.id: e for e in evaluations}
        for missing in [i for i in ids if i not in by_id]:
            console.print(f"[yellow]{missing}: not in the DB (seen in the last {days:g} days)[/]")
        picked = [by_id[i] for i in ids if i in by_id]
        for e in picked:
            if not e.is_candidate:
                flags = e.flags + [f"stop:{x}" for x in e.stop] + e.scam_reasons
                console.print(f"[dim]{e.listing.id} {e.listing.title}: not a candidate by rules ({', '.join(flags) or '> 90 %'})[/]")
        picked = [e for e in picked if e.is_candidate]
        picked += [e for e in rank(evaluations) if e.listing.id not in ids][:limit]

        if refresh:  # e.g. after changing the rules: ask again instead of using the cache
            for e in picked:
                db.delete_review(e.listing.source, e.listing.id)
        reviewer = GeminiReviewer(settings, cfg.gemini, cfg.pricing, prices, db)
        async with BazosClient(cfg.bazos) as client:
            reviews = await review_candidates(picked, reviewer, db, prices, cfg.pricing, client.fetch_detail)
        _print_reviews(reviews, reviewer, as_json)
    finally:
        db.close()


def show_reviews(settings: Settings, hours: float, skipped: bool) -> None:
    """Stored Gemini reviews (`skipped`: only those not sent) and logged pricing mismatches."""
    cfg = load_config(settings.config_path)
    prices = PriceList.load(cfg.pricing.prices_path)
    db = Database(settings.db_path)
    try:
        since = datetime.now() - timedelta(hours=hours)
        table = Table(title="Gemini: не отправлено" if skipped else "Gemini reviews", header_style="bold")
        for col in ("When", "Verdict", "Scam", "Price", "Adj. market", "Model", "Title", "Reason", "URL"):
            table.add_column(col, overflow="fold")
        for r in db.reviews(since, only_hidden=skipped):
            v = Verdict.model_validate_json(r["response"])
            table.add_row(
                r["created_at"][5:16].replace("T", " "), v.verdict, v.scam_risk, _fmt_price(r["price"]),
                _fmt_price(v.adjusted_market_price), r["model"], r["title"], v.reason_ru, r["url"],
            )
        console.print(table)

        mismatches = db.mismatches(since)
        if mismatches:
            mt = Table(title="pricing.py vs Gemini (to fix pricing.py)", header_style="bold")
            for col in ("Title", "Rules item", "Gemini says", "Gemini item", "Outcome"):
                mt.add_column(col, overflow="fold")
            for m in mismatches:
                mt.add_row(m["title"], m["rules_item"], m["gemini_model"], m["gemini_item"] or "—", m["outcome"])
            console.print(mt)
        _print_usage(GeminiReviewer(settings, cfg.gemini, cfg.pricing, prices, db))
    finally:
        db.close()


def _print_reviews(reviews: list[Review], reviewer: GeminiReviewer, as_json: bool) -> None:
    table = Table(title="Gemini second opinion", header_style="bold")
    for col, kw in [
        ("Model (Gemini)", {"max_width": 30}), ("Price", {"justify": "right"}), ("Verdict", {}),
        ("Offer", {"justify": "right"}), ("Max", {"justify": "right"}), ("Adj. market", {"justify": "right"}),
        ("Flags", {"max_width": 46}), ("Title", {"max_width": 34}),
    ]:
        table.add_column(col, overflow="fold", **kw)
    for rv in reviews:
        l, ev, v = rv.evaluation.listing, rv.evaluation, rv.verdict
        rules_flags = [f for f in ev.flags if f != "low_profit"] + [f"stop:{x}" for x in ev.stop] + ev.scam_reasons
        if v is None:
            verdict = "[dim]rules ✗[/]" if rv.rules_skip else "[yellow]без ИИ[/]"
            note = rv.rules_skip or rv.no_ai_reason or ""
            table.add_row(ev.match.item.label if ev.match else "", _fmt_price(l.price), verdict, "", "",
                          _fmt_price(ev.market_price), note, l.title)
            continue
        color = {"buy": "green", "negotiate": "yellow", "skip": "red"}[v.verdict]
        flags = rules_flags + v.red_flags + ([f"scam:{v.scam_risk}"] if v.scam_risk != "low" else [])
        if rv.rematched:
            flags.append(f"rematch: {rv.rematched}")
        table.add_row(
            f"{v.model} {v.variant}".strip(), _fmt_price(l.price),
            f"[{color}]{v.verdict}[/]" + ("" if rv.notify else " [dim](не шлём)[/]"),
            _fmt_price(v.offer_price), _fmt_price(v.max_price), _fmt_price(v.adjusted_market_price),
            "; ".join(dict.fromkeys(flags)), l.title,
        )
    console.print(table)
    if as_json:
        for rv in reviews:
            if rv.verdict:
                console.print(f"[bold]{rv.evaluation.listing.title}[/]  [dim]{rv.evaluation.listing.url}[/]")
                console.print_json(rv.verdict.model_dump_json())
    _print_usage(reviewer)


def _print_usage(reviewer: GeminiReviewer) -> None:
    for model, u in reviewer.usage().items():
        console.print(
            f"[dim]Gemini {model} ({reviewer.day()}): {u['requests']} req, {u['left']} left of "
            f"{reviewer.settings.gemini_rpd}, tokens {u['prompt_tokens']} in / {u['output_tokens']} out, "
            f"{u['errors']} errors[/]"
        )


def evaluate_db(settings: Settings, days: float, show_all: bool) -> None:
    """Re-evaluate listings already in the DB (no network), e.g. after editing prices.json."""
    cfg = load_config(settings.config_path)
    prices = PriceList.load(cfg.pricing.prices_path)
    db = Database(settings.db_path)
    try:
        listings = db.listings(seen_since=datetime.now() - timedelta(days=days))
    finally:
        db.close()
    _print_evaluations(evaluate_many(listings, prices, cfg.pricing), {}, show_all)
    console.print(f"Evaluated {len(listings)} listings seen in the last {days:g} days.")


def _print_evaluations(
    evaluations: list[Evaluation], labels: dict[tuple[str, str], str], show_all: bool
) -> None:
    candidates = rank(evaluations)
    matched = [e for e in evaluations if e.match]
    console.print(
        f"Price list match: {len(matched)}/{len(evaluations)}, candidates: [bold]{len(candidates)}[/]"
    )
    if candidates:
        console.print(_eval_table("Candidates", candidates, labels))
    if show_all:
        rest = sorted(
            (e for e in matched if not e.is_candidate),
            key=lambda e: e.discount_pct if e.discount_pct is not None else -9,
            reverse=True,
        )
        if rest:
            console.print(_eval_table("Matched, not candidates", rest, labels))


def _eval_table(title: str, evaluations: list[Evaluation], labels: dict[tuple[str, str], str]) -> Table:
    table = Table(title=title, header_style="bold", show_lines=False)
    table.add_column("", style="green", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Price", justify="right", no_wrap=True)
    table.add_column("Market / max", justify="right", no_wrap=True)
    table.add_column("Disc", justify="right")
    table.add_column("Profit", justify="right")
    table.add_column("Price list item", overflow="fold", max_width=28)
    table.add_column("Title", overflow="fold", max_width=40)
    table.add_column("Location", no_wrap=True)
    table.add_column("Date", no_wrap=True)
    table.add_column("Flags", overflow="fold", max_width=24, style="yellow")
    table.add_column("URL", overflow="fold", style="dim")
    for e in evaluations:
        l, m = e.listing, e.match
        item = f"{m.item.label}" + (" [red](low)[/]" if m.confidence == "low" else "") if m else ""
        if m and m.item.confidence == "estimate":
            item += " [dim]est.[/]"
        flags = e.flags + [f"stop:{s}" for s in e.stop] + e.scam_reasons
        table.add_row(
            labels.get((l.source, l.id), ""),
            str(e.score) if e.is_candidate else "",
            _fmt_price(l.price),
            f"{_fmt_price(e.market_price)} / {_fmt_price(e.max_price)}",
            f"{e.discount_pct:.0%}" if e.discount_pct is not None else "",
            _fmt_price(e.expected_profit),
            item,
            ("[magenta]TOP[/] " if l.is_top else "") + l.title,
            l.location or "",
            l.posted_date.strftime("%d.%m.") if l.posted_date else "",
            ", ".join(flags),
            l.url,
        )
    return table


def _fmt_price(price: int | None) -> str:
    return "—" if price is None else f"{price:,}".replace(",", " ")


async def save_fixtures(settings: Settings) -> None:
    """Save real HTML (1 query search page, 1 category page, 1 detail) for parser tests."""
    cfg = load_config(settings.config_path)
    query_search = next(s for s in cfg.bazos.searches if s.query)
    category_search = next(s for s in cfg.bazos.searches if s.category)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    async with BazosClient(cfg.bazos) as client:
        url = build_search_url(query_search, cfg.bazos)
        html = await client.get(url)
        (FIXTURES_DIR / "bazos_search_query.html").write_text(html, encoding="utf-8")
        listings, _ = parse_search_page(html, url)

        html = await client.get(build_search_url(category_search, cfg.bazos))
        (FIXTURES_DIR / "bazos_search_category.html").write_text(html, encoding="utf-8")

        target = next((l for l in listings if not l.is_top), listings[0])
        html = await client.get(target.url)
        (FIXTURES_DIR / "bazos_detail.html").write_text(html, encoding="utf-8")
    console.print(f"Saved fixtures to {FIXTURES_DIR} (detail: {target.url})")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scanner")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run-once", help="run all searches once")
    p_run.add_argument("--dry", action="store_true", help="no notifications, only print to console")
    p_run.add_argument("--source", choices=["bazos"], default="bazos")
    p_run.add_argument("--all", action="store_true", help="also show matched listings that are not candidates")
    p_run.add_argument("--no-ai", action="store_true", help="skip the Gemini second opinion")
    p_run.add_argument("--ai-limit", type=int, default=None, help="Gemini for at most N top candidates")

    p_review = sub.add_parser("review", help="Gemini second opinion for top candidates from the DB")
    p_review.add_argument("--limit", type=int, default=5)
    p_review.add_argument("--days", type=float, default=2, help="listings seen in the last N days")
    p_review.add_argument("--ids", nargs="*", default=[], help="listing ids to review first (Bazoš id)")
    p_review.add_argument("--json", action="store_true", help="print the full Gemini JSON")
    p_review.add_argument("--refresh", action="store_true", help="ignore cached reviews of these listings")

    p_reviews = sub.add_parser("reviews", help="stored Gemini reviews and today's Gemini usage")
    p_reviews.add_argument("--hours", type=float, default=24)
    p_reviews.add_argument("--skipped", action="store_true", help="only reviews that were not sent")

    p_eval = sub.add_parser("evaluate", help="re-evaluate listings from the DB without network")
    p_eval.add_argument("--days", type=float, default=2, help="listings seen in the last N days")
    p_eval.add_argument("--all", action="store_true", help="also show matched listings that are not candidates")

    sub.add_parser("save-fixtures", help="save real Bazoš HTML into tests/fixtures")

    args = parser.parse_args()
    settings = Settings()
    logging.basicConfig(
        level="DEBUG" if args.verbose else settings.log_level,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)

    if args.command == "run-once":
        asyncio.run(run_once(settings, args.dry, args.all, ai=not args.no_ai, ai_limit=args.ai_limit))
    elif args.command == "review":
        asyncio.run(review_db(settings, days=args.days, limit=args.limit, ids=args.ids, as_json=args.json, refresh=args.refresh))
    elif args.command == "reviews":
        show_reviews(settings, hours=args.hours, skipped=args.skipped)
    elif args.command == "evaluate":
        evaluate_db(settings, days=args.days, show_all=args.all)
    elif args.command == "save-fixtures":
        asyncio.run(save_fixtures(settings))


if __name__ == "__main__":
    main()
