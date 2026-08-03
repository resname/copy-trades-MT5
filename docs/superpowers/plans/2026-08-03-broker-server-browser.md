# Broker/Server Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user pick an MT5 server by **broker name** (which they remember) via a browsable Broker→Server picker, instead of typing an exact server string free-text, backed by a merged broker catalog (shipped snapshot + best-effort live refresh + previously-used servers).

**Architecture:** A pure-logic `BrokerCatalog` merges three sources (shipped `brokers_default.json`, a best-effort TradeVPS community-list live cache, and a learned-servers list from the settings store) into one broker→servers map, deduped by normalized name with demo-first ordering. A reusable Qt widget `BrokerServerPicker` (Broker combo + Server combo + Refresh button) replaces the free-text `Server` `QLineEdit` in both the master form and the slave editor. The controller owns catalog construction/refresh and records a worker's connected server (from its first `StatusMsg`) into the learned list once. `AccountSpec.server` and the worker `mt5.initialize(server=…)` path are unchanged — the picker is purely a name selector.

**Tech Stack:** Python ≥3.11, PySide6/Qt (GUI widget only), `urllib.request` (live fetch), stdlib `json`/`pathlib`/`datetime` (cache), pytest. No new third-party dependencies. No MetaQuotes endpoint, no MetaApi (YAGNI).

## Global Constraints

(Verbatim from the approved spec — bind every task.)

- Demo accounts only — never capture or log in with a real account. (The picker lists real servers too, ordering demo-first + labeling, but never blocks real.)
- Credentials are passed to workers through the pipe, never on the command line.
- DPAPI-encrypted credentials at rest (pywin32 win32crypt). The learned-servers list is non-secret public server names; plain JSON in the settings file is fine.
- Slave normalizes (EA-faithful).
- Capture artifacts (pcaps, Frida logs) are gitignored, never committed.
- No new runtime dependencies. Live fetch uses only stdlib `urllib.request`.
- `fetch_live` / `refresh_cache` / `load_cache` NEVER raise — on any error (network, timeout, non-200, bad JSON) they return `None`; the catalog falls back to default + cache + learned and logs a warning.
- `AccountSpec.server` and the worker `mt5.initialize(server=…)` call are unchanged. The picker only changes how the user supplies that string.
- Headless gate = no new failures, suite green. GUI tests use `pytest.importorskip("PySide6")` and skip cleanly when PySide6 is absent (existing pattern). Do NOT treat the plan's predicted pass counts as pass/fail criteria — run the real suite and report actual counts.

## File Structure

```
manager/
  brokers/
    __init__.py        Package marker (empty).
    catalog.py         BrokerServer / Broker dataclasses + BrokerCatalog
                       (merge/dedup by normalized name, demo-first sort, query).
                       Pure logic — no Qt, no network. Shared parse_brokers_json().
    default.py         Loads the shipped manager/data/brokers_default.json snapshot.
    live.py            TradeVPS fetch (urllib) + cache read/write. Best-effort,
                       timeout-bounded, never raises. No Qt.
    learned.py         record()/load() previously-used servers from the settings store.
  data/
    brokers_default.json   Shipped TradeVPS-format snapshot (zero-network fallback).
  gui/
    server_picker.py   BrokerServerPicker widget (Broker combo + Server combo +
                       Refresh button). Reused by both forms.
  settings/
    store.py           (existing) — add a learned_servers list field (setdefault).
  app/
    controller.py      (existing) — get_catalog / refresh_brokers / _on_worker_status
                       + wire supervisor.on_status_msg in build_supervisor.
  supervisor.py        (existing) — add on_status_msg callback, fire on StatusMsg.
  gui/
    main_window.py     (existing) — replace master_server QLineEdit with the picker.
    slave_editor.py    (existing) — replace server QLineEdit with the picker.
scripts/
  update_brokers_default.py  Maintainer-only: fetch TradeVPS → brokers_default.json.
manager/tests/
  test_catalog.py / test_default.py / test_live.py / test_learned.py (new, headless)
  test_server_picker.py (new, PySide6 importorskip)
  test_settings_store.py / test_controller.py / test_main_window.py /
  test_slave_editor.py (extended/updated)
docs/ README.md, docs/TESTING.md (updated)
```

---

### Task 1: Broker catalog (pure merge/dedup/sort)

**Files:**
- Create: `manager/brokers/__init__.py`
- Create: `manager/brokers/catalog.py`
- Test: `manager/tests/test_catalog.py`

**Interfaces:**
- Produces: `BrokerServer(name: str, type: str)`, `Broker(name: str, servers: tuple[BrokerServer,...], source: str)`, `BrokerCatalog(default=(), live=(), learned_servers=())`, `PREVIOUSLY_USED = "(Previously used)"`, `parse_brokers_json(data: dict, source: str) -> list[Broker]`. Later tasks import these.

- [ ] **Step 1: Write the failing test** — `manager/tests/test_catalog.py`

```python
from manager.brokers.catalog import (
    Broker, BrokerServer, BrokerCatalog, PREVIOUSLY_USED, parse_brokers_json,
)


def test_parse_brokers_json_tradevps_shape():
    data = {"brokers": [
        {"name": "IC Markets",
         "servers": [{"name": "ICMarketsSC-Demo", "type": "demo"},
                      {"name": "ICMarketsSC-Live", "type": "real"}]}]}
    out = parse_brokers_json(data, "default")
    assert out == [Broker(
        "IC Markets",
        (BrokerServer("ICMarketsSC-Demo", "demo"),
         BrokerServer("ICMarketsSC-Live", "real")),
        "default")]


def test_parse_brokers_json_unknown_type_becomes_unknown():
    out = parse_brokers_json(
        {"brokers": [{"name": "X", "servers": [{"name": "S", "type": "weird"}]}]},
        "live")
    assert out[0].servers[0].type == "unknown"


def test_parse_brokers_json_missing_type_is_unknown():
    out = parse_brokers_json(
        {"brokers": [{"name": "X", "servers": [{"name": "S"}]}]}, "live")
    assert out[0].servers[0].type == "unknown"


def test_parse_brokers_json_skips_empty_server_names():
    out = parse_brokers_json(
        {"brokers": [{"name": "X", "servers": [{"name": ""}, {"name": "S"}]}]},
        "default")
    assert [s.name for s in out[0].servers] == ["S"]


def test_merge_dedups_servers_across_sources():
    default = [Broker("IC Markets",
                      (BrokerServer("ICMarketsSC-Demo", "demo"),), "default")]
    live = [Broker("IC Markets",
                   (BrokerServer("ICMarketsSC-Demo", "demo"),
                    BrokerServer("ICMarketsSC-Live", "real")), "live")]
    cat = BrokerCatalog(default=default, live=live)
    ic = [b for b in cat.brokers if b.name == "IC Markets"][0]
    assert {s.name for s in ic.servers} == {"ICMarketsSC-Demo", "ICMarketsSC-Live"}


def test_merge_normalizes_broker_name_case_and_whitespace():
    a = [Broker("IC Markets", (BrokerServer("S1", "demo"),), "default")]
    b = [Broker("  ic markets ", (BrokerServer("S2", "real"),), "live")]
    cat = BrokerCatalog(default=a, live=b)
    ics = [bk for bk in cat.brokers if bk.name.strip().lower() == "ic markets"]
    assert len(ics) == 1
    assert {s.name for s in ics[0].servers} == {"S1", "S2"}


def test_servers_for_demo_first_then_real_then_unknown():
    servers = (BrokerServer("Z-Real", "real"), BrokerServer("A-Demo", "demo"),
               BrokerServer("M-Unknown", "unknown"), BrokerServer("B-Demo", "demo"))
    cat = BrokerCatalog(default=[Broker("X", servers, "default")])
    assert [s.name for s in cat.servers_for("X")] == \
           ["A-Demo", "B-Demo", "Z-Real", "M-Unknown"]


def test_broker_names_previously_used_first_when_learned():
    cat = BrokerCatalog(
        default=[Broker("Beta", (BrokerServer("B", "demo"),), "default"),
                 Broker("Alpha", (BrokerServer("A", "demo"),), "default")],
        learned_servers=["LearnedServer"])
    names = cat.broker_names()
    assert names[0] == PREVIOUSLY_USED
    assert names[1:] == ["Alpha", "Beta"]  # real brokers alphabetical


def test_broker_names_no_previously_used_when_empty():
    cat = BrokerCatalog(default=[Broker("Alpha", (), "default")])
    assert cat.broker_names() == ["Alpha"]


def test_servers_for_previously_used_returns_learned():
    cat = BrokerCatalog(learned_servers=["S1", "S2"])
    assert {s.name for s in cat.servers_for(PREVIOUSLY_USED)} == {"S1", "S2"}


def test_add_learned_dedups_in_memory():
    cat = BrokerCatalog(learned_servers=["S1"])
    cat.add_learned("S1")
    cat.add_learned("S2")
    assert {s.name for s in cat.servers_for(PREVIOUSLY_USED)} == {"S1", "S2"}


def test_servers_for_unknown_broker_returns_empty():
    cat = BrokerCatalog(default=[Broker("X", (BrokerServer("S", "demo"),), "default")])
    assert cat.servers_for("Nope") == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest manager/tests/test_catalog.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.brokers'`

