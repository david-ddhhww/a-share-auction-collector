from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as clock_time
from itertools import islice
from pathlib import Path
from typing import Any

from .config import SHANGHAI_TZ, Settings
from .eastmoney import EastmoneyClient
from .models import AuctionRecord, Stock
from .storage import Repository, build_report, export_outputs


REFERENCE_SAMPLE_CODES = {
    "上海主板": ["600519", "601318", "603259", "605499", "600000"],
    "深圳主板": ["000001", "000858", "001979", "002594", "003816"],
    "创业板": ["300750", "300308", "301269", "301236", "300274"],
    "科创板": ["688981", "688041", "688256", "688111", "688008"],
}


class CollectionBlocked(RuntimeError):
    pass


def configure_logging(settings: Settings, trade_date: date) -> logging.Logger:
    logger = logging.getLogger("auction_collector")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(
        settings.log_dir / f"{trade_date.isoformat()}.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def run_collection(
    settings: Settings,
    trade_date: date,
    *,
    mode: str = "auto",
    allow_catchup: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings.ensure_directories()
    logger = configure_logging(settings, trade_date)
    current = now or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    if trade_date.weekday() >= 5:
        raise CollectionBlocked("target date is a weekend")

    raw_day_dir = settings.raw_dir / trade_date.isoformat()
    raw_day_dir.mkdir(parents=True, exist_ok=True)
    client = EastmoneyClient(settings)
    repository = Repository(settings)
    run_id = repository.start_run(trade_date.isoformat())
    started = time.monotonic()
    try:
        if (
            current.date() == trade_date
            and clock_time(9, 26, 0) <= current.time().replace(tzinfo=None) < clock_time(9, 26, 5)
        ):
            wait_seconds = 5 - current.second - (current.microsecond / 1_000_000)
            if wait_seconds > 0:
                logger.info("waiting %.2fs for the 09:26 source row to settle", wait_seconds)
                time.sleep(wait_seconds)
            current = datetime.now(SHANGHAI_TZ)
        live_batch_window = (
            current.date() == trade_date
            and clock_time(9, 26, 5) <= current.time().replace(tzinfo=None) < clock_time(9, 29, 0)
        )
        if mode == "batch" and not live_batch_window:
            raise CollectionBlocked("batch snapshot is only valid from 09:26:05 to 09:28:59 Asia/Shanghai")
        if mode == "auto" and not live_batch_window and not allow_catchup:
            raise CollectionBlocked(
                "outside the verified live snapshot window; use --allow-catchup for per-stock latest-day recovery"
            )

        logger.info("fetching four-board stock pool and structured snapshot")
        pool, snapshot = client.fetch_pool_and_snapshot(raw_day_dir)
        _validate_pool(pool)
        logger.info("stock pool size=%s", len(pool))

        if live_batch_window and mode in ("auto", "batch"):
            captured_at = datetime.now(SHANGHAI_TZ)
            sample_result = validate_batch_snapshot(
                client, pool, snapshot, trade_date, raw_day_dir, captured_at, logger
            )
            if not sample_result["passed"]:
                logger.error("batch gate failed: %s", sample_result)
                records = collect_per_stock(
                    client,
                    repository,
                    pool,
                    trade_date,
                    raw_day_dir,
                    settings,
                    logger,
                    enforce_deadline=True,
                )
            else:
                logger.info("batch gate passed 20/20; accepting captured 09:26 snapshot")
                records = []
                for stock in pool:
                    record = client.auction_from_snapshot(
                        stock, trade_date, snapshot.get(stock.code, {}), captured_at
                    )
                    repository.upsert_stock(record, commit=False)
                    records.append(record.as_dict())
                repository.commit()
        else:
            records = collect_per_stock(
                client,
                repository,
                pool,
                trade_date,
                raw_day_dir,
                settings,
                logger,
                enforce_deadline=False,
            )

        database_rows = repository.rows_for_date(trade_date.isoformat())
        previous = repository.previous_board_amounts(trade_date.isoformat())
        history = repository.recent_market_amounts(trade_date.isoformat())
        report = build_report(trade_date.isoformat(), pool, database_rows, previous, history)
        repository.save_board_summaries(report["boards"] + [report["market_total"]])
        output_dir = export_outputs(settings, report, database_rows)
        quality = report["quality"]["label"]
        success_count = report["market_total"]["valid_stock_count"]
        failure_count = report["market_total"]["missing_stock_count"]
        repository.finish_run(
            run_id,
            stock_count=len(pool),
            success_count=success_count,
            failure_count=failure_count,
            retry_count=client.retry_count,
            data_quality=quality,
            status="completed" if quality != "poor" else "completed_with_warning",
            note=report["quality"].get("warning"),
        )
        logger.info(
            "finished quality=%s valid=%s missing=%s elapsed=%.2fs output=%s",
            quality,
            success_count,
            failure_count,
            time.monotonic() - started,
            output_dir,
        )
        for board in report["boards"]:
            logger.info(
                "board=%s valid=%s missing=%s amount=%s quality=%s",
                board["board"],
                board["valid_stock_count"],
                board["missing_stock_count"],
                board["auction_amount"],
                board["data_quality"],
            )
        return report
    except Exception as exc:
        repository.finish_run(
            run_id,
            stock_count=0,
            success_count=0,
            failure_count=0,
            retry_count=client.retry_count,
            data_quality="poor",
            status="blocked",
            note=str(exc),
        )
        logger.exception("collection blocked or failed: %s", exc)
        raise
    finally:
        repository.close()


def validate_batch_snapshot(
    client: EastmoneyClient,
    pool: list[Stock],
    snapshot: dict[str, dict[str, Any]],
    trade_date: date,
    raw_day_dir: Path,
    captured_at: datetime,
    logger: logging.Logger,
) -> dict[str, Any]:
    by_code = {stock.code: stock for stock in pool}
    samples = [by_code[code] for codes in REFERENCE_SAMPLE_CODES.values() for code in codes if code in by_code]
    if len(samples) != 20:
        return {"passed": False, "reason": "reference sample pool incomplete", "sample_count": len(samples)}

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(client.fetch_auction, stock, trade_date, raw_day_dir / "sample_gate"): stock
            for stock in samples
        }
        for future in as_completed(futures):
            stock = futures[future]
            record = future.result()
            raw_amount = snapshot.get(stock.code, {}).get("auction_amount")
            try:
                snapshot_amount = int(round(float(raw_amount)))
            except (TypeError, ValueError):
                snapshot_amount = None
            matched = bool(
                record.is_valid
                and record.auction_amount is not None
                and snapshot_amount == record.auction_amount
            )
            results.append(
                {
                    "code": stock.code,
                    "board": stock.board,
                    "snapshot_amount": snapshot_amount,
                    "reference_amount": record.auction_amount,
                    "matched": matched,
                    "error": record.error,
                }
            )
    result_path = raw_day_dir / "batch_snapshot_gate.json"
    payload = {
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "passed": len(results) == 20 and all(item["matched"] for item in results),
        "matched_count": sum(1 for item in results if item["matched"]),
        "sample_count": len(results),
        "results": sorted(results, key=lambda item: item["code"]),
    }
    result_path.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("batch gate matched=%s/%s", payload["matched_count"], payload["sample_count"])
    return payload


