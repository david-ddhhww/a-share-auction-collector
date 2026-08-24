from __future__ import annotations

import json
from datetime import date, datetime

from auction_collector.config import SHANGHAI_TZ, Settings
from auction_collector.eastmoney import EastmoneyClient
from auction_collector.models import Stock


def stock() -> Stock:
    return Stock(
        code="600519",
        name="贵州茅台",
        exchange="上海证券交易所",
        board="上海主板",
        industry="白酒",
        listing_date="2001-08-27",
        market_id=1,
    )


def test_fetch_auction_uses_0926_f57_not_0925_or_0931(tmp_path) -> None:
    client = EastmoneyClient(Settings(root=tmp_path, retries=0))
    payload = {
        "data": {
            "name": "贵州茅台",
            "preClose": 1272.83,
            "trends": [
                "2026-08-24 09:25,1273.10,1271.01,1273.10,1271.01,0,0.00,1272.830",
                "2026-08-24 09:26,1271.01,1271.01,1271.01,1271.01,298,37876098.00,1271.010",
                "2026-08-24 09:30,1271.01,1271.01,1271.01,1271.01,0,0.00,1271.010",
                "2026-08-24 09:31,1272.00,1273.10,1275.98,1270.33,559,71058165.00,1271.854",
            ],
        }
    }
    client.get_json = lambda *_args, **_kwargs: payload  # type: ignore[method-assign]
    record = client.fetch_auction(stock(), date(2026, 8, 24), tmp_path)
    assert record.is_valid
    assert record.source_timestamp == "2026-08-24 09:26"
    assert record.logical_auction_time == "2026-08-24 09:25:00"
    assert record.auction_volume_hands == 298
    assert record.auction_amount == 37_876_098
    assert record.auction_price == 1271.01
    raw = json.loads((tmp_path / "stocks" / "600519.json").read_text(encoding="utf-8"))
    assert raw == payload


def test_missing_0926_is_invalid_and_never_substitutes_zero(tmp_path) -> None:
    client = EastmoneyClient(Settings(root=tmp_path, retries=0))
    client.get_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "data": {
            "preClose": 10,
            "trends": ["2026-08-24 09:25,10,10,10,10,0,0.00,10"],
        }
    }
    record = client.fetch_auction(stock(), date(2026, 8, 24), tmp_path)
    assert not record.is_valid
    assert record.auction_amount is None
    assert record.status == "request_or_parse_error"


def test_snapshot_conversion_uses_captured_amount(tmp_path) -> None:
    client = EastmoneyClient(Settings(root=tmp_path))
    captured = datetime(2026, 8, 24, 9, 26, 12, tzinfo=SHANGHAI_TZ)
    record = client.auction_from_snapshot(
        stock(),
        date(2026, 8, 24),
        {
            "auction_volume_hands": 298,
            "auction_amount": 37_876_098,
            "auction_price": 1271.01,
            "previous_close": 1272.83,
        },
        captured,
    )
    assert record.is_valid
    assert record.auction_amount == 37_876_098
    assert record.source_timestamp == "2026-08-24 09:26:12"


def test_snapshot_zero_is_missing_not_valid_zero(tmp_path) -> None:
    client = EastmoneyClient(Settings(root=tmp_path))
    record = client.auction_from_snapshot(
        stock(),
        date(2026, 8, 24),
        {"auction_volume_hands": 0, "auction_amount": 0, "auction_price": "-", "previous_close": 10},
        datetime(2026, 8, 24, 9, 26, 12, tzinfo=SHANGHAI_TZ),
    )
    assert not record.is_valid
    assert record.auction_amount is None

