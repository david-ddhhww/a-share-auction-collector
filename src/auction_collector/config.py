from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Settings:
    root: Path
    concurrency: int = 6
    retries: int = 2
    request_timeout_seconds: float = 15.0
    deadline: time = time(9, 29, 30)
    new_stock_days: int = 30
    sample_validation_size: int = 20

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def database_path(self) -> Path:
        return self.root / "database" / "auction_data.db"

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.raw_dir,
            self.log_dir,
            self.state_dir,
            self.database_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

