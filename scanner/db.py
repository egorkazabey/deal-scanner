"""SQLite storage: listings + price history."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from scanner.models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source       TEXT NOT NULL,
    id           TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    price        INTEGER,
    location     TEXT,
    postal_code  TEXT,
    posted_date  TEXT,
    description  TEXT,
    is_top       INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new', 'alerted', 'hidden', 'contacted')),
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (source, id)
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    price       INTEGER,
    seen_at     TEXT NOT NULL,
    FOREIGN KEY (source, listing_id) REFERENCES listings (source, id)
);

CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history (source, listing_id);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings (last_seen);

-- Gemini second opinion, cached once per listing and price (a price change gets a new review).
CREATE TABLE IF NOT EXISTS gemini_reviews (
    source      TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    price       INTEGER NOT NULL,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,  -- verdict JSON as returned by Gemini
    verdict     TEXT NOT NULL,  -- buy / negotiate / skip
    scam_risk   TEXT NOT NULL,  -- low / medium / high
    notify      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (source, listing_id, price)
);

-- Requests and tokens per quota day (in GEMINI_QUOTA_TZ) and model.
CREATE TABLE IF NOT EXISTS gemini_usage (
    day            TEXT NOT NULL,
    model          TEXT NOT NULL,
    requests       INTEGER NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0,
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    total_tokens   INTEGER NOT NULL DEFAULT 0,
    exhausted      INTEGER NOT NULL DEFAULT 0,  -- the API answered "daily quota exceeded"
    PRIMARY KEY (day, model)
);
"""


class Status(StrEnum):
    NEW = "new"
    ALERTED = "alerted"
    HIDDEN = "hidden"
    CONTACTED = "contacted"


class UpsertKind(StrEnum):
    NEW = "new"
    PRICE_CHANGED = "price_changed"
    UNCHANGED = "unchanged"


@dataclass
class UpsertResult:
    kind: UpsertKind
    old_price: int | None = None


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def upsert(self, listing: Listing, now: datetime | None = None) -> UpsertResult:
        """Insert or refresh a listing; records price history on insert and on price change."""
        ts = (now or listing.last_seen or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            row = self.conn.execute(
                "SELECT price FROM listings WHERE source = ? AND id = ?", (listing.source, listing.id)
            ).fetchone()

            if row is None:
                self.conn.execute(
                    """INSERT INTO listings (source, id, url, title, price, location, postal_code,
                           posted_date, description, is_top, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        listing.source, listing.id, listing.url, listing.title, listing.price,
                        listing.location, listing.postal_code, _iso(listing.posted_date),
                        listing.description, int(listing.is_top), ts, ts,
                    ),
                )
                self._add_price(listing, ts)
                return UpsertResult(UpsertKind.NEW)

            self.conn.execute(
                """UPDATE listings SET url = ?, title = ?, price = ?, location = ?, postal_code = ?,
                       posted_date = COALESCE(?, posted_date), description = COALESCE(?, description),
                       is_top = ?, last_seen = ?
                   WHERE source = ? AND id = ?""",
                (
                    listing.url, listing.title, listing.price, listing.location, listing.postal_code,
                    _iso(listing.posted_date), listing.description, int(listing.is_top), ts,
                    listing.source, listing.id,
                ),
            )
            old_price = row["price"]
            if old_price != listing.price:
                self._add_price(listing, ts)
                return UpsertResult(UpsertKind.PRICE_CHANGED, old_price)
            return UpsertResult(UpsertKind.UNCHANGED, old_price)

    def _add_price(self, listing: Listing, ts: str) -> None:
        self.conn.execute(
            "INSERT INTO price_history (source, listing_id, price, seen_at) VALUES (?, ?, ?, ?)",
            (listing.source, listing.id, listing.price, ts),
        )

    def set_status(self, source: str, listing_id: str, status: Status) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE listings SET status = ? WHERE source = ? AND id = ?",
                (Status(status).value, source, listing_id),
            )

    def get(self, source: str, listing_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND id = ?", (source, listing_id)
        ).fetchone()

    def listings(self, seen_since: datetime | None = None) -> list[Listing]:
        """Stored listings (optionally only those seen since a moment), for offline re-evaluation."""
        sql, args = "SELECT * FROM listings", ()
        if seen_since:
            sql, args = sql + " WHERE last_seen >= ?", (seen_since.isoformat(timespec="seconds"),)
        return [_row_to_listing(r) for r in self.conn.execute(sql, args).fetchall()]

    def price_history(self, source: str, listing_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT price, seen_at FROM price_history WHERE source = ? AND listing_id = ? ORDER BY id",
            (source, listing_id),
        ).fetchall()

    def set_description(self, source: str, listing_id: str, description: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE listings SET description = ? WHERE source = ? AND id = ?",
                (description, source, listing_id),
            )

    # --- Gemini ---

    def get_review(self, source: str, listing_id: str, price: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM gemini_reviews WHERE source = ? AND listing_id = ? AND price = ?",
            (source, listing_id, price),
        ).fetchone()

    def save_review(
        self, source: str, listing_id: str, price: int, model: str, response: str,
        verdict: str, scam_risk: str, notify: bool, now: datetime | None = None,
    ) -> None:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO gemini_reviews
                       (source, listing_id, price, model, response, verdict, scam_risk, notify, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, listing_id, price, model, response, verdict, scam_risk, int(notify), ts),
            )

    def reviews(self, since: datetime, only_hidden: bool = False) -> list[sqlite3.Row]:
        """Stored reviews joined with their listing, newest first."""
        sql = """SELECT r.*, l.title, l.url, l.location, l.price AS current_price
                 FROM gemini_reviews r JOIN listings l ON l.source = r.source AND l.id = r.listing_id
                 WHERE r.created_at >= ?"""
        if only_hidden:
            sql += " AND r.notify = 0"
        return self.conn.execute(
            sql + " ORDER BY r.created_at DESC", (since.isoformat(timespec="seconds"),)
        ).fetchall()

    def add_gemini_usage(
        self, day: str, model: str, *, requests: int = 0, errors: int = 0,
        prompt_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO gemini_usage (day, model, requests, errors, prompt_tokens, output_tokens,
                       total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (day, model) DO UPDATE SET
                       requests = requests + excluded.requests, errors = errors + excluded.errors,
                       prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                       output_tokens = output_tokens + excluded.output_tokens,
                       total_tokens = total_tokens + excluded.total_tokens""",
                (day, model, requests, errors, prompt_tokens, output_tokens, total_tokens),
            )

    def mark_gemini_exhausted(self, day: str, model: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO gemini_usage (day, model, exhausted) VALUES (?, ?, 1)
                   ON CONFLICT (day, model) DO UPDATE SET exhausted = 1""",
                (day, model),
            )

    def gemini_usage(self, day: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM gemini_usage WHERE day = ?", (day,)).fetchall()
        return {r["model"]: r for r in rows}


def _row_to_listing(r: sqlite3.Row) -> Listing:
    return Listing(
        source=r["source"],
        id=r["id"],
        url=r["url"],
        title=r["title"],
        price=r["price"],
        location=r["location"],
        posted_date=date.fromisoformat(r["posted_date"]) if r["posted_date"] else None,
        description=r["description"],
        postal_code=r["postal_code"],
        is_top=bool(r["is_top"]),
        first_seen=datetime.fromisoformat(r["first_seen"]),
        last_seen=datetime.fromisoformat(r["last_seen"]),
    )


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None
