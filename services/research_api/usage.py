from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

BASE = Path.home() / "dakota"
LOG_DIR = BASE / "logs"
USAGE_LOG = LOG_DIR / "usage.jsonl"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log_usage(event_type: str, payload: dict[str, Any]) -> None:
    record = {
        "ts": datetime.now().isoformat(),
        "event_type": event_type,
        **payload,
    }
    with USAGE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
