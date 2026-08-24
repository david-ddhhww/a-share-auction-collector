from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .config import Settings
from .core import CollectionBlocked, run_collection


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="A股 9:25 最终集合竞价数据采集器")
    command.add_argument("--root", type=Path, default=Path.cwd(), help="data/database/logs/raw 根目录")
    command.add_argument("--date", type=date.fromisoformat, default=date.today())
    command.add_argument("--mode", choices=("auto", "batch", "per-stock"), default="auto")
    command.add_argument(
        "--allow-catchup",
        action="store_true",
        help="允许在09:26窗口外使用逐股接口补采最近交易日；绝不使用收盘累计快照",
    )
    command.add_argument("--concurrency", type=int, default=6)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 1 <= args.concurrency <= 12:
        print("concurrency must be between 1 and 12", file=sys.stderr)
        return 2
    settings = Settings(root=args.root.resolve(), concurrency=args.concurrency)
    try:
        report = run_collection(
            settings,
            args.date,
            mode=args.mode,
            allow_catchup=args.allow_catchup or args.mode == "per-stock",
        )
    except CollectionBlocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

