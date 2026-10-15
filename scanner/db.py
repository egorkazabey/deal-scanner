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

    def price_history(self, source: str, listing_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT price, seen_at FROM price_history WHERE source = ? AND listing_id = ? ORDER BY id",
            (source, listing_id),
        ).fetchall()


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None