- [ ] **Step 3: Write minimal implementation** — `manager/brokers/__init__.py`

```python
# manager/brokers/__init__.py
```

(empty file — package marker)

— `manager/brokers/catalog.py`

```python
# manager/brokers/catalog.py
from __future__ import annotations

from dataclasses import dataclass

PREVIOUSLY_USED = "(Previously used)"


@dataclass(frozen=True)
class BrokerServer:
    name: str
    type: str          # "demo" | "real" | "unknown"


@dataclass(frozen=True)
class Broker:
    name: str
    servers: tuple[BrokerServer, ...]
    source: str        # "default" | "live" | "learned" (debug provenance)


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _server_type(t) -> str:
    return t if t in ("demo", "real") else "unknown"


def parse_brokers_json(data: dict, source: str) -> list[Broker]:
    """Parse the TradeVPS shape {"brokers":[{"name","servers":[{"name","type"}]}]}
    into a list[Broker]. Tolerant: bad/missing types become "unknown"; empty
    server names are skipped. Non-dict input -> []."""
    if not isinstance(data, dict):
        return []
    out: list[Broker] = []
    for b in data.get("brokers") or []:
        if not isinstance(b, dict):
            continue
        name = str(b.get("name", "") or "")
        servers: list[BrokerServer] = []
        for s in b.get("servers") or []:
            if not isinstance(s, dict):
                continue
            sname = str(s.get("name", "") or "")
            if not sname:
                continue
            servers.append(BrokerServer(sname, _server_type(s.get("type"))))
        out.append(Broker(name=name, servers=tuple(servers), source=source))
    return out


def _union_servers(a: tuple[BrokerServer, ...],
                   b: tuple[BrokerServer, ...]) -> tuple[BrokerServer, ...]:
    """Union two server tuples, deduped by normalized server name. If a server
    appears in both, prefer a defined (demo/real) type over "unknown"."""
    seen: dict[str, BrokerServer] = {}
    for s in a:
        k = _norm(s.name)
        if k and k not in seen:
            seen[k] = s
    for s in b:
        k = _norm(s.name)
        if not k:
            continue
        if k in seen:
            if seen[k].type == "unknown" and s.type != "unknown":
                seen[k] = s
        else:
            seen[k] = s
    return tuple(seen.values())


def _merge_brokers(groups: list[list[Broker]]) -> list[Broker]:
    """Merge multiple broker lists into one. Brokers with the same normalized
    name (case-insensitive, trimmed) are ONE broker whose server sets are
    unioned. The first-seen display name and source win."""
    by_key: dict[str, Broker] = {}
    order: list[str] = []
    for group in groups:
        for broker in group:
            key = _norm(broker.name)
            if not key:
                continue
            if key in by_key:
                ex = by_key[key]
                by_key[key] = Broker(
                    name=ex.name or broker.name,
                    servers=_union_servers(ex.servers, broker.servers),
                    source=ex.source)
            else:
                by_key[key] = broker
                order.append(key)
    return [by_key[k] for k in order]


def _sort_demo_first(servers: tuple[BrokerServer, ...]) -> list[BrokerServer]:
    """demo first, then real, then unknown; within each group alphabetical by
    normalized name."""
    groups: dict[str, list[BrokerServer]] = {"demo": [], "real": [], "unknown": []}
    for s in servers:
        groups.setdefault(s.type, []).append(s)
    out: list[BrokerServer] = []
    for t in ("demo", "real", "unknown"):
        out.extend(sorted(groups.get(t, []), key=lambda s: _norm(s.name)))
    return out


class BrokerCatalog:
    """Merged broker->servers map from default + live + learned sources.

    `(Previously used)` (learned servers) is surfaced as a pseudo-broker listed
    first when non-empty; real brokers follow, alphabetical by name. Servers for
    a broker are returned demo-first. Pure logic — no Qt, no network."""

    def __init__(self, default=(), live=(), learned_servers=()):
        self._learned: list[str] = []
        merged = _merge_brokers([list(default), list(live)])
        self._brokers: list[Broker] = sorted(merged, key=lambda b: _norm(b.name))
        for s in learned_servers:
            self.add_learned(s)

    @property
    def brokers(self) -> list[Broker]:
        out: list[Broker] = []
        if self._learned:
            out.append(Broker(
                name=PREVIOUSLY_USED,
                servers=tuple(BrokerServer(s, "unknown") for s in self._learned),
                source="learned"))
        out.extend(self._brokers)
        return out

    def broker_names(self) -> list[str]:
        return [b.name for b in self.brokers]

    def servers_for(self, broker_name: str) -> list[BrokerServer]:
        key = _norm(broker_name)
        for b in self.brokers:
            if _norm(b.name) == key:
                return _sort_demo_first(b.servers)
        return []

    def add_learned(self, server: str) -> None:
        """Append a previously-used server (dedup) to the `(Previously used)`
        pseudo-broker, in memory. Persistence is the controller's job via
        learned.py."""
        server = (server or "").strip()
        if not server or server in self._learned:
            return
        self._learned.append(server)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest manager/tests/test_catalog.py -q`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add manager/brokers/__init__.py manager/brokers/catalog.py manager/tests/test_catalog.py
git commit -m "feat(brokers): pure BrokerCatalog merge/dedup/demo-first sort" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Shipped default snapshot + loader

