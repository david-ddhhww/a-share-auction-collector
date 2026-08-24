from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    exchange: str
    board: str
    industry: str | None
    listing_date: str | None
    market_id: int


@dataclass(frozen=True)
class AuctionRecord:
    date: str
    code: str
    name: str
    exchange: str
    board: str
    logical_auction_time: str
    source_timestamp: str | None
    auction_volume_hands: int | None
    auction_amount: int | None
    auction_price: float | None
    previous_close: float | None
    auction_change_pct: float | None
    industry: str | None
    is_new_stock: bool
    is_valid: bool
    status: str
    source: str
    fetched_at: str
    raw_path: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

