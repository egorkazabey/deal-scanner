"""CLI entry point: `python -m scanner <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from scanner.config import PROJECT_ROOT, Settings, load_config
from scanner.db import Database, UpsertKind
from scanner.models import Listing
from scanner.sources.bazos import BazosClient, build_search_url, parse_search_page

log = logging.getLogger("scanner")
console = Console()

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


async def run_once(settings: Settings, dry: bool, show_all: bool) -> None:
    cfg = load_config(settings.config_path)
    db = Database(settings.db_path)
    try:
        async with BazosClient(cfg.bazos) as client:
            result = await client.scan()

        rows: list[tuple[str, Listing]] = []
        counts = {kind: 0 for kind in UpsertKind}
        for listing in result.listings:
            upsert = db.upsert(listing)
            counts[upsert.kind] += 1
            if upsert.kind is UpsertKind.NEW:
                rows.append(("NEW", listing))
            elif upsert.kind is UpsertKind.PRICE_CHANGED:
                rows.append((f"{_fmt_price(upsert.old_price)} →", listing))
            elif show_all:
                rows.append(("", listing))
    finally:
        db.close()

    _print_table(rows)
    console.print(
        f"Fetched {len(result.listings)} unique listings: "
        f"[green]{counts[UpsertKind.NEW]} new[/], "
        f"[yellow]{counts[UpsertKind.PRICE_CHANGED]} price changed[/], "
        f"{counts[UpsertKind.UNCHANGED]} unchanged."
    )
    for err in result.errors:
        console.print(f"[red]error:[/] {err}")
    if not dry:
        log.info("Notifications are not implemented yet (Telegram comes in step 4)")


def _print_table(rows: list[tuple[str, Listing]]) -> None:
    if not rows:
        return
    table = Table(show_lines=False, header_style="bold")
    table.add_column("", style="green", no_wrap=True)
    table.add_column("Price", justify="right", no_wrap=True)
    table.add_column("Title", overflow="fold", max_width=60)
    table.add_column("Location", no_wrap=True)
    table.add_column("Date", no_wrap=True)
    table.add_column("URL", overflow="fold", style="dim")
    for label, l in sorted(rows, key=lambda r: (r[1].posted_date or date.min), reverse=True):
        table.add_row(
            label,
            _fmt_price(l.price),
            ("[magenta]TOP[/] " if l.is_top else "") + l.title,
            l.location or "",
            l.posted_date.strftime("%d.%m.") if l.posted_date else "",
            l.url,
        )
    console.print(table)


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
    p_run.add_argument("--all", action="store_true", help="also show already known unchanged listings")

    sub.add_parser("save-fixtures", help="save real Bazoš HTML into tests/fixtures")

    args = parser.parse_args()
    settings = Settings()
    logging.basicConfig(
        level="DEBUG" if args.verbose else settings.log_level,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.command == "run-once":
        asyncio.run(run_once(settings, dry=args.dry, show_all=args.all))
    elif args.command == "save-fixtures":
        asyncio.run(save_fixtures(settings))


if __name__ == "__main__":
    main()
