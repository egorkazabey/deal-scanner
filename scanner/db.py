"""SQLite storage: listings + price history."""

from __future__ import annotations

import json
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
    extra_cost   INTEGER NOT NULL DEFAULT 0,
    foreign_seller INTEGER NOT NULL DEFAULT 0,
    brand        TEXT,
    size         TEXT,
    condition    TEXT,
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
    rematched   TEXT,           -- "rules item → Gemini's item (outcome)" when Gemini disagreed
    created_at  TEXT NOT NULL,
    PRIMARY KEY (source, listing_id, price)
);

-- Listings where Gemini says pricing.py picked the wrong price list item (to fix pricing.py).
CREATE TABLE IF NOT EXISTS pricing_mismatches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    listing_id    TEXT NOT NULL,
    title         TEXT NOT NULL,
    rules_item    TEXT NOT NULL,
    gemini_model  TEXT NOT NULL,
    gemini_item   TEXT,           -- price list item for Gemini's model; NULL = not in the price list
    outcome       TEXT NOT NULL,
    created_at    TEXT NOT NULL
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

-- Telegram notifications. sent_at NULL = waiting (quiet hours digest or a failed send).
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    price       INTEGER,
    kind        TEXT NOT NULL,  -- deal / drop / watch / gone
    text        TEXT NOT NULL,  -- full HTML message
    summary     TEXT NOT NULL,  -- one HTML line for the morning digest
    quiet       INTEGER NOT NULL DEFAULT 0,  -- created in quiet hours: goes into the digest
    created_at  TEXT NOT NULL,
    sent_at     TEXT,
    message_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_listing ON alerts (source, listing_id);

-- "🔁 Снизится — сообщи": alert when the price falls >= watch_drop_pct below base_price.
CREATE TABLE IF NOT EXISTS watches (
    source      TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    base_price  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (source, listing_id)
);

-- Hardware of ready-built PCs as Gemini read it (scanner.pc.PcSpecs JSON), once per listing.
CREATE TABLE IF NOT EXISTS pc_specs (
    source      TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (source, listing_id)
);