**Files:**
- Create: `manager/data/brokers_default.json`
- Create: `manager/brokers/default.py`
- Modify: `pyproject.toml` (add package-data so the JSON ships in the wheel)
- Test: `manager/tests/test_default.py`

**Interfaces:**
- Consumes: `manager.brokers.catalog.parse_brokers_json` (from Task 1), `Broker`.
- Produces: `manager.brokers.default.load_default(path: Path | None = None) -> list[Broker]`.

- [ ] **Step 1: Write the failing test** — `manager/tests/test_default.py`

```python
from manager.brokers.default import load_default
from manager.brokers.catalog import Broker


def test_load_default_returns_brokers_with_servers():
    brokers = load_default()
    assert brokers
    assert all(isinstance(b, Broker) for b in brokers)
    # the shipped snapshot is non-empty and has at least one demo server
    assert any(b.servers for b in brokers)
    assert any(s.type == "demo" for b in brokers for s in b.servers)


def test_load_default_missing_file_returns_empty(tmp_path):
    assert load_default(tmp_path / "nope.json") == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest manager/tests/test_default.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.brokers.default'`

- [ ] **Step 3: Write the data file** — `manager/data/brokers_default.json`

A shipped snapshot of the TradeVPS community list (TradeVPS response shape) so first run works with no network. Refresh before release with `scripts/update_brokers_default.py` (Task 8). Keep servers realistic and type-tagged.

```json
{
  "brokers": [
    {"id": "icmarkets", "name": "IC Markets", "website": "https://www.icmarkets.com",
     "servers": [
       {"name": "ICMarketsSC-Demo", "type": "demo"},
       {"name": "ICMarketsSC-Live", "type": "real"}
     ]},
    {"id": "pepperstone", "name": "Pepperstone", "website": "https://pepperstone.com",
     "servers": [
       {"name": "Pepperstone-Demo", "type": "demo"},
       {"name": "Pepperstone-Live01", "type": "real"}
     ]},
    {"id": "ftmo", "name": "FTMO", "website": "https://ftmo.com",
     "servers": [
       {"name": "FTMO-Server", "type": "real"}
     ]},
    {"id": "oanda", "name": "OANDA", "website": "https://www.oanda.com",
     "servers": [
       {"name": "OANDA-Demo", "type": "demo"},
       {"name": "OANDA-Live", "type": "real"}
     ]},
    {"id": "metaquotes", "name": "MetaQuotes", "website": "https://metaquotes.net",
     "servers": [
       {"name": "MetaQuotes-Demo", "type": "demo"}
     ]}
  ]
}
```

- [ ] **Step 4: Write the loader** — `manager/brokers/default.py`

```python
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
```

- [ ] **Step 5: Add package-data to pyproject** — `pyproject.toml`

Append this section (so the JSON ships inside the wheel, not just the source tree). The wheel is renamed at install time (see `scripts/install.ps1`), and the loader resolves the path relative to the installed package, so the data file MUST be packaged.

```toml
[tool.setuptools.package-data]
manager = ["data/*.json"]
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest manager/tests/test_default.py -q`
Expected: PASS (2 passed)

- [ ] **Step 7: Commit**

```bash
git add manager/data/brokers_default.json manager/brokers/default.py manager/tests/test_default.py pyproject.toml
git commit -m "feat(brokers): shipped default broker/server snapshot + loader" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Live fetch + cache (best-effort, never raises)

**Files:**
- Create: `manager/brokers/live.py`
- Test: `manager/tests/test_live.py`

**Interfaces:**
- Consumes: nothing from other broker modules (returns raw dicts; the controller parses with `parse_brokers_json`).
- Produces: `URL`, `fetch_live(timeout=10.0) -> dict | None`, `load_cache(path) -> dict | None`, `refresh_cache(path, timeout=10.0, now=None) -> dict | None`, `is_fresh(payload, now) -> bool`, `iso_from_ts(ts) -> str`, `parse_iso(s) -> float`. The module imports `urllib.request` as a module attribute (so tests monkeypatch `live.urllib.request.urlopen`).

- [ ] **Step 1: Write the failing test** — `manager/tests/test_live.py`

```python
import json

from manager.brokers import live

TRADEVPS = {"brokers": [
    {"name": "IC Markets", "servers": [{"name": "ICMarketsSC-Demo", "type": "demo"}]}]}


