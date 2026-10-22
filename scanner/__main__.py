"""CLI entry point: `python -m scanner <command>`."""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import re
from datetime import datetime, timedelta

from rich.console import Console
from rich.table import Table

from scanner import learn
from scanner.bot import ScannerApp, send_pending
from scanner.browser_setup import BrowserSetupError, ensure_chromium, use_browsers_dir
from scanner.config import PROJECT_ROOT, Settings, load_config
from scanner.db import Database
from scanner.fb_worker import run_worker
from scanner.gemini import GeminiReviewer, Review, Verdict, review_candidates
from scanner.logs import setup_logging
from scanner.pc import breakdown_lines
from scanner.pipeline import PcResult, run_scan
from scanner.pricing import Evaluation, PriceList, evaluate_many, rank
from scanner.sources.bazos import BazosClient, build_search_url, parse_search_page
from scanner.sources.facebook import FacebookClient

log = logging.getLogger("scanner")
console = Console()

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


async def run_once(
    settings: Settings, dry: bool, show_all: bool, ai: bool, ai_limit: int | None, max_alerts: int | None,
    source: str = "bazos",
) -> None:
    """One scan through the pipeline; without --dry the alerts go to Telegram right away."""
    cfg = load_config(settings.config_path)
    db = Database(settings.db_path)
    try:
        async with make_client(settings, cfg, source) as client:
            limit = max_alerts if max_alerts is not None else ai_limit
            report = await run_scan(settings, cfg, db, client, ai=ai, max_alerts=limit, dry=dry)
        _print_evaluations(report.evaluations, report.labels, show_all)
        console.print(
            f"Fetched {report.listings} unique listings: [green]{report.new} new[/], "
            f"[yellow]{report.price_changed} price changed[/], "
            f"{report.listings - report.new - report.price_changed} unchanged; "
            f"{report.candidates} candidates."
        )
        for err in report.errors:
            console.print(f"[red]error:[/] {err}")
        if report.reviews:
            reviewer = GeminiReviewer(settings, cfg.gemini, cfg.pricing, PriceList([]), db)
            _print_reviews(report.reviews, reviewer, as_json=False)
        _print_pcs(report.pcs)
        if dry:
            for text in report.previews:
                console.rule("[dim]would send[/]")
                console.print(text, markup=False, highlight=False)
            console.print(f"[dim]--dry: {len(report.previews)} alerts not sent[/]")
        else:
            sent = await send_pending(settings, db)
            console.print(f"Telegram: {sent} messages sent ({len(report.alerts)} new alerts)")
    finally:
        db.close()


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


def _print_pcs(pcs: list[PcResult]) -> None:
    """Ready-built PCs of this run: the component calculation or why they were not valued."""
    for r in pcs:
        l, ev = r.evaluation.listing, r.evaluation
        console.rule(f"[bold]{l.title}[/] — {_fmt_price(l.price)} Kč", align="left")
        console.print(f"[dim]{l.url}[/]")
        if r.valuation and r.valuation.market is not None:
            for line in breakdown_lines(r.valuation):
                console.print("  " + line, markup=False)
            console.print(f"  Рынок {_fmt_price(ev.market_price)} · max {_fmt_price(ev.max_price)} · "
                          f"offer {_fmt_price(ev.offer_price)} · score {ev.score}", markup=False)
        if r.valuation and r.valuation.market is not None and r.valuation.flags:
            console.print("  флаги: " + "; ".join(r.valuation.flags), markup=False)
        console.print(f"  [yellow]{r.skipped}[/]" if r.skipped else "  [green]кандидат[/]")


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


