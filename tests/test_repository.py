from __future__ import annotations

from datetime import date, datetime

from auction_collector.config import SHANGHAI_TZ, Settings
from auction_collector.eastmoney import EastmoneyClient
from auction_collector.models import Stock
from auction_collector.storage import Repository


def test_sqlite_upsert_is_idempotent(tmp_path) -> None:
    settings = Settings(root=tmp_path)
    settings.ensure_directories()
    repository = Repository(settings)
    stock = Stock("600000", "浦发银行", "上海证券交易所", "上海主板", "银行", None, 1)
    record = EastmoneyClient(settings).auction_from_snapshot(
        stock,
        date(2026, 8, 24),
        {"auction_volume_hands": 3129, "auction_amount": 2_819_229, "auction_price": 9.01, "previous_close": 9.0},
        datetime(2026, 8, 24, 9, 26, 10, tzinfo=SHANGHAI_TZ),
    )
    repository.upsert_stock(record)
    repository.upsert_stock(record)
    rows = repository.rows_for_date("2026-08-24")
    repository.close()
    assert len(rows) == 1
    assert rows[0]["auction_amount"] == 2_819_229