-- Edits made from Telegram (radius, tier pct, searches, market prices), replayed after a redeploy.
-- base = the repo's value the first edit replaced (JSON), value = the wanted value (JSON).
CREATE TABLE IF NOT EXISTS overrides (
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    base        TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

-- Price learning (scanner.learn): one row per listing and price list item, last asking price.
-- gone_at = the detail page answered 404/410 (a probable sale at `price`).
CREATE TABLE IF NOT EXISTS price_observations (
    source      TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    item        TEXT NOT NULL,  -- price list label "model variant"
    price       INTEGER NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    checked_at  TEXT,
    gone_at     TEXT,
    PRIMARY KEY (source, listing_id, item)
);

-- Devices the parser recognized but the price list does not have (suggestions for new items).
CREATE TABLE IF NOT EXISTS unknown_models (
    source      TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    model       TEXT NOT NULL,  -- e.g. "iphone 16 pro max 1024gb"
    title       TEXT NOT NULL,
    price       INTEGER,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (source, listing_id)
);

-- Weekly price reports and their proposals (applied only by the owner's button).
CREATE TABLE IF NOT EXISTS price_proposals (
    report_id   INTEGER NOT NULL,
    n           INTEGER NOT NULL,   -- position in the report
    item        TEXT NOT NULL,
    old_price   INTEGER NOT NULL,
    new_price   INTEGER NOT NULL,
    selected    INTEGER NOT NULL DEFAULT 1,
    applied_at  TEXT,
    PRIMARY KEY (report_id, n)
);

-- One row per scan, for /status.
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    listings       INTEGER NOT NULL DEFAULT 0,
    new            INTEGER NOT NULL DEFAULT 0,
    price_changed  INTEGER NOT NULL DEFAULT 0,
    candidates     INTEGER NOT NULL DEFAULT 0,
    alerts         INTEGER NOT NULL DEFAULT 0,
    errors         TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT 'bazos'
);

-- Small bot state (paused, ...).
CREATE TABLE IF NOT EXISTS state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- market_price changes made with /price.
CREATE TABLE IF NOT EXISTS price_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item        TEXT NOT NULL,
    old_price   INTEGER NOT NULL,
    new_price   INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
"""


_VINTED_COLUMNS = {
    "extra_cost": "INTEGER NOT NULL DEFAULT 0",
    "foreign_seller": "INTEGER NOT NULL DEFAULT 0",
    "brand": "TEXT",
    "size": "TEXT",
    "condition": "TEXT",
}


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
        self._migrate()

    def _migrate(self) -> None:
        """Columns added after a table was first created (CREATE TABLE IF NOT EXISTS won't add them)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(gemini_reviews)")}
        if "rematched" not in cols:
            with self.conn:
                self.conn.execute("ALTER TABLE gemini_reviews ADD COLUMN rematched TEXT")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(listings)")}
        with self.conn:
            if "gone_at" not in cols:  # a "contacted" listing that disappeared (HTTP 410/404)
                self.conn.execute("ALTER TABLE listings ADD COLUMN gone_at TEXT")
            if "checked_at" not in cols:  # last detail page check of a "contacted" listing
                self.conn.execute("ALTER TABLE listings ADD COLUMN checked_at TEXT")
            for col, decl in _VINTED_COLUMNS.items():  # Vinted: cost on top of the price, item attributes
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {decl}")
        if "source" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")}:
            with self.conn:
                self.conn.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'bazos'")

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
                           posted_date, description, is_top, extra_cost, foreign_seller, brand, size,
                           condition, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        listing.source, listing.id, listing.url, listing.title, listing.price,
                        listing.location, listing.postal_code, _iso(listing.posted_date),
                        listing.description, int(listing.is_top), listing.extra_cost, int(listing.foreign),
                        listing.brand, listing.size, listing.condition, ts, ts,
                    ),
                )
                self._add_price(listing, ts)
                return UpsertResult(UpsertKind.NEW)

            self.conn.execute(
                """UPDATE listings SET url = ?, title = ?, price = ?, location = ?, postal_code = ?,
                       posted_date = COALESCE(?, posted_date), description = COALESCE(?, description),
                       is_top = ?, extra_cost = ?, foreign_seller = ?, brand = ?, size = ?,
                       condition = ?, last_seen = ?
                   WHERE source = ? AND id = ?""",
                (
                    listing.url, listing.title, listing.price, listing.location, listing.postal_code,
                    _iso(listing.posted_date), listing.description, int(listing.is_top), listing.extra_cost,
                    int(listing.foreign), listing.brand, listing.size, listing.condition, ts,
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

    def listing(self, source: str, listing_id: str) -> Listing | None:
        row = self.get(source, listing_id)
        return _row_to_listing(row) if row else None

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
        verdict: str, scam_risk: str, notify: bool, rematched: str | None = None,
        now: datetime | None = None,
    ) -> None:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO gemini_reviews (source, listing_id, price, model, response,
                       verdict, scam_risk, notify, rematched, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, listing_id, price, model, response, verdict, scam_risk, int(notify), rematched, ts),
            )

    def delete_review(self, source: str, listing_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM gemini_reviews WHERE source = ? AND listing_id = ?", (source, listing_id)
            )

    def log_mismatch(
        self, source: str, listing_id: str, title: str, rules_item: str, gemini_model: str,
        gemini_item: str | None, outcome: str, now: datetime | None = None,
    ) -> None:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                """INSERT INTO pricing_mismatches (source, listing_id, title, rules_item, gemini_model,
                       gemini_item, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, listing_id, title, rules_item, gemini_model, gemini_item, outcome, ts),
            )

    def mismatches(self, since: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pricing_mismatches WHERE created_at >= ? ORDER BY id DESC",
            (since.isoformat(timespec="seconds"),),
        ).fetchall()

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


    # --- overrides ---

    def set_override(self, kind: str, key: str, base, value, now: datetime | None = None) -> None:
        """Record an edit; the first edit's base is kept, and going back to it drops the record."""
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        row = self.conn.execute("SELECT base FROM overrides WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        first_base = json.loads(row["base"]) if row else base
        with self.conn:
            if first_base == value and kind != "search":
                self.conn.execute("DELETE FROM overrides WHERE kind = ? AND key = ?", (kind, key))
                return
            self.conn.execute(
                """INSERT INTO overrides (kind, key, base, value, updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (kind, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (kind, key, json.dumps(first_base, ensure_ascii=False), json.dumps(value, ensure_ascii=False), ts),
            )

    def overrides(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM overrides ORDER BY updated_at").fetchall()

    def delete_override(self, kind: str, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM overrides WHERE kind = ? AND key = ?", (kind, key))

    def get_pc_specs(self, source: str, listing_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM pc_specs WHERE source = ? AND listing_id = ?", (source, listing_id)
        ).fetchone()

    def save_pc_specs(self, source: str, listing_id: str, model: str, response: str,
                      now: datetime | None = None) -> None:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO pc_specs (source, listing_id, model, response, created_at) VALUES (?, ?, ?, ?, ?)",
                (source, listing_id, model, response, ts),
            )

    # --- Telegram ---

    def last_price_drop(self, source: str, listing_id: str) -> tuple[int, int, datetime] | None:
        """(old, new, when) if the latest price change of the listing was a drop."""
        rows = self.conn.execute(
            """SELECT price, seen_at FROM price_history WHERE source = ? AND listing_id = ?
               ORDER BY id DESC LIMIT 2""",
            (source, listing_id),
        ).fetchall()
        if len(rows) < 2 or rows[0]["price"] is None or rows[1]["price"] is None:
            return None
        new, old = rows[0]["price"], rows[1]["price"]
        return (old, new, datetime.fromisoformat(rows[0]["seen_at"])) if new < old else None

    def was_alerted(self, source: str, listing_id: str, price: int | None) -> bool:
        """Alerted at this price or a lower one: a price that went up is no news."""
        if price is None:
            sql = "SELECT 1 FROM alerts WHERE source = ? AND listing_id = ? AND price IS NULL AND kind != 'gone'"
            return self.conn.execute(sql, (source, listing_id)).fetchone() is not None
        return self.conn.execute(
            "SELECT 1 FROM alerts WHERE source = ? AND listing_id = ? AND price <= ? AND kind != 'gone'",
            (source, listing_id, price),
        ).fetchone() is not None

    def add_alert(
        self, source: str, listing_id: str, price: int | None, kind: str, text: str, summary: str,
        quiet: bool = False, now: datetime | None = None,
    ) -> int:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO alerts (source, listing_id, price, kind, text, summary, quiet, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, listing_id, price, kind, text, summary, int(quiet), ts),
            )
            if kind != "gone":
                self.conn.execute(
                    "UPDATE listings SET status = 'alerted' WHERE source = ? AND id = ? AND status = 'new'",
                    (source, listing_id),
                )
        return cur.lastrowid

    def pending_alerts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL ORDER BY id").fetchall()

    def mark_sent(self, alert_ids: list[int], message_id: int | None, now: datetime | None = None) -> None:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            self.conn.executemany(
                "UPDATE alerts SET sent_at = ?, message_id = ? WHERE id = ?",
                [(ts, message_id, i) for i in alert_ids],
            )

    def set_watch(self, source: str, listing_id: str, base_price: int, now: datetime | None = None) -> None:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                """INSERT INTO watches (source, listing_id, base_price, created_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (source, listing_id) DO UPDATE SET base_price = excluded.base_price""",
                (source, listing_id, base_price, ts),
            )

    def watches(self) -> dict[tuple[str, str], int]:
        rows = self.conn.execute("SELECT source, listing_id, base_price FROM watches").fetchall()
        return {(r["source"], r["listing_id"]): r["base_price"] for r in rows}

    def to_check_gone(
        self, seen_before: datetime, checked_before: datetime, limit: int, source: str = "bazos"
    ) -> list[sqlite3.Row]:
        """"Contacted" listings missing from the latest scan, not checked recently."""
        return self.conn.execute(
            """SELECT * FROM listings WHERE status = 'contacted' AND gone_at IS NULL AND last_seen < ?
                   AND (checked_at IS NULL OR checked_at < ?) AND source = ?
               ORDER BY checked_at IS NOT NULL, checked_at LIMIT ?""",
            (seen_before.isoformat(timespec="seconds"), checked_before.isoformat(timespec="seconds"), source, limit),
        ).fetchall()

    def mark_checked(self, source: str, listing_id: str, gone: bool, now: datetime | None = None) -> None:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                "UPDATE listings SET checked_at = ?, gone_at = CASE WHEN ? THEN ? ELSE gone_at END "
                "WHERE source = ? AND id = ?",
                (ts, int(gone), ts, source, listing_id),
            )

    def start_run(self, now: datetime | None = None, source: str = "bazos") -> int:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            return self.conn.execute("INSERT INTO runs (started_at, source) VALUES (?, ?)", (ts, source)).lastrowid

    def finish_run(self, run_id: int, now: datetime | None = None, **counts: int | str) -> None:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        cols = ", ".join(f"{k} = ?" for k in counts)
        with self.conn:
            self.conn.execute(
                f"UPDATE runs SET finished_at = ?{', ' + cols if cols else ''} WHERE id = ?",
                (ts, *counts.values(), run_id),
            )

    def last_success(self, source: str = "bazos") -> datetime | None:
        """End of the last scan of `source` that finished and fetched listings."""
        row = self.conn.execute(
            "SELECT MAX(finished_at) FROM runs WHERE finished_at IS NOT NULL AND listings > 0 AND source = ?",
            (source,),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def last_runs(self, limit: int = 1, source: str = "bazos") -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs WHERE source = ? ORDER BY id DESC LIMIT ?", (source, limit)
        ).fetchall()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def log_price_change(self, item: str, old: int, new: int, now: datetime | None = None) -> None:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                "INSERT INTO price_changes (item, old_price, new_price, created_at) VALUES (?, ?, ?, ?)",
                (item, old, new, ts),
            )

    def counts(self) -> dict[str, int]:
        q = "SELECT status, COUNT(*) AS n FROM listings GROUP BY status"
        out = {r["status"]: r["n"] for r in self.conn.execute(q)}
        out["total"] = sum(out.values())
        out["watched"] = self.conn.execute("SELECT COUNT(*) FROM watches").fetchone()[0]
        return out


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
        extra_cost=r["extra_cost"],
        foreign=bool(r["foreign_seller"]),
        brand=r["brand"],
        size=r["size"],
        condition=r["condition"],
        first_seen=datetime.fromisoformat(r["first_seen"]),
        last_seen=datetime.fromisoformat(r["last_seen"]),
    )


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None
