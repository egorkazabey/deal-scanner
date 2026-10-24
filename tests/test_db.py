from datetime import date, datetime

from scanner.db import Database, Status, UpsertKind
from scanner.models import Listing


def make_listing(price: int | None = 10000, **kw) -> Listing:
    base = dict(
        source="bazos",
        id="123",
        url="https://mobil.bazos.cz/inzerat/123/iphone.php",
        title="iPhone 15 128GB",
        price=price,
        location="Pardubice",
        posted_date=date(2026, 10, 15),
    )
    base.update(kw)
    return Listing(**base)


def test_upsert_new_unchanged_price_changed():
    db = Database(":memory:")
    t1, t2, t3 = (datetime(2026, 10, 15, h) for h in (10, 11, 12))

    assert db.upsert(make_listing(10000), now=t1).kind is UpsertKind.NEW
    assert db.upsert(make_listing(10000), now=t2).kind is UpsertKind.UNCHANGED

    result = db.upsert(make_listing(9000), now=t3)
    assert result.kind is UpsertKind.PRICE_CHANGED
    assert result.old_price == 10000

    row = db.get("bazos", "123")
    assert row["price"] == 9000
    assert row["status"] == "new"
    assert row["first_seen"] == t1.isoformat()
    assert row["last_seen"] == t3.isoformat()
    assert [r["price"] for r in db.price_history("bazos", "123")] == [10000, 9000]


def test_unique_by_source_and_id():
    db = Database(":memory:")
    assert db.upsert(make_listing(source="bazos")).kind is UpsertKind.NEW
    assert db.upsert(make_listing(source="fb")).kind is UpsertKind.NEW


def test_description_is_not_overwritten_by_none():
    db = Database(":memory:")
    db.upsert(make_listing(description="Baterie 89 %"))
    db.upsert(make_listing(description=None))
    assert db.get("bazos", "123")["description"] == "Baterie 89 %"


def test_set_status():
    db = Database(":memory:")
    db.upsert(make_listing())
    db.set_status("bazos", "123", Status.CONTACTED)
    assert db.get("bazos", "123")["status"] == "contacted"


def test_migration_adds_rematched_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE gemini_reviews (source TEXT NOT NULL, listing_id TEXT NOT NULL, price INTEGER NOT NULL,
               model TEXT NOT NULL, response TEXT NOT NULL, verdict TEXT NOT NULL, scam_risk TEXT NOT NULL,
               notify INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (source, listing_id, price))"""
    )
    conn.commit()
    conn.close()
    db = Database(path)
    db.save_review("bazos", "1", 100, "m", "{}", "buy", "low", True, rematched="a → b")
    assert db.get_review("bazos", "1", 100)["rematched"] == "a → b"


def test_was_alerted_at_this_or_lower_price():
    db = Database(":memory:")
    db.upsert(make_listing(10000))
    db.add_alert("bazos", "123", 10000, "deal", "t", "s")
    assert db.was_alerted("bazos", "123", 10000) and db.was_alerted("bazos", "123", 10500)
    assert not db.was_alerted("bazos", "123", 9500)
    assert db.listing("bazos", "123").price == 10000 and db.listing("bazos", "nope") is None
