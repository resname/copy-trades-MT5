# manager/brokers/default.py
from __future__ import annotations

import json
from pathlib import Path

from manager.brokers.catalog import Broker, parse_brokers_json

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_default(path: Path | None = None) -> list[Broker]:
    """Load the shipped brokers_default.json snapshot (TradeVPS shape) into a
    list[Broker]. Returns [] if the file is missing or unreadable — never
    raises, so a broken snapshot degrades to 'default + learned only' instead
    of crashing the app."""
    p = Path(path) if path is not None else (_DATA_DIR / "brokers_default.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return []
    return parse_brokers_json(data, "default")