def collect_per_stock(
    client: EastmoneyClient,
    repository: Repository,
    pool: list[Stock],
    trade_date: date,
    raw_day_dir: Path,
    settings: Settings,
    logger: logging.Logger,
    *,
    enforce_deadline: bool,
) -> list[dict[str, Any]]:
    processed = repository.processed_valid_codes(trade_date.isoformat())
    pending = [stock for stock in pool if stock.code not in processed]
    logger.info("per-stock collection pending=%s resumed_valid=%s", len(pending), len(processed))
    completed = 0
    records: list[dict[str, Any]] = []
    deadline_hit = False
    with ThreadPoolExecutor(max_workers=settings.concurrency) as executor:
        pending_iter = iter(pending)
        futures = {}
        for stock in islice(pending_iter, settings.concurrency):
            futures[executor.submit(client.fetch_auction, stock, trade_date, raw_day_dir)] = stock
        while futures:
            future = next(as_completed(futures))
            futures.pop(future)
            record = future.result()
            repository.upsert_stock(record, commit=False)
            records.append(record.as_dict())
            completed += 1
            if completed % 100 == 0:
                repository.commit()
                logger.info("per-stock completed=%s/%s", completed, len(pending))
            if enforce_deadline and datetime.now(SHANGHAI_TZ).time() >= settings.deadline:
                deadline_hit = True
                continue
            try:
                stock = next(pending_iter)
            except StopIteration:
                continue
            futures[executor.submit(client.fetch_auction, stock, trade_date, raw_day_dir)] = stock
    repository.commit()
    if deadline_hit:
        logger.error("deadline reached before all stocks were submitted; report must remain incomplete")
    return records


def _validate_pool(pool: list[Stock]) -> None:
    if not pool:
        raise CollectionBlocked("empty stock pool")
    codes = [stock.code for stock in pool]
    if len(codes) != len(set(codes)):
        raise CollectionBlocked("duplicate stock codes across board filters")
    invalid_boards = {stock.board for stock in pool} - set(REFERENCE_SAMPLE_CODES)
    if invalid_boards:
        raise CollectionBlocked(f"unexpected boards: {sorted(invalid_boards)}")