async def fb_login(settings: Settings) -> None:
    """A visible browser on Marketplace; you log in by hand. The session stays in the profile and is
    also exported to fb_state.json, which you send to the bot when it runs on a server."""
    cfg = load_config(settings.config_path)
    try:
        # Downloads Chromium (~150 MB, a few minutes) if this PC has none yet; the progress bar is shown.
        if done := await ensure_chromium(settings, show=True):
            console.print("; ".join(done))
    except BrowserSetupError as exc:
        console.print(f"[red]{exc}[/]\nTry: python -m playwright install chromium")
        return
    export = PROJECT_ROOT / "fb_state.json"
    # The installed Google Chrome first: on a Mac the bundled "Chrome for Testing" may be closed by the
    # system right away, and FB sees a regular browser. The session file works with Chromium on a server.
    attempts = [("Google Chrome", "chrome", settings.fb_profile_dir.with_name("fb_profile_chrome")),
                ("Chromium (Playwright)", None, settings.fb_profile_dir)]
    ok, errors = None, []
    for name, channel, profile in attempts:
        console.print(f"Opening {name}. Log in to Facebook (2FA if asked); the window closes by itself once you "
                      "are in. Nothing is typed for you.")
        try:
            async with FacebookClient(cfg, profile, settings.fb_browser_path, headless=False, channel=channel) as fb:
                ok = await fb.login_interactively()
                if ok:
                    await fb.export_state(export)
            break
        except Exception as exc:  # noqa: BLE001 - not installed / closed right away: try the next browser
            errors.append(f"{name}: {str(exc).splitlines()[0][:200]}")
            console.print(f"[yellow]{name} did not work: {errors[-1]}[/]")
    if ok is None:
        console.print("[red]No browser could be opened.[/] Details: DEBUG=pw:browser python -m scanner fb-login")
        return
    if ok:
        settings.fb_state_path.parent.mkdir(parents=True, exist_ok=True)
        settings.fb_state_path.write_bytes(export.read_bytes())  # local FB runs import it too
        db = Database(settings.db_path)
        db.set_state("fb_blocked", "0")  # the bot on this machine may use FB again
        db.close()
        console.print(f"[green]Logged in.[/] Bot on a server: send [bold]{export}[/] to the bot in Telegram "
                      "(it deletes the message after importing). It is your FB session: do not share it.")
        console.print("Check here: python -m scanner run-once --source fb --dry")
    else:
        console.print("[yellow]No login detected (window closed or 10 min passed). Run fb-login again.[/]")


def make_client(settings: Settings, cfg, source: str, headless: bool | None = None):
    if source == "fb":
        use_browsers_dir(settings)
        return FacebookClient(cfg, settings.fb_profile_dir, settings.fb_browser_path, headless=headless,
                              state_path=settings.fb_state_path)
    return BazosClient(cfg.bazos)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scanner")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run-once", help="run all searches once")
    p_run.add_argument("--dry", action="store_true", help="no notifications, only print to console")
    p_run.add_argument("--source", choices=["bazos", "fb"], default="bazos",
                       help="fb: Facebook Marketplace (log in first with fb-login)")
    sub.add_parser("fb-login", help="open a visible browser and log in to Facebook by hand (saved in fb_profile/)")
    sub.add_parser("fb-worker", help="Facebook only, no Telegram polling: for a home PC next to the hosted bot")
    p_run.add_argument("--all", action="store_true", help="also show matched listings that are not candidates")
    p_run.add_argument("--no-ai", action="store_true", help="skip the Gemini second opinion")
    p_run.add_argument("--ai-limit", type=int, default=None, help="Gemini for at most N top candidates")
    p_run.add_argument("--max-alerts", type=int, default=None,
                       help="at most N candidates reviewed / alerted (default: notify.max_alerts_per_run)")

    sub.add_parser("bot", help="Telegram bot + scheduler (Bazoš every 10 min)")

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
    sub.add_parser("price-report", help="price list proposals from the observations (what /prices shows)")

    args = parser.parse_args()
    settings = Settings()
    # Files only for the commands that run unattended or touch the network.
    log_dir = settings.log_dir if args.command in ("bot", "run-once", "fb-worker") else None
    setup_logging("DEBUG" if args.verbose else settings.log_level, log_dir, console)

    if args.command == "run-once":
        asyncio.run(run_once(settings, args.dry, args.all, ai=not args.no_ai, ai_limit=args.ai_limit,
                             max_alerts=args.max_alerts, source=args.source))
    elif args.command == "fb-login":
        asyncio.run(fb_login(settings))
    elif args.command == "fb-worker":
        asyncio.run(run_worker(settings))
    elif args.command == "bot":
        asyncio.run(ScannerApp(settings).run())
    elif args.command == "review":
        asyncio.run(review_db(settings, days=args.days, limit=args.limit, ids=args.ids, as_json=args.json, refresh=args.refresh))
    elif args.command == "reviews":
        show_reviews(settings, hours=args.hours, skipped=args.skipped)
    elif args.command == "evaluate":
        evaluate_db(settings, days=args.days, show_all=args.all)
    elif args.command == "price-report":
        db = Database(settings.db_path)
        try:
            cfg = load_config(settings.config_path)
            report = learn.build_report(db, PriceList.load(cfg.pricing.prices_path), cfg.learn, datetime.now())
            text = re.sub(r"<[^>]+>", "", learn.format_report(report, cfg.learn, limit=100))
            console.print(html.unescape(text), markup=False)
        finally:
            db.close()
    elif args.command == "save-fixtures":
        asyncio.run(save_fixtures(settings))


if __name__ == "__main__":
    main()
