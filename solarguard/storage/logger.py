"""Strukturovaný log událostí – JSONL formát, snadno parsovatelný."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("events")


class EventLogger:
    """Zapisuje jednu událost na řádek do events.jsonl."""

    def __init__(self, log_dir: str, filename: str = "events.jsonl"):
        self.path = Path(log_dir) / filename
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, **fields: Any) -> None:
        record: Dict[str, Any] = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "type": event_type,
            **fields,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"event log write failed: {e}")


def setup_logging(log_dir: str, level: str = "INFO", app_file: str = "solarguard.log") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper()))
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
    fh = logging.FileHandler(os.path.join(log_dir, app_file))
    fh.setFormatter(fmt)
    root.addHandler(fh)
