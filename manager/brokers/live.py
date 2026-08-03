# manager/brokers/live.py
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://broker-servers.apis.tradevps.net/"
FRESH_SECS = 24 * 3600


def fetch_live(timeout: float = 10.0) -> dict | None:
    """GET the TradeVPS community broker-servers list. Returns the raw parsed
    dict (TradeVPS shape {"brokers":[...]}) or None on ANY failure (network,
    timeout, non-200, bad JSON). Never raises."""
    try:
        with urllib.request.urlopen(URL, timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                return None
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def parse_iso(s) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return 0.0


def is_fresh(payload: dict, now: float) -> bool:
    """True if the cache payload's fetched_at is within FRESH_SECS of now."""
    fetched_at = parse_iso(payload.get("fetched_at"))
    return fetched_at > 0 and (now - fetched_at) < FRESH_SECS


def load_cache(path) -> dict | None:
    """Read the cached payload {"fetched_at","brokers"} or None if
    missing/corrupt. Never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def refresh_cache(path, timeout: float = 10.0, now: float | None = None) -> dict | None:
    """Fetch live, atomically write {"fetched_at": iso, "brokers": [...]} to
    path, and return the written payload. On fetch failure return None and
    leave the existing cache untouched. Never raises."""
    data = fetch_live(timeout=timeout)
    if data is None:
        return None
    ts = now if now is not None else time.time()
    payload = {"fetched_at": iso_from_ts(ts), "brokers": data.get("brokers", [])}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return payload