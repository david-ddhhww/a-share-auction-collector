from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import SHANGHAI_TZ, Settings
from .models import AuctionRecord, Stock


STOCK_COLUMNS = [
    "date",
    "code",
    "name",
    "exchange",
    "board",
    "logical_auction_time",
    "source_timestamp",
    "auction_volume_hands",
    "auction_amount",
    "auction_price",
    "previous_close",
    "auction_change_pct",
    "industry",
    "is_new_stock",
    "is_valid",
    "status",
    "source",
    "fetched_at",
    "raw_path",
    "error",
]


class Repository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.connection = sqlite3.connect(settings.database_path)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS daily_stock_auction (
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                board TEXT NOT NULL,
                logical_auction_time TEXT NOT NULL,
                source_timestamp TEXT,
                auction_volume_hands INTEGER,
                auction_amount INTEGER,
                auction_price REAL,
                previous_close REAL,
                auction_change_pct REAL,
                industry TEXT,
                is_new_stock INTEGER NOT NULL,
                is_valid INTEGER NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                raw_path TEXT,
                error TEXT,
                PRIMARY KEY (date, code)
            );
            CREATE TABLE IF NOT EXISTS daily_board_summary (
                date TEXT NOT NULL,
                board TEXT NOT NULL,
                auction_amount INTEGER NOT NULL,
                previous_auction_amount INTEGER,
                change_amount INTEGER,
                change_pct REAL,
                stock_count INTEGER NOT NULL,
                valid_stock_count INTEGER NOT NULL,
                missing_stock_count INTEGER NOT NULL,
                data_quality TEXT NOT NULL,
                PRIMARY KEY (date, board)
            );
            CREATE TABLE IF NOT EXISTS collection_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                stock_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                data_quality TEXT,
                status TEXT NOT NULL,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_stock_auction_date_valid
                ON daily_stock_auction(date, is_valid);
            """
        )
        self.connection.commit()

    def start_run(self, trade_date: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO collection_runs(date, started_at, status) VALUES (?, ?, ?)",
            (trade_date, datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"), "running"),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        stock_count: int,
        success_count: int,
        failure_count: int,
        retry_count: int,
        data_quality: str,
        status: str,
        note: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE collection_runs
               SET finished_at=?, stock_count=?, success_count=?, failure_count=?,
                   retry_count=?, data_quality=?, status=?, note=?
             WHERE run_id=?
            """,
            (
                datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
                stock_count,
                success_count,
                failure_count,
                retry_count,
                data_quality,
                status,
                note,
                run_id,
            ),
        )
        self.connection.commit()

    def upsert_stock(self, record: AuctionRecord, *, commit: bool = True) -> None:
        values = record.as_dict()
        values["is_new_stock"] = int(record.is_new_stock)
        values["is_valid"] = int(record.is_valid)
        placeholders = ",".join("?" for _ in STOCK_COLUMNS)
        updates = ",".join(f"{column}=excluded.{column}" for column in STOCK_COLUMNS[2:])
        self.connection.execute(
            f"""
            INSERT INTO daily_stock_auction ({','.join(STOCK_COLUMNS)})
            VALUES ({placeholders})
            ON CONFLICT(date, code) DO UPDATE SET {updates}
            """,
            tuple(values[column] for column in STOCK_COLUMNS),
        )
        if commit:
            self.connection.commit()

    def commit(self) -> None:
        self.connection.commit()

    def processed_valid_codes(self, trade_date: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT code FROM daily_stock_auction WHERE date=? AND is_valid=1",
            (trade_date,),
        )
        return {str(row["code"]) for row in rows}

    def rows_for_date(self, trade_date: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM daily_stock_auction WHERE date=? ORDER BY code",
            (trade_date,),
        )
        return [dict(row) for row in rows]

    def previous_date(self, trade_date: str) -> str | None:
        row = self.connection.execute(
            "SELECT MAX(date) AS date FROM daily_board_summary WHERE date < ?",
            (trade_date,),
        ).fetchone()
        return str(row["date"]) if row and row["date"] else None

    def previous_board_amounts(self, trade_date: str) -> dict[str, int]:
        previous = self.previous_date(trade_date)
        if not previous:
            return {}
        rows = self.connection.execute(
            "SELECT board, auction_amount FROM daily_board_summary WHERE date=?",
            (previous,),
        )
        return {str(row["board"]): int(row["auction_amount"]) for row in rows}

    def recent_market_amounts(self, trade_date: str, limit: int = 20) -> list[int]:
        rows = self.connection.execute(
            """
            SELECT auction_amount
              FROM daily_board_summary
             WHERE board='全A' AND date < ? AND auction_amount > 0
             ORDER BY date DESC LIMIT ?
            """,
            (trade_date, limit),
        )
        return [int(row["auction_amount"]) for row in rows]

    def save_board_summaries(self, summaries: Iterable[dict[str, Any]]) -> None:
        self.connection.executemany(
            """
            INSERT INTO daily_board_summary(
                date, board, auction_amount, previous_auction_amount, change_amount,
                change_pct, stock_count, valid_stock_count, missing_stock_count, data_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, board) DO UPDATE SET
                auction_amount=excluded.auction_amount,
                previous_auction_amount=excluded.previous_auction_amount,
                change_amount=excluded.change_amount,
                change_pct=excluded.change_pct,
                stock_count=excluded.stock_count,
                valid_stock_count=excluded.valid_stock_count,
                missing_stock_count=excluded.missing_stock_count,
                data_quality=excluded.data_quality
            """,
            [
                (
                    item["date"],
                    item["board"],
                    item["auction_amount"],
                    item["previous_auction_amount"],
                    item["change_amount"],
                    item["change_pct"],
                    item["stock_count"],
                    item["valid_stock_count"],
                    item["missing_stock_count"],
                    item["data_quality"],
                )
                for item in summaries
            ],
        )
        self.connection.commit()


