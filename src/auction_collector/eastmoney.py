from __future__ import annotations

import json
import random
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from .config import SHANGHAI_TZ, Settings
from .models import AuctionRecord, Stock


BOARD_FILTERS = {
    "上海主板": {"fs": "m:1 t:2", "exchange": "上海证券交易所", "market_id": 1},
    "深圳主板": {"fs": "m:0 t:6", "exchange": "深圳证券交易所", "market_id": 0},
    "创业板": {"fs": "m:0 t:80", "exchange": "深圳证券交易所", "market_id": 0},
    "科创板": {"fs": "m:1 t:23", "exchange": "上海证券交易所", "market_id": 1},
}


class EastmoneyError(RuntimeError):
    pass


class EastmoneyClient:
    """Structured Eastmoney JSON client with bounded retries and per-thread sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._local = threading.local()
        self._metrics_lock = threading.Lock()
        self.retry_count = 0

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                    ),
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://quote.eastmoney.com/",
                }
            )
            self._local.session = session
        return self._local.session

    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.settings.retries + 1):
            try:
                response = self._session().get(
                    url,
                    params=params,
                    timeout=self.settings.request_timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("data") is None:
                    raise EastmoneyError("response data is null")
                return payload
            except (requests.RequestException, ValueError, EastmoneyError) as exc:
                last_error = exc
                if attempt >= self.settings.retries:
                    break
                with self._metrics_lock:
                    self.retry_count += 1
                time.sleep((0.6 * (2**attempt)) + random.uniform(0.05, 0.35))
        raise EastmoneyError(str(last_error) if last_error else "unknown request error")

    def fetch_stock_pool(self, raw_day_dir: Path) -> list[Stock]:
        stocks, _ = self.fetch_pool_and_snapshot(raw_day_dir)
        return stocks

    def fetch_pool_and_snapshot(
        self, raw_day_dir: Path
    ) -> tuple[list[Stock], dict[str, dict[str, Any]]]:
        url = "https://82.push2.eastmoney.com/api/qt/clist/get"
        stocks: list[Stock] = []
        snapshot: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for board, meta in BOARD_FILTERS.items():
            page = 1
            while True:
                params = {
                    "pn": page,
                    "pz": 200,
                    "po": 1,
                    "np": 1,
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f12",
                    "fs": meta["fs"],
                    "fields": "f5,f6,f12,f13,f14,f17,f18,f26,f100,f292",
                }
                payload = self.get_json(url, params)
                raw_file = raw_day_dir / f"pool_{board}_{page:03d}.json"
                raw_file.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                data = payload.get("data") or {}
                rows = data.get("diff") or []
                if isinstance(rows, dict):
                    rows = list(rows.values())
                for row in rows:
                    code = str(row.get("f12") or "").zfill(6)
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    listed = str(row.get("f26") or "")
                    listing_date = (
                        f"{listed[:4]}-{listed[4:6]}-{listed[6:8]}"
                        if len(listed) == 8 and listed.isdigit()
                        else None
                    )
                    stocks.append(
                        Stock(
                            code=code,
                            name=str(row.get("f14") or ""),
                            exchange=str(meta["exchange"]),
                            board=board,
                            industry=(str(row.get("f100")) if row.get("f100") not in (None, "-") else None),
                            listing_date=listing_date,
                            market_id=int(meta["market_id"]),
                        )
                    )
                    snapshot[code] = {
                        "auction_volume_hands": row.get("f5"),
                        "auction_amount": row.get("f6"),
                        "auction_price": row.get("f17"),
                        "previous_close": row.get("f18"),
                        "market_status": row.get("f292"),
                    }
                total = int(data.get("total") or 0)
                if not rows or page * 200 >= total:
                    break
                page += 1
        return stocks, snapshot

    def auction_from_snapshot(
        self,
        stock: Stock,
        expected_date: date,
        snapshot: dict[str, Any],
        captured_at: datetime,
    ) -> AuctionRecord:
        source = "eastmoney.clist/09:26/f6+trends2-sample-gate"
        fetched_at = captured_at.isoformat(timespec="seconds")
        raw_path = f"raw/{expected_date.isoformat()}/pool_*.json"
        try:
            amount = int(round(float(snapshot["auction_amount"])))
            price = float(snapshot["auction_price"])
            volume_hands = int(float(snapshot["auction_volume_hands"]))
            previous_close = float(snapshot["previous_close"])
            if amount < 0:
                raise ValueError("negative auction amount")
            if amount == 0 or price <= 0:
                raise ValueError("zero_or_unconfirmed_auction")
            return AuctionRecord(
                date=expected_date.isoformat(),
                code=stock.code,
                name=stock.name,
                exchange=stock.exchange,
                board=stock.board,
                logical_auction_time=f"{expected_date.isoformat()} 09:25:00",
                source_timestamp=captured_at.strftime("%Y-%m-%d %H:%M:%S"),
                auction_volume_hands=volume_hands,
                auction_amount=amount,
                auction_price=price,
                previous_close=previous_close,
                auction_change_pct=(
                    round((price / previous_close - 1) * 100, 4)
                    if previous_close > 0
                    else None
                ),
                industry=stock.industry,
                is_new_stock=_is_new_stock(stock.listing_date, expected_date, self.settings.new_stock_days),
                is_valid=True,
                status="valid",
                source=source,
                fetched_at=fetched_at,
                raw_path=raw_path,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return self._invalid_record(
                stock,
                expected_date,
                fetched_at,
                source,
                Path(raw_path),
                "zero_or_unconfirmed_auction" if "zero_or_unconfirmed" in str(exc) else "snapshot_parse_error",
                error=str(exc),
            )

    def fetch_auction(
        self,
        stock: Stock,
        expected_date: date,
        raw_day_dir: Path,
    ) -> AuctionRecord:
        fetched_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
        raw_file = raw_day_dir / "stocks" / f"{stock.code}.json"
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        source = "eastmoney.trends2/09:26/f57"
        try:
            payload = self.get_json(
                "https://push2.eastmoney.com/api/qt/stock/trends2/get",
                {
                    "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                    "ndays": 1,
                    "iscr": 1,
                    "iscca": 0,
                    "secid": f"{stock.market_id}.{stock.code}",
                },
            )
            raw_file.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            data = payload.get("data") or {}
            trends = data.get("trends") or []
            target_prefix = f"{expected_date.isoformat()} 09:26,"
            row = next((item for item in trends if item.startswith(target_prefix)), None)
            if row is None:
                raise EastmoneyError("missing source row at 09:26 for expected date")
            cells = row.split(",")
            if len(cells) != 8:
                raise EastmoneyError(f"unexpected trend field count: {len(cells)}")
            price = float(cells[2])
            volume_hands = int(float(cells[5]))
            amount = int(round(float(cells[6])))
            previous_close_raw = data.get("preClose")
            previous_close = float(previous_close_raw) if previous_close_raw not in (None, "-") else None
            if amount < 0:
                raise EastmoneyError("negative auction amount")
            if price <= 0 or amount == 0:
                return self._invalid_record(
                    stock,
                    expected_date,
                    fetched_at,
                    source,
                    raw_file,
                    "zero_or_unconfirmed_auction",
                    source_timestamp=cells[0],
                )
            change_pct = (
                round((price / previous_close - 1) * 100, 4)
                if previous_close and previous_close > 0
                else None
            )
            return AuctionRecord(
                date=expected_date.isoformat(),
                code=stock.code,
                name=stock.name or str(data.get("name") or ""),
                exchange=stock.exchange,
                board=stock.board,
                logical_auction_time=f"{expected_date.isoformat()} 09:25:00",
                source_timestamp=cells[0],
                auction_volume_hands=volume_hands,
                auction_amount=amount,
                auction_price=price,
                previous_close=previous_close,
                auction_change_pct=change_pct,
                industry=stock.industry,
                is_new_stock=_is_new_stock(stock.listing_date, expected_date, self.settings.new_stock_days),
                is_valid=True,
                status="valid",
                source=source,
                fetched_at=fetched_at,
                raw_path=str(raw_file),
            )
        except Exception as exc:
            error_file = raw_file.with_suffix(".error.json")
            error_file.write_text(
                json.dumps(
                    {"code": stock.code, "error": str(exc), "fetched_at": fetched_at},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return self._invalid_record(
                stock,
                expected_date,
                fetched_at,
                source,
                error_file,
                "request_or_parse_error",
                error=str(exc),
            )

    def _invalid_record(
        self,
        stock: Stock,
        expected_date: date,
        fetched_at: str,
        source: str,
        raw_file: Path,
        status: str,
        *,
        source_timestamp: str | None = None,
        error: str | None = None,
    ) -> AuctionRecord:
        return AuctionRecord(
            date=expected_date.isoformat(),
            code=stock.code,
            name=stock.name,
            exchange=stock.exchange,
            board=stock.board,
            logical_auction_time=f"{expected_date.isoformat()} 09:25:00",
            source_timestamp=source_timestamp,
            auction_volume_hands=None,
            auction_amount=None,
            auction_price=None,
            previous_close=None,
            auction_change_pct=None,
            industry=stock.industry,
            is_new_stock=_is_new_stock(stock.listing_date, expected_date, self.settings.new_stock_days),
            is_valid=False,
            status=status,
            source=source,
            fetched_at=fetched_at,
            raw_path=str(raw_file),
            error=error,
        )


def _is_new_stock(listing_date: str | None, trade_date: date, threshold_days: int) -> bool:
    if not listing_date:
        return False
    try:
        listed = date.fromisoformat(listing_date)
    except ValueError:
        return False
    return 0 <= (trade_date - listed).days <= threshold_days


def build_request_url(url: str, params: dict[str, Any]) -> str:
    """Useful in diagnostics without logging secrets (these endpoints use no credentials)."""
    return f"{url}?{urlencode(params)}"
