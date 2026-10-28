"""Source-independent data models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class Listing:
    source: str  # "bazos" | "sbazar" | "fb" | "vinted"
    id: str  # source-specific listing id
    url: str  # full URL (for Bazoš including the slug)
    title: str
    price: int | None  # CZK; None = "Dohodou", "V textu", etc.
    location: str | None
    posted_date: date | None
    description: str | None = None  # filled lazily from the detail page
    postal_code: str | None = None
    is_top: bool = False  # paid TOP listing, not ordered by date
    extra_cost: int = 0  # paid on top of the price: Vinted buyer protection + shipping
    foreign: bool = False  # seller abroad (Vinted shows foreign listings converted to CZK)
    brand: str | None = None  # Vinted: brand field
    size: str | None = None  # Vinted: size (clothes, shoes)
    condition: str | None = None  # Vinted: "Nový s visačkou" / "Velmi dobrý" / "Dobrý" / ...
    first_seen: datetime | None = None
    last_seen: datetime | None = None


@dataclass
class ListingDetail:
    url: str
    title: str
    price: int | None
    description: str | None
    posted_date: date | None