class _Resp:
    def __init__(self, body=b"{}", status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_live_parses(monkeypatch):
    monkeypatch.setattr(
        live.urllib.request, "urlopen",
        lambda url, timeout=None: _Resp(json.dumps(TRADEVPS).encode("utf-8")))
    assert live.fetch_live(timeout=5.0) == TRADEVPS


def test_fetch_live_returns_none_on_network_error(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("no network")
    monkeypatch.setattr(live.urllib.request, "urlopen", boom)
    assert live.fetch_live(timeout=1.0) is None


def test_fetch_live_returns_none_on_non200(monkeypatch):
    monkeypatch.setattr(
        live.urllib.request, "urlopen",
        lambda url, timeout=None: _Resp(b"{}", status=500))
    assert live.fetch_live(timeout=1.0) is None


def test_fetch_live_returns_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        live.urllib.request, "urlopen",
        lambda url, timeout=None: _Resp(b"not json"))
    assert live.fetch_live(timeout=1.0) is None


def test_refresh_cache_writes_then_loads_round_trip(tmp_path, monkeypatch):
    cache = tmp_path / "brokers_cache.json"
    monkeypatch.setattr(
        live.urllib.request, "urlopen",
        lambda url, timeout=None: _Resp(json.dumps(TRADEVPS).encode("utf-8")))
    payload = live.refresh_cache(cache, timeout=5.0, now=1000.0)
    assert payload is not None
    assert payload["brokers"] == TRADEVPS["brokers"]
    loaded = live.load_cache(cache)
    assert loaded is not None
    assert loaded["brokers"] == TRADEVPS["brokers"]


def test_refresh_cache_failure_leaves_cache_untouched(tmp_path, monkeypatch):
    cache = tmp_path / "brokers_cache.json"
    cache.write_text(json.dumps({"fetched_at": "1970-01-01T00:00:00",
                                 "brokers": []}), encoding="utf-8")

    def boom(url, timeout=None):
        raise OSError("offline")
    monkeypatch.setattr(live.urllib.request, "urlopen", boom)
    assert live.refresh_cache(cache, timeout=1.0) is None
    # existing cache file unchanged
    assert json.loads(cache.read_text(encoding="utf-8"))["brokers"] == []


def test_load_cache_missing_returns_none(tmp_path):
    assert live.load_cache(tmp_path / "nope.json") is None


def test_load_cache_corrupt_returns_none(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("not json", encoding="utf-8")
    assert live.load_cache(p) is None


def test_is_fresh_true_within_24h():
    payload = {"fetched_at": live.iso_from_ts(1000.0)}
    assert live.is_fresh(payload, now=1000.0 + 3600.0) is True


def test_is_fresh_false_after_24h():
    payload = {"fetched_at": live.iso_from_ts(1000.0)}
    assert live.is_fresh(payload, now=1000.0 + 25 * 3600.0) is False


def test_is_fresh_false_when_fetched_at_missing():
    assert live.is_fresh({"fetched_at": ""}, now=1000.0) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest manager/tests/test_live.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.brokers.live'`

- [ ] **Step 3: Write minimal implementation** — `manager/brokers/live.py`

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest manager/tests/test_live.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add manager/brokers/live.py manager/tests/test_live.py
git commit -m "feat(brokers): best-effort TradeVPS live fetch + cache (never raises)" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Learned servers + settings store field

**Files:**
- Create: `manager/brokers/learned.py`
- Modify: `manager/settings/store.py` (add `learned_servers` setdefault in `load`)
- Test: `manager/tests/test_learned.py`
- Modify: `manager/tests/test_settings_store.py` (one existing full-equality test must now include `learned_servers`)

**Interfaces:**
- Consumes: a `SettingsStore` (duck-typed: `.load() -> dict`, `.save(dict)`).
- Produces: `manager.brokers.learned.load(store) -> list[str]`, `record(store, server: str) -> None`.

- [ ] **Step 1: Write the failing test** — `manager/tests/test_learned.py`

```python
from manager.brokers import learned
from manager.settings.store import SettingsStore


def _store(tmp_path):
    return SettingsStore(path=tmp_path / "settings.json")


def test_load_empty_when_absent(tmp_path):
    assert learned.load(_store(tmp_path)) == []


def test_record_then_load_persists(tmp_path):
    s = _store(tmp_path)
    learned.record(s, "ICMarketsSC-Demo")
    assert learned.load(s) == ["ICMarketsSC-Demo"]
    # persisted across a new store instance pointing at the same file
    assert learned.load(SettingsStore(path=tmp_path / "settings.json")) == \
           ["ICMarketsSC-Demo"]


def test_record_dedups(tmp_path):
    s = _store(tmp_path)
    learned.record(s, "A")
    learned.record(s, "A")
    learned.record(s, "B")
    assert learned.load(s) == ["A", "B"]


def test_record_ignores_blank(tmp_path):
    s = _store(tmp_path)
    learned.record(s, "   ")
    assert learned.load(s) == []


def test_record_preserves_other_settings(tmp_path):
    s = _store(tmp_path)
    s.save({"accounts": {"master": {"login": 1}}, "provisioned_instances": [],
            "global": {}})
    learned.record(s, "A")
    loaded = s.load()
    assert loaded["accounts"] == {"master": {"login": 1}}
    assert loaded["learned_servers"] == ["A"]
```

- [ ] **Step 2: Write the settings-store test addition** — edit `manager/tests/test_settings_store.py`

This is a behavior change to `store.load` (it now defaults `learned_servers`), so the one existing full-equality round-trip test must include the new field. Replace the body of `test_save_then_load_round_trip`:

```python
def test_save_then_load_round_trip(tmp_path):
    store = _store(tmp_path)
    data = {"accounts": {"master": {"login": 5001, "server": "Demo-Server"}},
            "provisioned_instances": [], "global": {"heartbeat_seconds": 5},
            "learned_servers": []}
    store.save(data)
    assert store.load() == data
```

And add a new test asserting the default appears for a present file that omits it:

```python
def test_load_defaults_learned_servers_list(tmp_path):
    store = _store(tmp_path)
    store.save({"accounts": {}, "provisioned_instances": [], "global": {}})
    assert store.load()["learned_servers"] == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_learned.py manager/tests/test_settings_store.py -q`
Expected: FAIL — `test_learned_*` with `ModuleNotFoundError: No module named 'manager.brokers.learned'`; `test_load_defaults_learned_servers_list` with `KeyError: 'learned_servers'`; `test_save_then_load_round_trip` with inequality (load now adds the field — but only after Step 4, so at this point it still fails on the learned module import / KeyError).

- [ ] **Step 4: Write minimal implementation** — `manager/brokers/learned.py`

```python
# manager/brokers/learned.py
from __future__ import annotations


def load(store) -> list[str]:
    """Return the previously-used server names from the settings store, or []
    if absent/corrupt. These are non-secret public server names."""
    data = store.load()
    servers = data.get("learned_servers", [])
    if not isinstance(servers, list):
        return []
    return [s for s in servers if isinstance(s, str)]


def record(store, server: str) -> None:
    """Append a server to the learned_servers list (dedup, preserved order) and
    persist. Blank servers are ignored. Other settings fields are preserved."""
    server = (server or "").strip()
    if not server:
        return
    data = store.load()
    servers = data.get("learned_servers", [])
    if not isinstance(servers, list):
        servers = []
    if server in servers:
        return
    servers.append(server)
    data["learned_servers"] = servers
    store.save(data)
```

- [ ] **Step 5: Add the field default to the store** — `manager/settings/store.py`

In `load`, after the existing `setdefault` lines, add `learned_servers`:

```python
        data.setdefault("accounts", {})
        data.setdefault("provisioned_instances", [])
        data.setdefault("global", {})
        data.setdefault("learned_servers", [])
        return data
```

(The missing-file and corrupt-JSON paths still return `{}` early, before these setdefaults — unchanged, matching the existing `accounts`/`global` behavior.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest manager/tests/test_learned.py manager/tests/test_settings_store.py -q`
Expected: PASS (all learned + settings-store tests; the existing settings-store suite remains green).

- [ ] **Step 7: Run the full headless suite to confirm no regressions**

Run: `python -m pytest -q`
Expected: green, no new failures. (Predicted ~183 + 13 (catalog) + 2 (default) + 0 live-headless-only? live tests run headless +11, learned +5, settings +1 added ≈ passed count rises; the HARD GATE is "no new failures" — report the actual count.)

- [ ] **Step 8: Commit**

```bash
git add manager/brokers/learned.py manager/settings/store.py manager/tests/test_learned.py manager/tests/test_settings_store.py
git commit -m "feat(brokers): learned servers + settings store learned_servers field" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Controller catalog + learned hook + supervisor status callback

**Files:**
- Modify: `manager/app/controller.py` (imports, `__init__` state, `get_catalog`/`_build_catalog`/`refresh_brokers`/`_cache_path`/`_on_worker_status`, wire `on_status_msg` in `build_supervisor`)
- Modify: `manager/supervisor.py` (add `on_status_msg` attr + fire on `StatusMsg`)
- Test: `manager/tests/test_controller.py` (extend with 3 tests)

**Interfaces:**
- Consumes: `manager.brokers.catalog` (`BrokerCatalog`, `parse_brokers_json`), `manager.brokers.default` (`load_default`), `manager.brokers.live` (`load_cache`, `is_fresh`, `refresh_cache`), `manager.brokers.learned` (`load`, `record`). `StatusMsg` (already has a `.server` field).
- Produces: `CopyController.get_catalog() -> BrokerCatalog`, `CopyController.refresh_brokers() -> BrokerCatalog`, `CopyController._on_worker_status(name, role, msg)`, `Supervisor.on_status_msg` callback `(name, role, StatusMsg) -> None`.

- [ ] **Step 1: Write the failing tests** — append to `manager/tests/test_controller.py`

```python
def test_worker_status_records_learned_server_once(tmp_path):
    from manager.settings.store import SettingsStore
    from manager.brokers import learned
    from manager.ipc.messages import StatusMsg
    store = SettingsStore(path=tmp_path / "settings.json")
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store)
    msg = StatusMsg(source_id="master", role="master", connected=True,
                    login=1, balance=0.0, equity=0.0, currency="USD",
                    server="ICMarketsSC-Demo")
    c._on_worker_status("master", "master", msg)
    assert learned.load(store) == ["ICMarketsSC-Demo"]
    # a second status for the same server does not duplicate / re-record
    c._on_worker_status("master", "master", msg)
    assert learned.load(store) == ["ICMarketsSC-Demo"]


def test_worker_status_records_distinct_servers(tmp_path):
    from manager.settings.store import SettingsStore
    from manager.brokers import learned
    from manager.ipc.messages import StatusMsg
    store = SettingsStore(path=tmp_path / "settings.json")
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store)
    c._on_worker_status("s1", "slave", StatusMsg(
        source_id="s1", role="slave", connected=True, login=2, balance=0.0,
        equity=0.0, currency="USD", server="A-Demo"))
    c._on_worker_status("master", "master", StatusMsg(
        source_id="master", role="master", connected=True, login=1, balance=0.0,
        equity=0.0, currency="USD", server="B-Demo"))
    assert learned.load(store) == ["A-Demo", "B-Demo"]


def test_worker_status_ignores_empty_server(tmp_path):
    from manager.settings.store import SettingsStore
    from manager.brokers import learned
    from manager.ipc.messages import StatusMsg
    store = SettingsStore(path=tmp_path / "settings.json")
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store)
    c._on_worker_status("master", "master", StatusMsg(
        source_id="master", role="master", connected=True, login=1, balance=0.0,
        equity=0.0, currency="USD", server=""))
    assert learned.load(store) == []


def test_build_supervisor_wires_status_hook():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    sup = c.build_supervisor(heartbeat_seconds=5)
    # bound-method equality is identity, like the existing kill_terminal test
    assert sup.on_status_msg == c._on_worker_status
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest manager/tests/test_controller.py -q -k "worker_status or build_supervisor_wires"`
Expected: FAIL with `AttributeError: 'CopyController' object has no attribute '_on_worker_status'` / `'Supervisor' object has no attribute 'on_status_msg'`

- [ ] **Step 3: Add the supervisor callback** — `manager/supervisor.py`

In `Supervisor.__init__`, after the `self.on_restart = None` line, add:

```python
        self.on_restart = None  # callback(name, role) for GUI status (Plan 4)
        self.on_status_msg = None  # callback(name, role, StatusMsg) for the
                                   # learned-server hook (broker browser plan)
```

In `_dispatch_slave`, in the `StatusMsg` branch, fire the hook after `apply_status`:

```python
        elif isinstance(msg, StatusMsg):
            self._engine.apply_status(slave_id, msg)
            if self.on_status_msg is not None:
                self.on_status_msg(slave_id, "slave", msg)
```

In `_read_master`, add a `StatusMsg` branch (the master sends one `StatusMsg` at the start of its loop; it was previously ignored). Insert this `elif` between the `SnapshotMsg` and `ErrorMsg` branches:

```python
            if isinstance(msg, SnapshotMsg):
                self._last_snapshot_ts = self._time_fn()
                self.heartbeat_warning = False
                snap = Snapshot(timestamp=msg.timestamp, heartbeat=msg.heartbeat,
                                positions=msg.positions)
                cmds = self._engine.ingest_snapshot(snap, now=msg.timestamp)
                for slave_id, clist in cmds.items():
                    for cmd in clist:
                        self._send(slave_id, cmd)
            elif isinstance(msg, StatusMsg):
                if self.on_status_msg is not None:
                    self.on_status_msg("master", "master", msg)
            elif isinstance(msg, ErrorMsg):
                self.errors.append(f"master: {msg.message}")
            return True
```

- [ ] **Step 4: Wire the controller** — `manager/app/controller.py`

Add imports near the top (after the existing `from manager.settings.store import SettingsStore`):

```python
from pathlib import Path

from manager.brokers import catalog as _catalog_mod
from manager.brokers import default as _default_mod
from manager.brokers import learned as _learned_mod
from manager.brokers import live as _live_mod
from manager.brokers.catalog import BrokerCatalog
```

In `CopyController.__init__`, after `self._clock = clock`, add:

```python
        self._clock = clock
        self._catalog: BrokerCatalog | None = None
        self._recorded_servers: set[str] = set()
```

In `build_supervisor`, set the hook on the built supervisor (after the `sup.on_restart = ...` line):

```python
        sup.on_restart = lambda name, role: self._status(
            "info", f"restarted {role} {name}")
        sup.on_status_msg = self._on_worker_status
        return sup
```

Add the catalog + learned methods to the class (place them after the `discover_instances` method, before `prepare`):

```python
    # ---- broker catalog (broker/server browser) ----
    def _cache_path(self) -> Path:
        # same directory the settings store writes to
        return self._store.path.parent / "brokers_cache.json"

    def get_catalog(self) -> BrokerCatalog:
        """The merged broker catalog (default + fresh live cache + learned),
        built lazily and cached. The GUI pickers call this to populate."""
        if self._catalog is None:
            self._catalog = self._build_catalog()
        return self._catalog

    def _build_catalog(self) -> BrokerCatalog:
        default_brokers = _default_mod.load_default()
        live_brokers: list = []
        cache = _live_mod.load_cache(self._cache_path())
        if cache is not None and _live_mod.is_fresh(cache, self._clock()):
            live_brokers = _catalog_mod.parse_brokers_json(cache, "live")
        learned_servers = _learned_mod.load(self._store)
        return BrokerCatalog(default=default_brokers, live=live_brokers,
                             learned_servers=learned_servers)

    def refresh_brokers(self) -> BrokerCatalog:
        """Best-effort refresh of the community broker list (called from the
        GUI Refresh button, off the GUI thread). Fetches live, writes the cache,
        rebuilds the catalog. On failure the cache is untouched and a warning
        is logged; the catalog is still rebuilt from default + cache + learned.
        Never raises."""
        payload = _live_mod.refresh_cache(self._cache_path(), timeout=10.0,
                                          now=self._clock())
        if payload is None:
            self._log("community broker list unavailable; using cached/default list")
        self._catalog = self._build_catalog()
        return self._catalog

    def _on_worker_status(self, name: str, role: str, msg) -> None:
        """Record the server a worker logged into, once per distinct server, so
        it appears under '(Previously used)' on the next launch. Runs on the
        supervisor's daemon thread; the settings store writes atomically."""
        server = (getattr(msg, "server", "") or "").strip()
        if not server or server in self._recorded_servers:
            return
        self._recorded_servers.add(server)
        _learned_mod.record(self._store, server)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest manager/tests/test_controller.py -q`
Expected: PASS (existing controller tests + 4 new).

- [ ] **Step 6: Run the full headless suite to confirm no regressions**

Run: `python -m pytest -q`
Expected: green, no new failures. (`test_supervisor*.py` must still pass — the `on_status_msg` addition is opt-in, defaulting to None.)

- [ ] **Step 7: Commit**

```bash
git add manager/app/controller.py manager/supervisor.py manager/tests/test_controller.py
git commit -m "feat(controller): broker catalog + learned-server hook on worker status" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: BrokerServerPicker widget

**Files:**
- Create: `manager/gui/server_picker.py`
- Test: `manager/tests/test_server_picker.py`

**Interfaces:**
- Consumes: a controller exposing `get_catalog() -> BrokerCatalog` and `refresh_brokers() -> BrokerCatalog`; `BrokerCatalog.broker_names()` / `servers_for(name)`. The widget only needs those two methods (a fake controller with just them is used in tests).
- Produces: `BrokerServerPicker(controller, parent=None)` with `broker_combo`, `server_combo`, `refresh_button` attributes and `server() -> str`, `set_server(name)`, `set_broker(name)` methods.

- [ ] **Step 1: Write the failing test** — `manager/tests/test_server_picker.py`

```python
import pytest

pytest.importorskip("PySide6")

from manager.brokers.catalog import Broker, BrokerServer, BrokerCatalog


class FakeController:
    def __init__(self, catalog):
        self._catalog = catalog

    def get_catalog(self):
        return self._catalog

    def refresh_brokers(self):
        return self._catalog


def _catalog():
    return BrokerCatalog(
        default=[Broker(
            "IC Markets",
            (BrokerServer("ICMarketsSC-Demo", "demo"),
             BrokerServer("ICMarketsSC-Live", "real")), "default")],
        learned_servers=["MyOldServer"])


def test_picker_populates_brokers(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    names = [p.broker_combo.itemText(i) for i in range(p.broker_combo.count())]
    assert "(Previously used)" in names
    assert "IC Markets" in names


def test_picker_servers_demo_first(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    p.set_broker("IC Markets")
    items = [p.server_combo.itemData(i) for i in range(p.server_combo.count())]
    assert items == ["ICMarketsSC-Demo", "ICMarketsSC-Live"]  # demo first


def test_picker_server_returns_raw_name_no_label(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    p.set_broker("IC Markets")
    p.set_server("ICMarketsSC-Live")
    assert p.server() == "ICMarketsSC-Live"  # no "(real)" suffix


def test_picker_free_text_server(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    p.server_combo.setEditText("MyCustomServer")
    assert p.server() == "MyCustomServer"


def test_picker_strips_label_from_free_text(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    p.server_combo.setEditText("SomeServer (demo)")
    assert p.server() == "SomeServer"


def test_picker_empty_catalog_allows_free_text(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(BrokerCatalog()))
    p.server_combo.setEditText("Anything")
    assert p.server() == "Anything"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest manager/tests/test_server_picker.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.gui.server_picker'` (or SKIPPED without PySide6 — fine; on a PySide6 host it FAILs on import).

- [ ] **Step 3: Write minimal implementation** — `manager/gui/server_picker.py`

```python
# manager/gui/server_picker.py
from __future__ import annotations

import re

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QFormLayout, QComboBox, QPushButton,
)

from manager.brokers.catalog import PREVIOUSLY_USED

# trailing " (demo|real|unknown|manual)" label added to dropdown display text
_LABEL_RE = re.compile(r"\s*\((demo|real|unknown|manual)\)\s*$")
_MANUAL = "(manual)"


def _strip_label(text: str) -> str:
    return _LABEL_RE.sub("", text or "").strip()


class _RefreshWorker(QThread):
    """Runs controller.refresh_brokers off the GUI thread; emits the new
    BrokerCatalog on done (Qt marshals the signal back to the GUI thread)."""
    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        self.done.emit(self._fn())


class BrokerServerPicker(QWidget):
    """A reusable Broker -> Server picker: an editable Broker combo, an
    editable Server combo (demo-first, display "<server> (demo|real)" with the
    raw name held as item user-data), and a Refresh button (best-effort live
    refresh off-thread). Used by the master form and the slave editor.

    The widget is purely a name selector — it holds no credentials and never
    logs in. ``server()`` returns the raw server name (selected item's
    user-data, or typed free-text with any label suffix stripped)."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._refresh_worker = None

        form = QFormLayout(self)
        self.broker_combo = QComboBox()
        self.broker_combo.setEditable(True)
        self.server_combo = QComboBox()
        self.server_combo.setEditable(True)
        self.refresh_button = QPushButton("Refresh")

        broker_row = type(self)._row(self.broker_combo, self.refresh_button)
        form.addRow("Broker", broker_row)
        form.addRow("Server", self.server_combo)

        self.broker_combo.currentIndexChanged.connect(self._on_broker_changed)
        self.refresh_button.clicked.connect(self._on_refresh)

        self._populate_brokers()

    @staticmethod
    def _row(*widgets):
        from PySide6.QtWidgets import QHBoxLayout
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            row.addWidget(w)
        return row

    # ---- population ----
    def _catalog(self):
        return self._controller.get_catalog()

    def _populate_brokers(self, preserve_server: str = ""):
        prev_broker = self.broker_combo.currentText()
        prev_server = preserve_server or self.server_combo.currentText()
        self.broker_combo.blockSignals(True)
        self.broker_combo.clear()
        names = self._catalog().broker_names()
        if not names:
            names = [_MANUAL]  # no brokers at all -> free-text only
        for n in names:
            self.broker_combo.addItem(n)
        idx = self.broker_combo.findText(prev_broker)
        if idx >= 0:
            self.broker_combo.setCurrentIndex(idx)
        elif prev_broker:
            self.broker_combo.setEditText(prev_broker)
        self.broker_combo.blockSignals(False)
        self._populate_servers(prev_server)

    def _on_broker_changed(self, _idx):
        # broker changed -> show that broker's servers (demo-first); do not
        # carry the previous server (it belongs to another broker)
        self._populate_servers("")

    def _populate_servers(self, preserve: str):
        broker = self.broker_combo.currentText()
        if broker and broker != _MANUAL:
            servers = self._catalog().servers_for(broker)
        else:
            servers = []
        self.server_combo.blockSignals(True)
        self.server_combo.clear()
        for s in servers:
            self.server_combo.addItem(f"{s.name} ({s.type})", s.name)
        idx = self.server_combo.findData(preserve) if preserve else -1
        if idx >= 0:
            self.server_combo.setCurrentIndex(idx)
        elif servers:
            self.server_combo.setCurrentIndex(0)  # demo-first default
        elif preserve:
            self.server_combo.setEditText(preserve)
        self.server_combo.blockSignals(False)

    # ---- public API ----
    def server(self) -> str:
        """Raw server name: the selected item's user-data if a dropdown item is
        selected, else the typed free-text with any trailing label stripped."""
        idx = self.server_combo.currentIndex()
        data = self.server_combo.itemData(idx)
        if isinstance(data, str) and data:
            return data
        return _strip_label(self.server_combo.currentText())

    def set_server(self, name: str) -> None:
        """Pre-fill the server for an existing account: select the matching
        dropdown item if present, else set it as free text."""
        idx = self.server_combo.findData(name)
        if idx >= 0:
            self.server_combo.setCurrentIndex(idx)
        else:
            self.server_combo.setEditText(name)

    def set_broker(self, name: str) -> None:
        idx = self.broker_combo.findText(name)
        if idx >= 0:
            self.broker_combo.setCurrentIndex(idx)
        else:
            self.broker_combo.setEditText(name)

    # ---- refresh (best-effort, off-thread) ----
    def _on_refresh(self):
        self._refresh_worker = _RefreshWorker(self._controller.refresh_brokers,
                                               self)
        self._refresh_worker.done.connect(self._on_refresh_done)
        self.refresh_button.setEnabled(False)
        self._refresh_worker.start()

    def _on_refresh_done(self, _catalog):
        self._refresh_worker = None
        self.refresh_button.setEnabled(True)
        # repopulate, preserving the current broker + server if still present
        self._populate_brokers()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest manager/tests/test_server_picker.py -q`
Expected: PASS on a PySide6 host (6 passed); SKIPPED cleanly on a headless host (module-level `importorskip`). Either is acceptable for the gate; on the headless host confirm the module is collected and skipped, not errored.

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python -m pytest -q`
Expected: green, no new failures (the new module adds 1 skip on a headless host).

- [ ] **Step 6: Commit**

```bash
git add manager/gui/server_picker.py manager/tests/test_server_picker.py
git commit -m "feat(gui): BrokerServerPicker widget (broker/server combos + refresh)" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Wire the picker into the master form and slave editor

**Files:**
- Modify: `manager/gui/main_window.py` (replace `master_server` QLineEdit with the picker; update `_on_start`)
- Modify: `manager/gui/slave_editor.py` (replace `server` QLineEdit with the picker; update `spec()`)
- Modify: `manager/tests/test_main_window.py` (FakeController gains `get_catalog`/`refresh_brokers`; replace `master_server` assertions with `master_picker`)
- Modify: `manager/tests/test_slave_editor.py` (FakeController gains `get_catalog`/`refresh_brokers`; replace `server` assertions with `_picker`)

**Interfaces:**
- Consumes: `BrokerServerPicker` (Task 6) and the controller's `get_catalog()` / `refresh_brokers()` (Task 5).
- Produces: `MainWindow.master_picker` (a `BrokerServerPicker`), `SlaveEditor._picker` (a `BrokerServerPicker`). `AccountSpec.server` is still a plain `str`; `picker.server()` supplies it.

- [ ] **Step 1: Update the master-form tests** — `manager/tests/test_main_window.py`

Add `get_catalog` / `refresh_brokers` to `FakeController`:

```python
class FakeController:
    """Minimal controller double for construction + wiring smoke tests."""
    def __init__(self):
        self.started = False
        self.stopped = False
        self._instances = []
    def discover_instances(self):
        return self._instances
    def start(self, master, slaves, **kw):
        self.started = True
    def stop(self):
        self.stopped = True
    def is_running(self):
        return self.started and not self.stopped
    def get_catalog(self):
        from manager.brokers.catalog import BrokerCatalog
        return BrokerCatalog()
    def refresh_brokers(self):
        return self.get_catalog()
```

In `test_main_window_constructs`, replace `assert w.master_server is not None` with:

```python
    assert w.master_picker is not None
```

In `test_start_button_calls_controller_start`, replace `w.master_server.setText("Demo")` with:

```python
    w.master_picker.set_server("Demo")
```

- [ ] **Step 2: Update the slave-editor tests** — `manager/tests/test_slave_editor.py`

Add `get_catalog` / `refresh_brokers` to `FakeController`:

```python
class FakeController:
    def __init__(self, instances=None):
        self._instances = instances or []
    def discover_instances(self):
        return self._instances
    def get_catalog(self):
        from manager.brokers.catalog import BrokerCatalog
        return BrokerCatalog()
    def refresh_brokers(self):
        return self.get_catalog()
```

In `test_slave_editor_constructs`, replace `assert dlg.server is not None` with:

```python
    assert dlg._picker is not None
```

In `test_slave_editor_spec_returns_accountspec`, replace `dlg.server.setText("Demo")` with:

```python
    dlg._picker.set_server("Demo")
```

(`test_slave_editor_symbol_table_round_trips_into_csv` calls `_spec_from_fields(..., "Demo", ...)` directly with a literal server string — no change needed.)

- [ ] **Step 3: Run the tests to verify they fail (red against the not-yet-wired GUI)**

Run: `python -m pytest manager/tests/test_main_window.py manager/tests/test_slave_editor.py -q`
Expected: FAIL with `AttributeError: 'MainWindow' object has no attribute 'master_picker'` / `'SlaveEditor' object has no attribute '_picker'` (on a PySide6 host; SKIPPED on headless).

- [ ] **Step 4: Wire main_window** — `manager/gui/main_window.py`

Add the import near the top (with the other `manager.gui` / `manager.app` imports):

```python
from manager.gui.server_picker import BrokerServerPicker
```

In `_build_ui`, in the Master pane, replace these two lines:

```python
        self.master_server = QLineEdit()
        self.master_server.setPlaceholderText("server name")
```

with nothing (delete them), and replace:

```python
        mform.addRow("Login", self.master_login)
        mform.addRow("Server", self.master_server)
```

with:

```python
        mform.addRow("Login", self.master_login)
        self.master_picker = BrokerServerPicker(self._controller)
        mform.addRow(self.master_picker)
```

(`QLineEdit` is still imported and used for `master_login` / `master_password` — leave the import.)

In `_on_start`, replace:

```python
            server=self.master_server.text().strip(),
```

with:

```python
            server=self.master_picker.server(),
```

- [ ] **Step 5: Wire slave_editor** — `manager/gui/slave_editor.py`

Add the import near the top:

```python
from manager.gui.server_picker import BrokerServerPicker
```

In `_build_ui`, replace:

```python
        self.server = QLineEdit()
```

with nothing (delete it), and replace:

```python
        form.addRow("Server", self.server)
```

with:

```python
        self._picker = BrokerServerPicker(self._controller)
        form.addRow(self._picker)
```

In `spec()`, replace:

```python
        return self._spec_from_fields(
            self.id_edit.text().strip() or "s1",
            self.login.text().strip(), self.server.text().strip(),
            self.password.text(), self.step_amount.text(),
            self.step_size.text(), self.max_lot.text(),
            self.max_trade_age_minutes.text(),
            self.normalize_sltp.isChecked())
```

with:

```python
        return self._spec_from_fields(
            self.id_edit.text().strip() or "s1",
            self.login.text().strip(), self._picker.server(),
            self.password.text(), self.step_amount.text(),
            self.step_size.text(), self.max_lot.text(),
            self.max_trade_age_minutes.text(),
            self.normalize_sltp.isChecked())
```

(`QLineEdit` is still used for `id_edit` / `login` / `password` / lot-sizing — leave the import.)

- [ ] **Step 6: Run the GUI tests to verify they pass**

Run: `python -m pytest manager/tests/test_main_window.py manager/tests/test_slave_editor.py -q`
Expected: PASS on a PySide6 host; SKIPPED cleanly on a headless host.

- [ ] **Step 7: Run the full suite to confirm no regressions**

Run: `python -m pytest -q`
Expected: green, no new failures. The GUI test modules still skip cleanly on a headless host (module-level `importorskip`); on a PySide6 host they run and pass.

- [ ] **Step 8: Commit**

```bash
git add manager/gui/main_window.py manager/gui/slave_editor.py manager/tests/test_main_window.py manager/tests/test_slave_editor.py
git commit -m "feat(gui): wire BrokerServerPicker into master form + slave editor" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Maintainer regen script + docs

**Files:**
- Create: `scripts/update_brokers_default.py`
- Modify: `README.md` (Features, File layout, scripts list)
- Modify: `docs/TESTING.md` (suite-layout table rows)

**Interfaces:**
- Produces: `scripts/update_brokers_default.py` (maintainer-only CLI; not run in CI). No test (verified by execution, like the install.ps1 task in the release plan).

- [ ] **Step 1: Write the regen script** — `scripts/update_brokers_default.py`

```python
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
```

- [ ] **Step 2: Update README** — `README.md`

In the **Features** list, add a bullet (after the "Restart recovery" bullet):

```markdown
- **Browsable broker/server picker** — pick an MT5 server by broker name
  (the thing you remember) instead of typing the exact server string. Backed
  by a shipped community broker list, a best-effort live refresh, and your
  previously-used servers; free-text entry still works for anything not listed.
```

In the **File layout** block, insert the new packages/files in their alphabetical positions:

```
  brokers/
    catalog.py          Broker catalog: merge/dedup/demo-first sort (pure, no Qt)
    default.py          Loads the shipped brokers_default.json snapshot
    live.py             TradeVPS community-list fetch + cache (best-effort)
    learned.py          Record/retrieve previously-used servers from settings
  data/
    brokers_default.json   Shipped TradeVPS broker/server snapshot
```

and in the `gui/` section add:

```
    server_picker.py   Broker→Server picker (Broker combo + Server combo + Refresh)
```

and in the `scripts/` section add:

```
  update_brokers_default.py  Maintainer: refresh the shipped broker snapshot from TradeVPS
```

- [ ] **Step 3: Update TESTING** — `docs/TESTING.md`

In the **Suite layout** table, add rows (in a sensible position, e.g. after the `test_controller.py` row):

```markdown
| `test_catalog.py`, `test_default.py`, `test_live.py`, `test_learned.py` | Broker catalog merge/dedup/demo-first sort, shipped snapshot loader, live fetch + cache (best-effort, never raises), learned-servers persistence |
| `test_server_picker.py` | BrokerServerPicker: broker→server population, demo-first, free-text server entry (skip without PySide6) |
```

- [ ] **Step 4: Verify the docs/script don't break the suite**

Run: `python -m pytest -q`
Expected: green, no new failures (docs + a maintainer script touch no runtime code). Confirm the regen script is syntactically valid: `python -c "import ast; ast.parse(open('scripts/update_brokers_default.py',encoding='utf-8').read())"`.

- [ ] **Step 5: Commit**

```bash
git add scripts/update_brokers_default.py README.md docs/TESTING.md
git commit -m "docs(brokers): regen script + README/TESTING for broker/server browser" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage** — checking each spec section against tasks:

- Goal (pick server by broker name, replace free-text Server field): Tasks 6 + 7 (widget replaces `QLineEdit` in both forms). ✅
- Background & constraints (no MetaQuotes endpoint, TradeVPS API, security constraints verbatim): Global Constraints. ✅
- Architecture (brokers/ package split, data file, server_picker.py, store field): File Structure + Tasks 1–7. ✅
- Data model (`BrokerServer`, `Broker`, `BrokerCatalog`, merge rule, demo-first, `(Previously used)` first, `add_learned`): Task 1. ✅
- Sources: shipped default JSON (Task 2), live cache `fetch_live`/`load_cache`/`refresh_cache` + 24h freshness + cache path = settings dir / brokers_cache.json (Task 3 + controller `_cache_path` Task 5), learned `record`/`load` + `learned_servers` field + populated from `account_info.server` on worker ready (Tasks 4 + 5). ✅
- GUI `BrokerServerPicker`: editable broker combo (type-to-filter, `(Previously used)` first then alpha, `(manual)` fallback when empty), editable server combo (demo-first, display `"<server> (type)"`, raw name in itemData, free-text on Enter), Refresh button (best-effort, leaves list + warns on failure), `server()`/`set_server()`/`set_broker()`, no credentials. Task 6. ✅
- Wiring (main_window replaces `master_server`; slave_editor replaces `server`; `AccountSpec.server` unchanged): Task 7. ✅
- Data flow (app start builds catalog from default+cache+learned; background refresh; configure→picker.server()→AccountSpec→worker initialize unchanged; learn on worker ready): Tasks 5 (controller get_catalog/refresh_brokers/_on_worker_status + supervisor hook) + 6 (refresh worker off-thread) + 7. ✅
- Error handling (live fail→None→default+cache+learned+warn; cache corrupt→ignored; no brokers→`(manual)`+free-text; broker with zero servers→listed+empty editable combo; refresh offline→leave list+log "community list unavailable", no modal): Tasks 1 (`(manual)` only when broker_names empty — picker adds it), 3 (None returns), 5 (`_log` warning), 6 (refresh leaves list, no modal). ✅
- Testing (test_catalog, test_live, test_learned, test_server_picker, extend test_controller; headless gate = no new failures; PySide6 importorskip): Tasks 1–7. ✅
- YAGNI (no MetaQuotes scraping, no MetaApi, no login→server auto-discovery, no GUI curation): enforced by scope — none of the tasks implement these. ✅
- Open data (shipped snapshot is TradeVPS; brokers not pre-added are reachable by typing once → learned): Task 2 snapshot + Task 4 learned + Task 6 free-text. ✅

**2. Placeholder scan** — searched for TBD/TODO/"implement later"/"add appropriate error handling"/"similar to Task N": none. Every code step contains full, runnable code. ✅

**3. Type consistency** — cross-task name/type check:
- `BrokerServer(name, type)`, `Broker(name, servers: tuple, source)` — defined Task 1, used Tasks 2/6/7. ✅
- `BrokerCatalog(default=(), live=(), learned_servers=())` — Task 1; controller calls `BrokerCatalog(default=..., live=..., learned_servers=...)` (Task 5); tests use `BrokerCatalog(default=[...], learned_servers=[...])` (Task 6). ✅
- `parse_brokers_json(data, source)` — Task 1; used in Task 2 (`load_default`) and Task 5 (`_build_catalog` for live). ✅
- `load_default(path=None) -> list[Broker]` — Task 2; used Task 5 `_build_catalog`. ✅
- `fetch_live`/`load_cache`/`refresh_cache`/`is_fresh`/`iso_from_ts`/`parse_iso` — Task 3 signatures; used Task 5 (`load_cache`, `is_fresh`, `refresh_cache`). ✅
- `learned.load(store) -> list[str]`, `learned.record(store, server)` — Task 4; used Task 5 (`_learned_mod.load`, `_learned_mod.record`). ✅
- `Supervisor.on_status_msg` (Task 5) set in `build_supervisor`; fired in `_dispatch_slave` + `_read_master` with `(name, role, msg)`. Controller `_on_worker_status(name, role, msg)` matches. ✅
- `BrokerServerPicker(controller, parent=None)` + `broker_combo`/`server_combo`/`refresh_button`/`server()`/`set_server()`/`set_broker()` — Task 6; used Task 7 (`self.master_picker = BrokerServerPicker(self._controller)`, `self.master_picker.server()`, `self._picker.set_server(...)`). ✅
- `StatusMsg.server` field — existing (messages.py); used in Task 5 hook. ✅
- `AccountSpec.server` — unchanged; supplied by `picker.server()`. ✅
- Store `learned_servers` setdefault — Task 4 `store.load`; Task 5/learned read/write. ✅

No type/name drift found.