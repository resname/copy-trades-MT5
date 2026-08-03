#!/usr/bin/env python3
"""Fetch the TradeVPS community broker-servers list and write it to
manager/data/brokers_default.json (the shipped zero-network snapshot).

Run manually before a release to refresh the snapshot. Not part of CI
(no network in CI). The snapshot format is identical to the live TradeVPS
response so the same parser serves both.

    python scripts/update_brokers_default.py
"""
import json
import urllib.request
from pathlib import Path

URL = "https://broker-servers.apis.tradevps.net/"
OUT = Path(__file__).resolve().parent.parent / "manager" / "data" / \
    "brokers_default.json"


def main() -> int:
    with urllib.request.urlopen(URL, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    n = len(data.get("brokers", []))
    print(f"wrote {n} brokers to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())