def quality_label(stock_count: int, missing_count: int) -> str:
    if stock_count <= 0:
        return "poor"
    rate = missing_count / stock_count
    if rate < 0.01:
        return "good"
    if rate <= 0.03:
        return "warning"
    return "poor"


def build_report(
    trade_date: str,
    pool: list[Stock],
    records: list[dict[str, Any]],
    previous_amounts: dict[str, int],
    historical_market_amounts: list[int] | None = None,
) -> dict[str, Any]:
    counts = defaultdict(int)
    amounts = defaultdict(int)
    valid_counts = defaultdict(int)
    pool_counts = defaultdict(int)
    for stock in pool:
        pool_counts[stock.board] += 1
    valid_records = []
    for record in records:
        counts[record["board"]] += 1
        if record["is_valid"] and record["auction_amount"] is not None:
            valid_counts[record["board"]] += 1
            amounts[record["board"]] += int(record["auction_amount"])
            valid_records.append(record)

    boards = []
    board_order = ["上海主板", "深圳主板", "创业板", "科创板"]
    for board in board_order:
        stock_count = pool_counts[board]
        valid_count = valid_counts[board]
        missing_count = stock_count - valid_count
        current = amounts[board]
        previous = previous_amounts.get(board)
        change = current - previous if previous is not None else None
        change_pct = (
            round(change / previous * 100, 2)
            if change is not None and previous not in (None, 0)
            else None
        )
        boards.append(
            {
                "date": trade_date,
                "board": board,
                "auction_amount": current,
                "previous_auction_amount": previous,
                "change_amount": change,
                "change_pct": change_pct,
                "stock_count": stock_count,
                "valid_stock_count": valid_count,
                "missing_stock_count": missing_count,
                "data_quality": quality_label(stock_count, missing_count),
            }
        )

    market_current = sum(item["auction_amount"] for item in boards)
    market_previous = (
        sum(previous_amounts.get(board, 0) for board in board_order)
        if previous_amounts
        else None
    )
    market_change = market_current - market_previous if market_previous is not None else None
    market_stock_count = sum(item["stock_count"] for item in boards)
    market_valid_count = sum(item["valid_stock_count"] for item in boards)
    market_missing = market_stock_count - market_valid_count
    top20 = sorted(valid_records, key=lambda item: int(item["auction_amount"]), reverse=True)[:20]
    top20_output = []
    for rank, item in enumerate(top20, start=1):
        top20_output.append(
            {
                "rank": rank,
                "code": item["code"],
                "name": item["name"],
                "board": item["board"],
                "industry": item["industry"],
                "auction_amount": item["auction_amount"],
                "auction_change_pct": item["auction_change_pct"],
                "market_share_pct": (
                    round(int(item["auction_amount"]) / market_current * 100, 4)
                    if market_current > 0
                    else None
                ),
            }
        )

    industry_amounts: dict[str, int] = defaultdict(int)
    industry_top: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in valid_records:
        industry = item["industry"] or "未分类"
        industry_amounts[industry] += int(item["auction_amount"])
        industry_top[industry].append(item)
    industries = []
    for industry, amount in sorted(industry_amounts.items(), key=lambda pair: pair[1], reverse=True):
        leaders = sorted(
            industry_top[industry], key=lambda item: int(item["auction_amount"]), reverse=True
        )[:3]
        industries.append(
            {
                "industry": industry,
                "auction_amount": amount,
                "market_share_pct": round(amount / market_current * 100, 4) if market_current else None,
                "top_stocks": [
                    {"code": item["code"], "name": item["name"], "auction_amount": item["auction_amount"]}
                    for item in leaders
                ],
            }
        )

    quality = quality_label(market_stock_count, market_missing)
    anomaly_messages: list[str] = []
    historical_median = None
    historical_ratio = None
    history = sorted(historical_market_amounts or [])
    if len(history) >= 5 and market_current > 0:
        middle = len(history) // 2
        historical_median = (
            history[middle]
            if len(history) % 2
            else round((history[middle - 1] + history[middle]) / 2)
        )
        historical_ratio = market_current / historical_median if historical_median else None
        if historical_ratio is not None and (historical_ratio < 0.2 or historical_ratio > 5):
            quality = "poor"
            anomaly_messages.append(
                f"全市场竞价额为近{len(history)}日中位数的{historical_ratio:.2f}倍，超出0.2–5倍守卫区间"
            )
    return {
        "date": trade_date,
        "market_total": {
            "date": trade_date,
            "board": "全A",
            "auction_amount": market_current,
            "previous_auction_amount": market_previous,
            "change_amount": market_change,
            "change_pct": (
                round(market_change / market_previous * 100, 2)
                if market_change is not None and market_previous not in (None, 0)
                else None
            ),
            "stock_count": market_stock_count,
            "valid_stock_count": market_valid_count,
            "missing_stock_count": market_missing,
            "data_quality": quality,
        },
        "boards": boards,
        "top20": top20_output,
        "industries": industries,
        "quality": {
            "label": quality,
            "missing_rate": round(market_missing / market_stock_count, 6) if market_stock_count else 1,
            "historical_median": historical_median,
            "historical_ratio": round(historical_ratio, 4) if historical_ratio is not None else None,
            "anomalies": anomaly_messages,
            "warning": (
                "今日集合竞价数据不完整或异常，不建议直接用于交易判断。"
                if quality == "poor"
                else None
            ),
        },
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
    }


def export_outputs(settings: Settings, report: dict[str, Any], records: list[dict[str, Any]]) -> Path:
    day_dir = settings.data_dir / report["date"]
    day_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(day_dir / "stock_auction.csv", records, STOCK_COLUMNS)
    _write_csv(day_dir / "board_summary.csv", report["boards"], list(report["boards"][0].keys()))
    _write_csv(day_dir / "top20.csv", report["top20"], list(report["top20"][0].keys()) if report["top20"] else [])
    industry_rows = [
        {
            "industry": item["industry"],
            "auction_amount": item["auction_amount"],
            "market_share_pct": item["market_share_pct"],
            "top_stocks": json.dumps(item["top_stocks"], ensure_ascii=False),
        }
        for item in report["industries"]
    ]
    _write_csv(
        day_dir / "industry_summary.csv",
        industry_rows,
        ["industry", "auction_amount", "market_share_pct", "top_stocks"],
    )
    (day_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (day_dir / "quality.json").write_text(
        json.dumps(report["quality"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return day_dir


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
