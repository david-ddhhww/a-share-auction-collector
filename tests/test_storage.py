from __future__ import annotations

from auction_collector.models import Stock
from auction_collector.storage import build_report, quality_label


def make_stock(code: str, board: str, industry: str) -> Stock:
    return Stock(code, code, "交易所", board, industry, None, 1 if board in ("上海主板", "科创板") else 0)


def make_row(code: str, board: str, amount: int | None, industry: str) -> dict:
    return {
        "code": code,
        "name": code,
        "board": board,
        "industry": industry,
        "auction_amount": amount,
        "auction_change_pct": 1.2,
        "is_valid": int(amount is not None),
    }


def test_quality_thresholds() -> None:
    assert quality_label(1000, 9) == "good"
    assert quality_label(1000, 10) == "warning"
    assert quality_label(1000, 30) == "warning"
    assert quality_label(1000, 31) == "poor"


def test_report_reconciles_four_boards_and_market_total() -> None:
    pool = [
        make_stock("600001", "上海主板", "银行"),
        make_stock("000001", "深圳主板", "银行"),
        make_stock("300001", "创业板", "软件"),
        make_stock("688001", "科创板", "半导体"),
    ]
    rows = [
        make_row("600001", "上海主板", 100, "银行"),
        make_row("000001", "深圳主板", 200, "银行"),
        make_row("300001", "创业板", 300, "软件"),
        make_row("688001", "科创板", 400, "半导体"),
    ]
    report = build_report(
        "2026-08-24",
        pool,
        rows,
        {"上海主板": 50, "深圳主板": 100, "创业板": 150, "科创板": 200},
    )
    assert sum(item["auction_amount"] for item in report["boards"]) == 1000
    assert report["market_total"]["auction_amount"] == 1000
    assert report["market_total"]["previous_auction_amount"] == 500
    assert report["market_total"]["change_pct"] == 100.0
    assert report["top20"][0]["code"] == "688001"
    bank = next(item for item in report["industries"] if item["industry"] == "银行")
    assert bank["auction_amount"] == 300


def test_extreme_historical_deviation_forces_poor_quality() -> None:
    pool = [make_stock("600001", "上海主板", "银行")]
    rows = [make_row("600001", "上海主板", 100, "银行")]
    report = build_report(
        "2026-08-24",
        pool,
        rows,
        {},
        [1000, 1000, 1000, 1000, 1000],
    )
    assert report["quality"]["label"] == "poor"
    assert report["quality"]["historical_ratio"] == 0.1
    assert report["quality"]["anomalies"]
