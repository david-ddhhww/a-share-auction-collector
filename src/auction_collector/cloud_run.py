from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import date, datetime, time as clock_time
from pathlib import Path
from typing import Callable

from .config import SHANGHAI_TZ, Settings
from .core import CollectionBlocked, run_collection


LIVE_START = clock_time(9, 26, 5)
LIVE_END = clock_time(9, 29, 0)


def seconds_until_live_window(now: datetime) -> float:
    current = now.astimezone(SHANGHAI_TZ)
    target = datetime.combine(current.date(), LIVE_START, tzinfo=SHANGHAI_TZ)
    return max(0.0, (target - current).total_seconds())


def wait_for_live_window(
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(SHANGHAI_TZ),
    sleep_fn: Callable[[float], None] = time.sleep,
) -> datetime:
    current = now_fn().astimezone(SHANGHAI_TZ)
    wait_seconds = seconds_until_live_window(current)
    if wait_seconds > 0:
        print(f"waiting {wait_seconds:.1f}s for 09:26:05 Asia/Shanghai", flush=True)
        sleep_fn(wait_seconds)
        current = now_fn().astimezone(SHANGHAI_TZ)
    if current.time().replace(tzinfo=None) >= LIVE_END:
        raise CollectionBlocked("GitHub Actions runner missed the verified 09:26:05–09:28:59 live window")
    return current


def is_publishable(report: dict) -> bool:
    market = report.get("market_total") or {}
    quality = report.get("quality") or {}
    return bool(
        quality.get("label") in {"good", "warning"}
        and int(market.get("valid_stock_count") or 0) > 0
        and int(market.get("auction_amount") or 0) > 0
    )


def publish_latest(root: Path, report: dict) -> Path:
    trade_date = str(report["date"])
    day_dir = root / "data" / trade_date
    latest_dir = root / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    required = (
        "report.json",
        "quality.json",
        "board_summary.csv",
        "top20.csv",
        "industry_summary.csv",
    )
    for filename in required:
        source = day_dir / filename
        if not source.exists():
            raise CollectionBlocked(f"publishable output is missing: {source}")
        shutil.copy2(source, latest_dir / filename)
    manifest = {
        "date": trade_date,
        "quality": report["quality"]["label"],
        "generated_at": report["generated_at"],
        "report_path": f"data/{trade_date}/report.json",
        "semantics": "09:25 final opening auction; Eastmoney source row 09:26/f57",
    }
    (latest_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return latest_dir


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="GitHub Actions 09:25 集合竞价云端执行入口")
    command.add_argument("--root", type=Path, default=Path.cwd())
    command.add_argument("--concurrency", type=int, default=6)
    command.add_argument("--no-wait", action="store_true", help="仅供测试；不等待实盘窗口")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    current = datetime.now(SHANGHAI_TZ)
    trade_date: date = current.date()
    if trade_date.weekday() >= 5:
        print("weekend: no collection scheduled")
        return 0
    root = args.root.resolve()
    existing = root / "data" / trade_date.isoformat() / "report.json"
    if existing.exists():
        try:
            report = json.loads(existing.read_text(encoding="utf-8"))
            if is_publishable(report):
                publish_latest(root, report)
                print("today already has a publishable report; latest pointer refreshed")
                return 0
        except (OSError, ValueError, KeyError, TypeError):
            pass
    try:
        if not args.no_wait:
            current = wait_for_live_window()
        settings = Settings(root=root, concurrency=args.concurrency)
        report = run_collection(settings, trade_date, mode="auto", now=current)
        if not is_publishable(report):
            raise CollectionBlocked(
                "collection finished but quality is poor or market amount is empty; latest report is unchanged"
            )
        latest_dir = publish_latest(root, report)
    except CollectionBlocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    print(f"published verified report to {latest_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
