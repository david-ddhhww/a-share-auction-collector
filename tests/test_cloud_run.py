from __future__ import annotations

import json
from datetime import datetime

import pytest

from auction_collector.cloud_run import (
    is_publishable,
    publish_latest,
    seconds_until_live_window,
    wait_for_live_window,
)
from auction_collector.config import SHANGHAI_TZ
from auction_collector.core import CollectionBlocked


def test_wait_math_uses_shanghai_live_window() -> None:
    now = datetime(2026, 8, 24, 9, 25, 5, tzinfo=SHANGHAI_TZ)
    assert seconds_until_live_window(now) == 60


def test_wait_rejects_missed_window() -> None:
    now = datetime(2026, 8, 24, 9, 29, 1, tzinfo=SHANGHAI_TZ)
    with pytest.raises(CollectionBlocked, match="missed"):
        wait_for_live_window(now_fn=lambda: now, sleep_fn=lambda _seconds: None)


def test_publish_latest_never_accepts_poor_or_empty_report(tmp_path) -> None:
    assert not is_publishable(
        {
            "quality": {"label": "poor"},
            "market_total": {"valid_stock_count": 5000, "auction_amount": 1},
        }
    )
    assert not is_publishable(
        {
            "quality": {"label": "good"},
            "market_total": {"valid_stock_count": 0, "auction_amount": 0},
        }
    )


def test_publish_latest_copies_only_verified_public_outputs(tmp_path) -> None:
    day_dir = tmp_path / "data" / "2026-08-24"
    day_dir.mkdir(parents=True)
    report = {
        "date": "2026-08-24",
        "generated_at": "2026-08-24T09:28:00+08:00",
        "quality": {"label": "good"},
        "market_total": {"valid_stock_count": 5000, "auction_amount": 123},
    }
    for name in ("report.json", "quality.json", "board_summary.csv", "top20.csv", "industry_summary.csv"):
        (day_dir / name).write_text(json.dumps(report), encoding="utf-8")
    latest = publish_latest(tmp_path, report)
    manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
    assert (latest / "report.json").exists()
    assert manifest["date"] == "2026-08-24"
    assert manifest["quality"] == "good"
    assert not (latest / "stock_auction.csv").exists()
