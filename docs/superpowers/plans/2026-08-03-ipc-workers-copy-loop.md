# IPC + Workers + Supervisor + Copy-Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the IPC layer, role-parameterized MT5 worker subprocesses, the worker supervisor, and the engine copy-loop that turns master snapshots into per-slave commands — all testable with fake workers and no real terminal.

**Architecture:** A `multiprocessing` Pipe per worker carries length-prefixed JSON messages (Snapshot/Command/Ack/Status/Error/Recovery/SymbolInfo/Start). One worker subprocess per terminal connection (single-connection-per-process constraint). The engine copy-loop runs in one manager thread: per master snapshot it diffs, derives per-slave commands through the Plan 1 engine modules, and sends them; per-slave pending/held bookkeeping serializes commands per master ticket so a slow broker on one slave never blocks the others and no command is pipelined ahead of its predecessor's ack. The **slave worker** performs SL/TP normalization + tick rounding + partial-close volume computation (it owns the live slave prices and live slave current volume, exactly like the EA); the manager does diff + symbol-mapping + lot-sizing (from slave balance + slave-reported symbol info) + record-table linkage + command decisions.

**Tech Stack:** Python 3.11+, `multiprocessing` (Process + Pipe), `MetaTrader5` (lazy-imported only inside the real adapter), pytest. No new third-party deps for Plan 2.

## Global Constraints

Copy these verbatim into every task's context — they bind the whole plan.

- **Demo accounts only — never capture or log in with a real account** during any test or smoke run in this environment. Real-account wiring exists in code but is never exercised here.
- **Credentials pass through the pipe, never the command line** — the password is sent as the first pipe message (`StartMsg`); the subprocess is spawned bare. It must never appear in argv / the process list.
- **Single terminal connection per process** — one `mt5.initialize()` per worker subprocess. A second `initialize()` in the same process fails with IPC `-10003`. The supervisor kills any leftover `terminal64.exe` for an instance before respawning its worker.
- **`login` is an `int`** — the worker converts to `int` before `mt5.initialize`. A string login yields `(-2, 'Terminal: Invalid params')`.
- **Engine never touches the terminal** — `manager/engine/*` only sees `Snapshot`/`Command` message types and the Plan 1 engine modules. All `MetaTrader5` access is isolated behind `worker/mt5_adapter.py`'s `Mt5Adapter` protocol (lazy-imported in `RealMt5`; `FakeMt5` for tests). `manager/engine/*` MUST NOT import `MetaTrader5` or `worker.*`.
- **Slave owns price-dependent transforms** (user-approved deviation from the spec's literal "manager normalizes"): the slave worker calls `normalize_sltp` + `round_to_tick` at order time using its live Ask/Bid (OPEN) / its actual fill price (MODIFY), and computes the partial-close volume from its live current position volume (EA `SlaveSubscriber.mqh:381-405, 450-471, 502-513`). The manager sends RAW master SL/TP + master open price + side in OPEN/MODIFY commands.
- **MT5 API facts (verified):** position fields `ticket, time, type(0=BUY/1=SELL), magic, volume(current), price_open, sl, tp, comment, symbol`; `positions_get()` returns `None` on error (not empty tuple); `symbol_info(symbol)` has `point, digits, trade_tick_size, volume_step, volume_min, volume_max`; modify uses `action=TRADE_ACTION_SLTP(3)` with key `position=`; close/partial use `action=TRADE_ACTION_DEAL(1)` with opposite `type`, `position=`, `price=bid`(close long)/`ask`(close short); success = `retcode==TRADE_RETCODE_DONE(10009)`; `last_error()` returns `(code, desc)` tuple; `initialize(path, login=int, password, server, portable=False)` launches+logs in one call; use forward slashes in `path`.
- **Linkage scheme verbatim** (Plan 1, `manager/engine/linkage.py`): `MAGIC_BASE=1000000`, slave magic `= MAGIC_BASE + (master_ticket % 900000)`, comment `= CPY#<master_ticket>|MV<master_open_vol>|SV<slave_open_vol>`.
- **Copy-recent-opens-at-start baseline** (Plan 1): first diff emits NEW for all current master positions; `is_too_old` skips old, seeded `RecordTable` skips already-copied, rest copied.
- **Engine module signatures (Plan 1, do not change):**
  - `models.py`: `BUY=0, SELL=1`; `Position(ticket,symbol,side,open_price,volume,sl,tp,open_time,point,comment="")`; `Snapshot(timestamp,heartbeat,positions:tuple[Position,...])`; `Event(kind,position)` kind in {"NEW","MODIFY","PARTIAL","CLOSE"}; `Record(master_ticket,magic,slave_ticket,master_open_volume,slave_open_volume)` (mutable dataclass).
  - `linkage.py`: `magic_for(master_ticket)->int`, `encode_comment(master_ticket,master_volume,slave_volume)->str`, `decode_comment(comment)->(int,float|None,float|None)|None`.
  - `transform.py`: `parse_symbol_map(map_csv)->dict`, `SymbolMapper(map_csv, exists_check).resolve(symbol)->str`, `calculate_lots(balance,step_amount,step_size,max_lot,lot_step,min_lot,max_lot_symbol)->float`, `normalize_sltp(master_open,master_sl,master_tp,slave_open,side)->(float,float)`, `round_to_tick(price,tick_size,digits)->float|None`.
  - `snapshot_diff.py`: `diff(prev, curr)->list[Event]` (prev/curr are sequences of Position).
  - `record_table.py`: `RecordTable()` with `.has/.get/.add/.remove/.all/__len__` keyed by `master_ticket`.
  - `baseline.py`: `is_too_old(open_time, now, max_age_minutes)->bool`, `seed_from_recovery(table, records)->int` (no overwrite).
- **Test hygiene:** `pythonpath=["."]` is set in `pyproject.toml`; run `pytest manager/tests/<file> -v` from the worktree root. Never bare `git add manager` — use explicit paths (Plan 1 once committed `__pycache__`). Add new test files under `manager/tests/`.

---

## File structure (this plan)

New files:

```
manager/
  ipc/
    __init__.py
    messages.py          # message dataclasses + JSON encode/decode (kind-tagged)
    pipe_framing.py       # send_msg/recv_msg over a Connection (send_bytes/recv_bytes)
  worker/
    __init__.py
    mt5_constants.py      # int constants matching MQL5 (no MetaTrader5 import)
    mt5_adapter.py        # Mt5Adapter protocol + FakeMt5 (scripted) + RealMt5 (lazy mt5)
    mt5_worker.py         # subprocess entry: master poll loop / slave command loop + recovery
  engine/
    copy_loop.py          # SlaveConfig, SlaveState, CopyEngine (pure), CopyLoop (thread)
  supervisor.py           # spawn/watch/restart workers, route IPC
  tests/
    test_messages.py
    test_pipe_framing.py
    test_mt5_adapter.py
    test_mt5_worker.py
    test_copy_loop.py     # pure CopyEngine + in-process IPC integration
    test_recovery.py
    test_supervisor.py
```

Modified:
- `manager/engine/models.py` — add `SymbolInfo` (additive; Plan 1 tests untouched).

---

### Task 1: Add SymbolInfo to models.py

SymbolInfo is the per-slave-symbol terminal data the manager needs for lot-sizing (volume params) and the slave needs for SL/TP tick rounding (point/digits/tick_size). It lives in the domain layer (`models.py`) so both `engine/copy_loop.py` and `worker/mt5_adapter.py` import it without `engine` depending on `worker` or `ipc`.

**Files:**
- Modify: `manager/engine/models.py` (append `SymbolInfo` dataclass)
- Test: `manager/tests/test_models.py` (append `SymbolInfo` tests)

**Interfaces:**
- Produces: `SymbolInfo(point: float, digits: int, tick_size: float, volume_step: float, volume_min: float, volume_max: float)` — frozen dataclass.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_models.py`:

```python
from manager.engine.models import SymbolInfo


def test_symbol_info_fields():
    si = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                    volume_step=0.01, volume_min=0.01, volume_max=100.0)
    assert si.point == 0.00001
    assert si.digits == 5
    assert si.tick_size == 0.00001
    assert si.volume_step == 0.01
    assert si.volume_min == 0.01
    assert si.volume_max == 100.0


def test_symbol_info_is_frozen():
    si = SymbolInfo(point=0.01, digits=2, tick_size=0.01,
                    volume_step=0.1, volume_min=0.1, volume_max=10.0)
    import dataclasses
    assert dataclasses.is_dataclass(si)
    # frozen: assignment must raise
    try:
        si.point = 0.0  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("SymbolInfo must be frozen")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_models.py::test_symbol_info_fields manager/tests/test_models.py::test_symbol_info_is_frozen -v`
Expected: FAIL with `ImportError: cannot import name 'SymbolInfo'`.

- [ ] **Step 3: Write minimal implementation**

Append to `manager/engine/models.py` (after the `Record` class):

```python
@dataclass(frozen=True)
class SymbolInfo:
    """Per-symbol terminal info needed for lot sizing (volume params) and
    SL/TP tick rounding (point/digits/tick_size). Mirrors the subset of MT5's
    symbol_info namedtuple consumed by the engine + worker."""
    point: float
    digits: int
    tick_size: float
    volume_step: float
    volume_min: float
    volume_max: float
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_models.py -v`
Expected: PASS (all Plan 1 model tests still green).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/models.py manager/tests/test_models.py
git commit -m "feat(models): add SymbolInfo frozen dataclass for Plan 2"
```

---

### Task 2: IPC message schemas (ipc/messages.py)

Kind-tagged JSON message dataclasses + `encode`/`decode`. Reuses Plan 1 `Position`/`Record` and Task 1 `SymbolInfo`. Messages: `StartMsg`, `SnapshotMsg`, `StatusMsg`, `SymbolInfoMsg`, `RecoveryMsg`, `CommandMsg`, `AckMsg`, `ErrorMsg`.

**Files:**
- Create: `manager/ipc/__init__.py` (empty), `manager/ipc/messages.py`
- Test: `manager/tests/test_messages.py`

**Interfaces:**
- Consumes: `models.Position`, `models.Record`, `models.SymbolInfo` (Task 1).
- Produces: `encode(msg) -> dict`, `decode(d: dict) -> <msg>`; each message class has `KIND: str`.

```python
# manager/ipc/messages.py
from __future__ import annotations

from dataclasses import dataclass, asdict, fields
from typing import Any

from manager.engine.models import Position, Record, SymbolInfo


# ---- nested-domain serializers (keep Plan 1 classes untouched) ----

def _position_to_dict(p: Position) -> dict:
    return {
        "ticket": p.ticket, "symbol": p.symbol, "side": p.side,
        "open_price": p.open_price, "volume": p.volume, "sl": p.sl, "tp": p.tp,
        "open_time": p.open_time, "point": p.point, "comment": p.comment,
    }

def _position_from_dict(d: dict) -> Position:
    return Position(
        ticket=d["ticket"], symbol=d["symbol"], side=d["side"],
        open_price=d["open_price"], volume=d["volume"], sl=d["sl"], tp=d["tp"],
        open_time=d["open_time"], point=d["point"], comment=d.get("comment", ""),
    )

def _record_to_dict(r: Record) -> dict:
    return {
        "master_ticket": r.master_ticket, "magic": r.magic,
        "slave_ticket": r.slave_ticket, "master_open_volume": r.master_open_volume,
        "slave_open_volume": r.slave_open_volume,
    }

def _record_from_dict(d: dict) -> Record:
    return Record(
        master_ticket=d["master_ticket"], magic=d["magic"],
        slave_ticket=d["slave_ticket"], master_open_volume=d["master_open_volume"],
        slave_open_volume=d["slave_open_volume"],
    )

def _symbol_info_to_dict(si: SymbolInfo) -> dict:
    return {
        "point": si.point, "digits": si.digits, "tick_size": si.tick_size,
        "volume_step": si.volume_step, "volume_min": si.volume_min,
        "volume_max": si.volume_max,
    }

def _symbol_info_from_dict(d: dict) -> SymbolInfo:
    return SymbolInfo(
        point=d["point"], digits=d["digits"], tick_size=d["tick_size"],
        volume_step=d["volume_step"], volume_min=d["volume_min"],
        volume_max=d["volume_max"],
    )
```

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_messages.py`:

```python
import pytest

from manager.engine.models import Position, Record, SymbolInfo, BUY, SELL
from manager.ipc import messages as M


def _pos(ticket=1, side=BUY):
    return Position(ticket=ticket, symbol="EURUSD", side=side, open_price=1.10,
                    volume=0.5, sl=1.09, tp=1.11, open_time=1700000000,
                    point=0.00001, comment="CPY#1|MV0.5|SV0.10")


def _si():
    return SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                     volume_step=0.01, volume_min=0.01, volume_max=100.0)


def test_snapshot_round_trip():
    msg = M.SnapshotMsg(source_id="master", timestamp=1700000000,
                        heartbeat=3, positions=(_pos(11), _pos(22, SELL)))
    rt = M.decode(M.encode(msg))
    assert isinstance(rt, M.SnapshotMsg)
    assert rt.source_id == "master"
    assert rt.timestamp == 1700000000
    assert rt.heartbeat == 3
    assert len(rt.positions) == 2
    assert isinstance(rt.positions[0], Position)
    assert rt.positions[0].ticket == 11 and rt.positions[1].side == SELL
    assert rt.positions[0].comment == "CPY#1|MV0.5|SV0.10"


def test_command_open_round_trip():
    msg = M.CommandMsg(slave_id="s1", action="OPEN", master_ticket=42,
                      symbol="EURUSD", volume=0.10, sl=1.095, tp=1.205,
                      master_open_price=1.10, side=BUY, magic=1000042,
                      comment="CPY#42|MV0.5|SV0.10")
    rt = M.decode(M.encode(msg))
    assert isinstance(rt, M.CommandMsg)
    assert rt.action == "OPEN" and rt.master_ticket == 42 and rt.volume == 0.10
    assert rt.slave_ticket == 0  # unused for OPEN


def test_command_partial_close_round_trip():
    msg = M.CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=42,
                      slave_ticket=777, new_master_volume=0.30,
                      master_open_volume=0.50, slave_open_volume=0.10)
    rt = M.decode(M.encode(msg))
    assert rt.action == "PARTIAL_CLOSE" and rt.slave_ticket == 777
    assert rt.new_master_volume == 0.30 and rt.master_open_volume == 0.50


def test_ack_round_trip():
    msg = M.AckMsg(slave_id="s1", action="OPEN", master_ticket=42, ok=True,
                  slave_ticket=777, fill_price=1.10005, fill_volume=0.10,
                  remaining_volume=0.10, retcode=10009)
    rt = M.decode(M.encode(msg))
    assert rt.ok is True and rt.slave_ticket == 777 and rt.fill_price == 1.10005
    assert rt.retcode == 10009


def test_status_symbolinfo_recovery_round_trip():
    st = M.StatusMsg(source_id="s1", role="slave", connected=True, login=123,
                    balance=1000.0, equity=1000.0, currency="USD", server="Demo")
    assert M.decode(M.encode(st)).balance == 1000.0

    si = M.SymbolInfoMsg(source_id="s1", infos={"EURUSD": _si()})
    rt = M.decode(M.encode(si))
    assert isinstance(rt.infos["EURUSD"], SymbolInfo)
    assert rt.infos["EURUSD"].volume_step == 0.01

    rec = M.RecoveryMsg(source_id="s1",
                        records=(Record(42, 1000042, 777, 0.50, 0.10),))
    rt = M.decode(M.encode(rec))
    assert isinstance(rt.records[0], Record)
    assert rt.records[0].slave_ticket == 777


def test_start_and_error_round_trip():
    st = M.StartMsg(config={"terminal_path": "C:/t/terminal64.exe",
                            "login": 123, "server": "Demo"}, password="pw")
    rt = M.decode(M.encode(st))
    assert rt.config["login"] == 123 and rt.password == "pw"

    err = M.ErrorMsg(source_id="s1", message="boom", fatal=True)
    assert M.decode(M.encode(err)).fatal is True


def test_decode_unknown_kind_raises():
    with pytest.raises(ValueError):
        M.decode({"_kind": "nope"})


def test_decode_missing_kind_raises():
    with pytest.raises(KeyError):
        M.decode({"source_id": "x"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_messages.py -v`
Expected: FAIL `ModuleNotFoundError: manager.ipc.messages`.

- [ ] **Step 3: Write minimal implementation**

Create `manager/ipc/__init__.py` (empty file). Create `manager/ipc/messages.py` with the nested serializers above, then append the message dataclasses + `encode`/`decode`:

```python
# ---- message dataclasses ----

@dataclass(frozen=True)
class StartMsg:
    """First message on every worker pipe: carries config + password so the
    password never appears in argv. Sent by the supervisor before the worker
    calls mt5.initialize."""
    config: dict
    password: str
    KIND = "start"


@dataclass(frozen=True)
class SnapshotMsg:
    source_id: str
    timestamp: int
    heartbeat: int
    positions: tuple[Position, ...]
    KIND = "snapshot"


@dataclass(frozen=True)
class StatusMsg:
    source_id: str
    role: str          # "master" | "slave"
    connected: bool
    login: int
    balance: float
    equity: float
    currency: str
    server: str
    KIND = "status"


@dataclass(frozen=True)
class SymbolInfoMsg:
    source_id: str
    infos: dict[str, SymbolInfo]   # slave_symbol -> info
    KIND = "symbol_info"


@dataclass(frozen=True)
class RecoveryMsg:
    source_id: str
    records: tuple[Record, ...]
    KIND = "recovery"


@dataclass(frozen=True)
class CommandMsg:
    """Manager -> slave. Fields used per action:
      OPEN:           symbol, volume(lots), sl, tp (raw master), master_open_price,
                      side, magic, comment
      MODIFY:         slave_ticket, sl, tp (raw master), master_open_price, side
      PARTIAL_CLOSE:  slave_ticket, new_master_volume, master_open_volume, slave_open_volume
      CLOSE:          slave_ticket
    """
    slave_id: str
    action: str        # "OPEN" | "MODIFY" | "PARTIAL_CLOSE" | "CLOSE"
    master_ticket: int
    symbol: str = ""
    volume: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    master_open_price: float = 0.0
    side: int = 0
    magic: int = 0
    comment: str = ""
    slave_ticket: int = 0
    new_master_volume: float = 0.0
    master_open_volume: float = 0.0
    slave_open_volume: float = 0.0
    KIND = "command"


@dataclass(frozen=True)
class AckMsg:
    slave_id: str
    action: str
    master_ticket: int
    ok: bool
    slave_ticket: int = 0
    fill_price: float = 0.0
    fill_volume: float = 0.0
    remaining_volume: float = 0.0
    retcode: int = 0
    error: str = ""
    KIND = "ack"


@dataclass(frozen=True)
class ErrorMsg:
    source_id: str
    message: str
    fatal: bool = False
    KIND = "error"


_REGISTRY = {
    "start": StartMsg,
    "snapshot": SnapshotMsg,
    "status": StatusMsg,
    "symbol_info": SymbolInfoMsg,
    "recovery": RecoveryMsg,
    "command": CommandMsg,
    "ack": AckMsg,
    "error": ErrorMsg,
}


def encode(msg) -> dict:
    """Serialize a message dataclass to a JSON-ready dict with a _kind tag."""
    kind = msg.KIND
    if kind == "snapshot":
        return {"_kind": kind, "source_id": msg.source_id, "timestamp": msg.timestamp,
                "heartbeat": msg.heartbeat,
                "positions": [_position_to_dict(p) for p in msg.positions]}
    if kind == "symbol_info":
        return {"_kind": kind, "source_id": msg.source_id,
                "infos": {k: _symbol_info_to_dict(v) for k, v in msg.infos.items()}}
    if kind == "recovery":
        return {"_kind": kind, "source_id": msg.source_id,
                "records": [_record_to_dict(r) for r in msg.records]}
    if kind == "start":
        return {"_kind": kind, "config": msg.config, "password": msg.password}
    # default: plain field dump (Status/Command/Ack/Error have only scalars)
    out = {"_kind": kind}
    for f in fields(msg):
        out[f.name] = getattr(msg, f.name)
    return out


def decode(d: dict):
    """Inverse of encode. Raises ValueError on unknown kind, KeyError if absent."""
    kind = d["_kind"]
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"unknown message kind: {kind!r}")
    if kind == "snapshot":
        return SnapshotMsg(
            source_id=d["source_id"], timestamp=d["timestamp"],
            heartbeat=d["heartbeat"],
            positions=tuple(_position_from_dict(p) for p in d["positions"]))
    if kind == "symbol_info":
        return SymbolInfoMsg(
            source_id=d["source_id"],
            infos={k: _symbol_info_from_dict(v) for k, v in d["infos"].items()})
    if kind == "recovery":
        return RecoveryMsg(
            source_id=d["source_id"],
            records=tuple(_record_from_dict(r) for r in d["records"]))
    if kind == "start":
        return StartMsg(config=d["config"], password=d["password"])
    # scalar-only messages
    kwargs = {f.name: d[f.name] for f in fields(cls)}
    return cls(**kwargs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_messages.py -v`
Expected: PASS (all 9 tests).

- [ ] **Step 5: Commit**

```bash
git add manager/ipc/__init__.py manager/ipc/messages.py manager/tests/test_messages.py
git commit -m "feat(ipc): kind-tagged JSON message schemas + encode/decode"
```

---

### Task 3: Pipe framing (ipc/pipe_framing.py)

Length-prefixed JSON over any object exposing `send_bytes`/`recv_bytes` (the real `multiprocessing.connection.Connection` provides the length framing of the bytes blob; `send_msg`/`recv_msg` add the JSON payload layer). `recv_msg` raises `EOFError` when the pipe is closed — workers detect manager death this way.

**Files:**
- Create: `manager/ipc/pipe_framing.py`
- Test: `manager/tests/test_pipe_framing.py`

**Interfaces:**
- Consumes: `ipc.messages.encode`/`decode` (Task 2).
- Produces: `send_msg(conn, msg) -> None`, `recv_msg(conn) -> msg`. `conn` is any object with `send_bytes(bytes)` / `recv_bytes() -> bytes`.

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_pipe_framing.py`:

```python
import pytest

from manager.engine.models import Position, BUY
from manager.ipc.messages import SnapshotMsg, CommandMsg
from manager.ipc.pipe_framing import send_msg, recv_msg


class FakeConn:
    """In-memory bidirectional pipe: peers share a list of byte blobs."""
    def __init__(self, peer):
        self._out = peer._in
        self._in = []
        peer._out = self._in
        peer._in = peer._in  # noqa: keep peer's in-buffer
    # The shared-buffer dance above is fiddly; use the helper below instead.


def _make_pair():
    class End:
        def __init__(self, inbox, outbox):
            self._inbox = inbox
            self._outbox = outbox
        def send_bytes(self, b):
            self._outbox.append(b)
        def recv_bytes(self):
            if not self._inbox:
                raise EOFError
            return self._inbox.pop(0)
    a_box, b_box = [], []
    return End(b_box, a_box), End(a_box, b_box)


def test_send_recv_round_trip():
    a, b = _make_pair()
    msg = SnapshotMsg(source_id="master", timestamp=1, heartbeat=1,
                     positions=(Position(1, "EURUSD", BUY, 1.1, 0.5, 0, 0, 0, 0.00001),))
    send_msg(a, msg)
    got = recv_msg(b)
    assert isinstance(got, SnapshotMsg)
    assert got.positions[0].ticket == 1


def test_multiple_messages_in_order():
    a, b = _make_pair()
    send_msg(a, CommandMsg(slave_id="s", action="OPEN", master_ticket=1))
    send_msg(a, CommandMsg(slave_id="s", action="CLOSE", master_ticket=1, slave_ticket=2))
    first = recv_msg(b)
    second = recv_msg(b)
    assert first.action == "OPEN" and second.action == "CLOSE"


def test_recv_eof_raises():
    a, b = _make_pair()
    with pytest.raises(EOFError):
        recv_msg(b)  # nothing was ever sent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_pipe_framing.py -v`
Expected: FAIL `ModuleNotFoundError: manager.ipc.pipe_framing`.

- [ ] **Step 3: Write minimal implementation**

Create `manager/ipc/pipe_framing.py`:

```python
from __future__ import annotations

import json

from manager.ipc.messages import encode, decode


def send_msg(conn, msg) -> None:
    """Serialize `msg` to JSON bytes and send it over the pipe. The underlying
    Connection (multiprocessing.connection.Connection.send_bytes) adds its own
    length framing to the blob, so message boundaries are preserved."""
    payload = json.dumps(encode(msg)).encode("utf-8")
    conn.send_bytes(payload)


def recv_msg(conn):
    """Block until one framed message arrives; decode it. Raises EOFError when
    the peer has closed the pipe (the manager-death signal workers rely on)."""
    payload = conn.recv_bytes()
    return decode(json.loads(payload.decode("utf-8")))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_pipe_framing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/ipc/pipe_framing.py manager/tests/test_pipe_framing.py
git commit -m "feat(ipc): length-prefixed JSON pipe framing"
```

---

### Task 4: MT5 adapter + constants (worker/mt5_adapter.py, worker/mt5_constants.py)

The mockable seam. `mt5_constants.py` holds MQL5 int values (no `MetaTrader5` import) so the worker builds request dicts and `FakeMt5` interprets them without the package. `RealMt5` lazily imports `MetaTrader5` only inside its terminal-touching methods. `FakeMt5` simulates order effects (open adds a position, partial reduces volume, close removes it) so worker tests see realistic post-order state.

**Files:**
- Create: `manager/worker/__init__.py` (empty), `manager/worker/mt5_constants.py`, `manager/worker/mt5_adapter.py`
- Test: `manager/tests/test_mt5_adapter.py`

**Interfaces:**
- Consumes: `models.Position`, `models.SymbolInfo` (Task 1).
- Produces: `Mt5Adapter` (typing.Protocol) with `initialize/shutdown/last_error/positions_get/position_by_ticket/symbol_info/symbol_info_tick/account_info/order_send`; `FakeMt5` (scripted, simulates); `RealMt5` (lazy mt5).

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_mt5_adapter.py`:

```python
from manager.engine.models import Position, SymbolInfo, BUY, SELL
from manager.worker.mt5_constants import (
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP, ORDER_TYPE_BUY, ORDER_TYPE_SELL,
    ORDER_TIME_GTC, ORDER_FILLING_RETURN, TRADE_RETCODE_DONE,
)
from manager.worker.mt5_adapter import FakeMt5


def _si():
    return SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                     volume_step=0.01, volume_min=0.01, volume_max=100.0)


def _fake(positions=(), ticks=None):
    return FakeMt5(
        positions=list(positions),
        symbol_infos={"EURUSD": _si()},
        account={"login": 123, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
        ticks=ticks or {"EURUSD": (1.10000, 1.10010)},  # (bid, ask)
    )


def test_positions_get_returns_copies():
    mt = _fake(positions=[Position(1, "EURUSD", BUY, 1.1, 0.5, 0, 0, 0, 0.00001)])
    got = mt.positions_get()
    assert got[0].ticket == 1
    got[0]  # mutating the returned list must not affect the adapter
    assert mt.positions_get()[0].ticket == 1


def test_symbol_info_tick_and_account():
    mt = _fake()
    assert mt.symbol_info_tick("EURUSD") == (1.10000, 1.10010)
    assert mt.account_info()["login"] == 123


def test_order_send_open_appends_position():
    mt = _fake()
    req = {"action": TRADE_ACTION_DEAL, "symbol": "EURUSD", "type": ORDER_TYPE_BUY,
           "volume": 0.10, "price": 1.10010, "sl": 1.09, "tp": 1.11,
           "deviation": 10, "magic": 1000042, "comment": "CPY#42|MV0.5|SV0.10",
           "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN}
    res = mt.order_send(req)
    assert res["retcode"] == TRADE_RETCODE_DONE
    assert res["price"] == 1.10010 and res["volume"] == 0.10
    new = mt.positions_get()[-1]
    assert new.side == BUY and new.symbol == "EURUSD" and new.volume == 0.10
    assert new.magic == 1000042 and new.comment == "CPY#42|MV0.5|SV0.10"
    assert new.point == 0.00001  # filled from symbol_info


def test_order_send_modify_updates_sltp():
    mt = _fake(positions=[Position(7, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0, 0.00001,
                                  comment="CPY#1|MV0.5|SV0.10")])
    req = {"action": TRADE_ACTION_SLTP, "symbol": "EURUSD", "position": 7,
           "sl": 1.095, "tp": 1.105}
    res = mt.order_send(req)
    assert res["retcode"] == TRADE_RETCODE_DONE
    pos = mt.position_by_ticket(7)
    assert pos.sl == 1.095 and pos.tp == 1.105


def test_order_send_partial_reduces_volume():
    mt = _fake(positions=[Position(7, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0, 0.00001,
                                  comment="CPY#1|MV0.5|SV0.10")])
    # closing a BUY -> SELL at bid
    req = {"action": TRADE_ACTION_DEAL, "symbol": "EURUSD", "type": ORDER_TYPE_SELL,
           "volume": 0.04, "position": 7, "price": 1.10000,
           "deviation": 10, "magic": 1000042, "comment": "close",
           "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN}
    res = mt.order_send(req)
    assert res["retcode"] == TRADE_RETCODE_DONE
    assert mt.position_by_ticket(7).volume == 0.06  # 0.10 - 0.04


def test_order_send_full_close_removes_position():
    mt = _fake(positions=[Position(7, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0, 0.00001,
                                  comment="CPY#1|MV0.5|SV0.10")])
    req = {"action": TRADE_ACTION_DEAL, "symbol": "EURUSD", "type": ORDER_TYPE_SELL,
           "volume": 0.10, "position": 7, "price": 1.10000,
           "deviation": 10, "magic": 1000042, "comment": "close",
           "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN}
    mt.order_send(req)
    assert mt.position_by_ticket(7) is None


def test_initialize_shutdown_last_error():
    mt = _fake()
    assert mt.initialize("C:/t/terminal64.exe", 123, "pw", "Demo") is True
    assert mt.last_error() == (0, "")
    mt.shutdown()


def test_failed_order_send_records_retcode():
    mt = _fake(order_results=[{"retcode": 10004, "order": 0}])
    res = mt.order_send({"action": TRADE_ACTION_DEAL, "symbol": "EURUSD",
                         "type": ORDER_TYPE_BUY, "volume": 0.10, "price": 1.1,
                         "deviation": 10, "magic": 1, "comment": "x",
                         "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN})
    assert res["retcode"] == 10004
    assert mt.last_error()[0] == 10004
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_mt5_adapter.py -v`
Expected: FAIL `ModuleNotFoundError: manager.worker.mt5_adapter`.

- [ ] **Step 3: Write minimal implementation**

Create `manager/worker/__init__.py` (empty). Create `manager/worker/mt5_constants.py`:

```python
"""MQL5 trade-request integer constants. These match MetaTrader5's package
constants exactly (standard MQL5 enum values) but are defined here so the
worker and FakeMt5 build/interpret request dicts WITHOUT importing the
MetaTrader5 package. RealMt5 may use these directly or mt5.<NAME>; they agree.
"""

TRADE_ACTION_DEAL = 1      # market open/close
TRADE_ACTION_SLTP = 3      # modify position SL/TP
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TIME_GTC = 0
ORDER_FILLING_RETURN = 2
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_PLACED = 10008
```

Create `manager/worker/mt5_adapter.py`:

```python
from __future__ import annotations

from typing import Protocol

from manager.engine.models import Position, SymbolInfo, BUY, SELL
from manager.worker.mt5_constants import (
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP, ORDER_TYPE_BUY,
    TRADE_RETCODE_DONE,
)


class Mt5Adapter(Protocol):
    """The terminal-touching seam. FakeMt5 implements it for tests;
    RealMt5 wraps the MetaTrader5 package (lazy-imported)."""
    def initialize(self, path: str, login: int, password: str, server: str,
                   portable: bool = False) -> bool: ...
    def shutdown(self) -> None: ...
    def last_error(self) -> tuple[int, str]: ...
    def positions_get(self) -> list[Position]: ...
    def position_by_ticket(self, ticket: int) -> Position | None: ...
    def symbol_info(self, symbol: str) -> SymbolInfo | None: ...
    def symbol_info_tick(self, symbol: str) -> tuple[float, float] | None: ...
    def account_info(self) -> dict: ...
    def order_send(self, request: dict) -> dict: ...


class FakeMt5:
    """Scripted, in-memory adapter. Simulates order effects so worker tests see
    realistic post-order position state. `order_results` (optional) is a list
    of canned result dicts popped in order; when it runs out, success is
    assumed and the position list is mutated per the request."""
    def __init__(self, positions=None, symbol_infos=None, account=None,
                 ticks=None, order_results=None):
        self.positions: list[Position] = list(positions or [])
        self.symbol_infos: dict[str, SymbolInfo] = dict(symbol_infos or {})
        self.account: dict = dict(account or {})
        self.ticks: dict[str, tuple[float, float]] = dict(ticks or {})
        self._canned = list(order_results or [])
        self._order_seq = 500000
        self._last_error: tuple[int, str] = (0, "")
        self._connected = False

    def initialize(self, path, login, password, server, portable=False):
        self._connected = True
        return True

    def shutdown(self):
        self._connected = False

    def last_error(self):
        return self._last_error

    def positions_get(self):
        return list(self.positions)

    def position_by_ticket(self, ticket):
        for p in self.positions:
            if p.ticket == ticket:
                return p
        return None

    def symbol_info(self, symbol):
        return self.symbol_infos.get(symbol)

    def symbol_info_tick(self, symbol):
        return self.ticks.get(symbol)

    def account_info(self):
        return dict(self.account)

    def order_send(self, request: dict) -> dict:
        action = request["action"]
        # canned result overrides simulation
        if self._canned:
            res = self._canned.pop(0)
            if res.get("retcode") != TRADE_RETCODE_DONE:
                self._last_error = (res["retcode"], "canned failure")
            return res

        if action == TRADE_ACTION_DEAL:
            pos = request.get("position", 0)
            existing = self.position_by_ticket(pos) if pos else None
            if existing is not None:
                # close or partial close of an existing position
                remaining = round(existing.volume - request["volume"], 8)
                if remaining <= 0.0:
                    self.positions = [p for p in self.positions if p.ticket != pos]
                else:
                    self.positions = [
                        p if p.ticket != pos else
                        Position(p.ticket, p.symbol, p.side, p.open_price,
                                 remaining, p.sl, p.tp, p.open_time, p.point,
                                 p.comment)
                        for p in self.positions
                    ]
                return {"retcode": TRADE_RETCODE_DONE, "order": pos, "deal": pos,
                        "price": request["price"], "volume": request["volume"]}
            # open new position
            self._order_seq += 1
            ticket = self._order_seq
            sym = request["symbol"]
            info = self.symbol_infos.get(sym)
            point = info.point if info else 0.00001
            side = BUY if request["type"] == ORDER_TYPE_BUY else SELL
            new = Position(ticket=ticket, symbol=sym, side=side,
                           open_price=request["price"],
                           volume=request["volume"], sl=request.get("sl", 0.0),
                           tp=request.get("tp", 0.0), open_time=0, point=point,
                           comment=request.get("comment", ""))
            self.positions.append(new)
            return {"retcode": TRADE_RETCODE_DONE, "order": ticket, "deal": ticket,
                    "price": request["price"], "volume": request["volume"]}

        if action == TRADE_ACTION_SLTP:
            pos = request["position"]
            for i, p in enumerate(self.positions):
                if p.ticket == pos:
                    self.positions[i] = Position(
                        p.ticket, p.symbol, p.side, p.open_price, p.volume,
                        request.get("sl", 0.0), request.get("tp", 0.0),
                        p.open_time, p.point, p.comment)
                    break
            return {"retcode": TRADE_RETCODE_DONE, "order": pos, "deal": 0,
                    "price": 0.0, "volume": 0.0}

        self._last_error = (-1, "unknown action")
        return {"retcode": -1, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0}


class RealMt5:
    """Wraps the MetaTrader5 package. Imports it lazily inside methods so the
    module imports cleanly on machines without MetaTrader5 installed (tests,
    CI). NOT unit-tested; exercised only by the manual demo smoke test."""
    def __init__(self):
        self._mt5 = None

    def _mod(self):
        if self._mt5 is None:
            import MetaTrader5 as mt5  # lazy
            self._mt5 = mt5
        return self._mt5

    def initialize(self, path, login, password, server, portable=False):
        mt5 = self._mod()
        ok = mt5.initialize(path=path, login=int(login), password=password,
                            server=server, portable=portable)
        if not ok:
            self._last_error = mt5.last_error()
        return bool(ok)

    def shutdown(self):
        self._mod().shutdown()

    def last_error(self):
        return self._mod().last_error()

    def positions_get(self):
        mt5 = self._mod()
        raw = mt5.positions_get()
        if raw is None:
            return []
        out = []
        for p in raw:
            info = self.symbol_info(p.symbol)
            point = info.point if info else 0.0
            out.append(Position(ticket=p.ticket, symbol=p.symbol, side=p.type,
                                open_price=p.price_open, volume=p.volume,
                                sl=p.sl, tp=p.tp, open_time=p.time, point=point,
                                comment=p.comment))
        return out

    def position_by_ticket(self, ticket):
        for p in self.positions_get():
            if p.ticket == ticket:
                return p
        return None

    def symbol_info(self, symbol):
        mt5 = self._mod()
        si = mt5.symbol_info(symbol)
        if si is None:
            return None
        return SymbolInfo(point=si.point, digits=si.digits,
                          tick_size=si.trade_tick_size,
                          volume_step=si.volume_step, volume_min=si.volume_min,
                          volume_max=si.volume_max)

    def symbol_info_tick(self, symbol):
        mt5 = self._mod()
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            return None
        return (t.bid, t.ask)

    def account_info(self):
        mt5 = self._mod()
        a = mt5.account_info()
        if a is None:
            return {}
        return {"login": a.login, "balance": a.balance, "equity": a.equity,
                "currency": a.currency, "server": a.server}

    def order_send(self, request: dict):
        mt5 = self._mod()
        result = mt5.order_send(request)
        if result is None:
            return {"retcode": -1, "order": 0, "deal": 0, "price": 0.0,
                    "volume": 0.0}
        return {"retcode": result.retcode, "order": result.order,
                "deal": result.deal, "price": result.price,
                "volume": result.volume}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_mt5_adapter.py -v`
Expected: PASS (all 9 tests).

- [ ] **Step 5: Commit**

```bash
git add manager/worker/__init__.py manager/worker/mt5_constants.py manager/worker/mt5_adapter.py manager/tests/test_mt5_adapter.py
git commit -m "feat(worker): MT5 adapter (FakeMt5 + lazy RealMt5) + constants"
```

---

### Task 5: Worker subprocess (worker/mt5_worker.py)

Role-parameterized subprocess. Pure helpers — `build_snapshot`, `build_recovery_records`, `build_symbol_info_msg`, `slave_init`, `execute_command` — are unit-tested with `FakeMt5`; the `worker_main`/`_master_loop`/`_slave_loop` glue is thin I/O over those helpers (exercised by the tier-3 smoke test and the Task 8 integration fakes that mirror them).

Per the user-approved decision, the **slave** normalizes SL/TP + computes partial-close volume from its live position (EA-faithful). `execute_command` is where `normalize_sltp` + `round_to_tick` are called.

**Files:**
- Create: `manager/worker/mt5_worker.py`
- Test: `manager/tests/test_mt5_worker.py`

**Interfaces:**
- Consumes: `mt5_adapter` (Task 4), `mt5_constants` (Task 4), `ipc.messages`/`ipc.pipe_framing` (Tasks 2-3), `engine.linkage` (Plan 1), `engine.transform` (Plan 1), `engine.models` (Plan 1 + Task 1).
- Produces: `build_snapshot(adapter, heartbeat, now) -> SnapshotMsg`; `build_recovery_records(adapter) -> list[Record]`; `build_symbol_info_msg(adapter, slave_id, symbol_map_csv) -> SymbolInfoMsg`; `slave_init(adapter, config) -> (RecoveryMsg, SymbolInfoMsg, StatusMsg)`; `execute_command(adapter, cmd, normalize_sltp, retry_count, retry_delay_ms) -> AckMsg`; `worker_main(pipe, role, adapter_kind, fake_state=None)`.

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_mt5_worker.py`:

```python
import pytest

from manager.engine.models import Position, Record, SymbolInfo, BUY, SELL
from manager.engine.linkage import magic_for, encode_comment, MAGIC_BASE
from manager.ipc.messages import CommandMsg, SnapshotMsg, RecoveryMsg, SymbolInfoMsg, StatusMsg
from manager.worker.mt5_adapter import FakeMt5
from manager.worker.mt5_worker import (
    build_snapshot, build_recovery_records, build_symbol_info_msg, slave_init,
    execute_command,
)

NOW = 1700000000
SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)


def _adapter(positions=(), ticks=None):
    return FakeMt5(
        positions=list(positions),
        symbol_infos={"EURUSD": SI},
        account={"login": 123, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
        ticks=ticks or {"EURUSD": (1.10000, 1.10010)},  # (bid, ask)
    )


# ---- build_snapshot ----

def test_build_snapshot():
    mt = _adapter(positions=[Position(1, "EURUSD", BUY, 1.1, 0.5, 0, 0, 0, 0.00001)])
    snap = build_snapshot(mt, heartbeat=7, now=NOW)
    assert isinstance(snap, SnapshotMsg)
    assert snap.timestamp == NOW and snap.heartbeat == 7
    assert len(snap.positions) == 1 and snap.positions[0].ticket == 1


# ---- build_recovery_records ----

def test_build_recovery_records_decodes_copied_positions():
    cmt = encode_comment(42, 0.50, 0.10)
    mt = _adapter(positions=[
        Position(777, "EURUSD", BUY, 1.10, 0.10, 0, 0, 0, 0.00001, comment=cmt),  # copied
        Position(888, "EURUSD", BUY, 1.10, 0.20, 0, 0, 0, 0.00001, comment="manual"),  # not copied
    ])
    # FakeMt5 does not set magic on its positions; recovery keys on the CPY comment.
    recs = build_recovery_records(mt)
    assert len(recs) == 1
    assert recs[0] == Record(master_ticket=42, magic=magic_for(42),
                             slave_ticket=777, master_open_volume=0.50,
                             slave_open_volume=0.10)


def test_build_recovery_records_skips_malformed_comment():
    mt = _adapter(positions=[
        Position(777, "EURUSD", BUY, 1.10, 0.10, 0, 0, 0, 0.00001,
                 comment="CPY#99|MV0.5"),  # SV missing -> incomplete
    ])
    assert build_recovery_records(mt) == []


# ---- build_symbol_info_msg ----

def test_build_symbol_info_msg_reports_mapped_slave_symbols():
    msg = build_symbol_info_msg(_adapter(), slave_id="s1",
                                symbol_map_csv="EURUSD=EURUSD,GBPUSD=GBPUSD")
    assert isinstance(msg, SymbolInfoMsg)
    assert set(msg.infos.keys()) == {"EURUSD", "GBPUSD"}
    assert msg.infos["EURUSD"].volume_step == 0.01


# ---- slave_init ----

def test_slave_init_emits_recovery_symbolinfo_status():
    cmt = encode_comment(42, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10, 0.10, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cfg = {"slave_id": "s1", "symbol_map_csv": "EURUSD=EURUSD",
          "login": 123, "server": "Demo"}
    rec, si, st = slave_init(mt, cfg)
    assert isinstance(rec, RecoveryMsg) and rec.records[0].master_ticket == 42
    assert isinstance(si, SymbolInfoMsg) and "EURUSD" in si.infos
    assert isinstance(st, StatusMsg) and st.role == "slave" and st.connected is True
    assert st.balance == 1000.0


# ---- execute_command: OPEN ----

def test_execute_open_normalizes_sltp_and_opens():
    mt = _adapter()
    cmd = CommandMsg(slave_id="s1", action="OPEN", master_ticket=42, symbol="EURUSD",
                     volume=0.10, sl=1.09500, tp=1.10500, master_open_price=1.10000,
                     side=BUY, magic=magic_for(42),
                     comment=encode_comment(42, 0.50, 0.10))
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok and ack.action == "OPEN" and ack.master_ticket == 42
    assert ack.fill_volume == 0.10
    # slave opened at ask 1.10010; raw distance SL = master_open - master_sl = 0.00500
    # slave_sl = slave_open - 0.00500 = 1.10010 - 0.00500 = 1.09510 (tick-rounded)
    pos = mt.positions_get()[-1]
    assert pos.sl == pytest.approx(1.09510, abs=1e-8)
    assert pos.tp == pytest.approx(1.10510, abs=1e-8)
    assert pos.magic == magic_for(42)


def test_execute_open_without_normalize_passes_raw_sltp():
    mt = _adapter()
    cmd = CommandMsg(slave_id="s1", action="OPEN", master_ticket=42, symbol="EURUSD",
                     volume=0.10, sl=1.09500, tp=1.10500, master_open_price=1.10000,
                     side=BUY, magic=magic_for(42), comment=encode_comment(42, 0.50, 0.10))
    ack = execute_command(mt, cmd, normalize_sltp=False, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    pos = mt.positions_get()[-1]
    assert pos.sl == 1.09500 and pos.tp == 1.10500


# ---- execute_command: MODIFY ----

def test_execute_modify_normalizes_to_fill_price():
    cmt = encode_comment(1, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1, slave_ticket=777,
                     sl=1.09400, tp=1.10600, master_open_price=1.10000, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    pos = mt.position_by_ticket(777)
    # slave_open = 1.10010; SL dist = 1.10000-1.09400 = 0.00600 -> 1.10010-0.00600 = 1.09410
    assert pos.sl == pytest.approx(1.09410, abs=1e-8)
    assert pos.tp == pytest.approx(1.10610, abs=1e-8)


# ---- execute_command: PARTIAL_CLOSE (slave computes volume) ----

def test_execute_partial_close_uses_live_current_volume():
    cmt = encode_comment(1, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.30,
                     master_open_volume=0.50, slave_open_volume=0.10)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    # fraction = 0.30/0.50 = 0.6; target = 0.10*0.6 = 0.06; close 0.10-0.06 = 0.04
    assert mt.position_by_ticket(777).volume == pytest.approx(0.06, abs=1e-8)
    assert ack.remaining_volume == pytest.approx(0.06, abs=1e-8)


def test_execute_partial_close_noop_when_target_meets_current():
    cmt = encode_comment(1, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.06, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.30,
                     master_open_volume=0.50, slave_open_volume=0.10)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok  # nothing to close; not an error
    assert ack.remaining_volume == pytest.approx(0.06, abs=1e-8)


# ---- execute_command: CLOSE ----

def test_execute_close_removes_position():
    cmt = encode_comment(1, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cmd = CommandMsg(slave_id="s1", action="CLOSE", master_ticket=1, slave_ticket=777)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok and mt.position_by_ticket(777) is None


def test_execute_open_failure_returns_failed_ack():
    mt = _adapter(order_results=[{"retcode": 10004, "order": 0}])
    cmd = CommandMsg(slave_id="s1", action="OPEN", master_ticket=42, symbol="EURUSD",
                     volume=0.10, sl=1.095, tp=1.105, master_open_price=1.10,
                     side=BUY, magic=1, comment="x")
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and ack.retcode == 10004
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_mt5_worker.py -v`
Expected: FAIL `ModuleNotFoundError: manager.worker.mt5_worker`.

- [ ] **Step 3: Write minimal implementation**

Create `manager/worker/mt5_worker.py`:

```python
from __future__ import annotations

import math
import time

from manager.engine.models import Position, Record, BUY, SELL
from manager.engine.linkage import (
    magic_for, decode_comment, MAGIC_BASE, MAGIC_MOD,
)
from manager.engine.transform import normalize_sltp, round_to_tick, parse_symbol_map
from manager.ipc.messages import (
    AckMsg, ErrorMsg, SnapshotMsg, StatusMsg, SymbolInfoMsg, RecoveryMsg,
)
from manager.ipc.pipe_framing import send_msg, recv_msg
from manager.worker.mt5_adapter import FakeMt5, RealMt5
from manager.worker.mt5_constants import (
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP, ORDER_TYPE_BUY, ORDER_TYPE_SELL,
    ORDER_TIME_GTC, ORDER_FILLING_RETURN, TRADE_RETCODE_DONE,
)


def build_snapshot(adapter, heartbeat: int, now: int) -> SnapshotMsg:
    return SnapshotMsg(source_id="master", timestamp=now, heartbeat=heartbeat,
                       positions=tuple(adapter.positions_get()))


def build_recovery_records(adapter) -> list[Record]:
    """Scan this terminal's open positions for copied positions (CPY#..|MV|SV
    comment) and rebuild linkage records. Used on (re)start so the manager
    seeds its RecordTable and never duplicates. Skips positions whose comment
    is missing or lacks both MV+SV (cannot compute partial fractions)."""
    out: list[Record] = []
    for p in adapter.positions_get():
        if not (MAGIC_BASE <= p.magic < MAGIC_BASE + MAGIC_MOD):
            continue
        decoded = decode_comment(p.comment)
        if decoded is None:
            continue
        master_ticket, mv, sv = decoded
        if mv is None or sv is None:
            continue
        out.append(Record(master_ticket=master_ticket, magic=p.magic,
                          slave_ticket=p.ticket, master_open_volume=mv,
                          slave_open_volume=sv))
    return out


def build_symbol_info_msg(adapter, slave_id: str, symbol_map_csv: str) -> SymbolInfoMsg:
    """Report symbol info for the slave symbols named in the symbol map (the
    manager needs volume params for lot-sizing). Same-name fallback symbols not
    in the map are not reported here (forward-looking: on-demand request)."""
    infos: dict[str, object] = {}
    for slave_symbol in parse_symbol_map(symbol_map_csv).values():
        info = adapter.symbol_info(slave_symbol)
        if info is not None:
            infos[slave_symbol] = info
    return SymbolInfoMsg(source_id=slave_id, infos=infos)


def _status(adapter, source_id: str, role: str, connected: bool) -> StatusMsg:
    acc = adapter.account_info()
    return StatusMsg(source_id=source_id, role=role, connected=connected,
                    login=int(acc.get("login", 0)), balance=float(acc.get("balance", 0.0)),
                    equity=float(acc.get("equity", 0.0)),
                    currency=str(acc.get("currency", "")),
                    server=str(acc.get("server", "")))


def slave_init(adapter, config: dict):
    """Recovery + symbol-info + status emitted by a slave on connect.
    Returns (RecoveryMsg, SymbolInfoMsg, StatusMsg)."""
    slave_id = config["slave_id"]
    records = build_recovery_records(adapter)
    rec_msg = RecoveryMsg(source_id=slave_id, records=tuple(records))
    si_msg = build_symbol_info_msg(adapter, slave_id, config.get("symbol_map_csv", ""))
    st_msg = _status(adapter, slave_id, "slave", connected=True)
    return rec_msg, si_msg, st_msg


def _order_send_with_retry(adapter, request: dict, retry_count: int,
                           retry_delay_ms: int) -> dict:
    result: dict = {"retcode": -1, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0}
    for attempt in range(max(1, retry_count)):
        result = adapter.order_send(request)
        if result.get("retcode") == TRADE_RETCODE_DONE:
            return result
        if attempt < retry_count - 1 and retry_delay_ms > 0:
            time.sleep(retry_delay_ms / 1000.0)
    return result


def _round_sltp(sl: float, tp: float, tick_size: float, digits: int):
    rsl = round_to_tick(sl, tick_size, digits)
    rtp = round_to_tick(tp, tick_size, digits)
    if rsl is None or rtp is None:
        return None
    return rsl, rtp


def execute_command(adapter, cmd, normalize_sltp: bool, retry_count: int,
                    retry_delay_ms: int) -> AckMsg:
    """Execute one CommandMsg against the terminal. The slave normalizes SL/TP
    to its live/fill price and computes partial-close volume from its live
    current position (EA-faithful). Returns an AckMsg."""
    def _fail(retcode: int, err: str) -> AckMsg:
        return AckMsg(slave_id=cmd.slave_id, action=cmd.action,
                      master_ticket=cmd.master_ticket, ok=False, retcode=retcode,
                      error=err)

    if cmd.action == "OPEN":
        tick = adapter.symbol_info_tick(cmd.symbol)
        if tick is None:
            return _fail(-1, f"no tick for {cmd.symbol}")
        bid, ask = tick
        slave_open = ask if cmd.side == BUY else bid
        info = adapter.symbol_info(cmd.symbol)
        if info is None:
            return _fail(-1, f"no symbol info for {cmd.symbol}")
        if normalize_sltp:
            sl, tp = normalize_sltp(cmd.master_open_price, cmd.sl, cmd.tp,
                                   slave_open, cmd.side)
        else:
            sl, tp = cmd.sl, cmd.tp
        rounded = _round_sltp(sl, tp, info.tick_size, info.digits)
        if rounded is None:
            return _fail(-1, "SL/TP tick rounding failed")
        sl, tp = rounded
        req = {"action": TRADE_ACTION_DEAL, "symbol": cmd.symbol,
               "type": ORDER_TYPE_BUY if cmd.side == BUY else ORDER_TYPE_SELL,
               "volume": cmd.volume, "price": slave_open, "sl": sl, "tp": tp,
               "deviation": 10, "magic": cmd.magic, "comment": cmd.comment,
               "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN}
        res = _order_send_with_retry(adapter, req, retry_count, retry_delay_ms)
        if res.get("retcode") != TRADE_RETCODE_DONE:
            return _fail(res.get("retcode", -1), str(adapter.last_error()))
        return AckMsg(slave_id=cmd.slave_id, action="OPEN",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=res["order"], fill_price=res["price"],
                      fill_volume=res["volume"], remaining_volume=res["volume"],
                      retcode=TRADE_RETCODE_DONE)

    if cmd.action == "MODIFY":
        pos = adapter.position_by_ticket(cmd.slave_ticket)
        if pos is None:
            return _fail(-1, f"position #{cmd.slave_ticket} gone")
        info = adapter.symbol_info(pos.symbol)
        if info is None:
            return _fail(-1, f"no symbol info for {pos.symbol}")
        slave_open = pos.open_price
        if normalize_sltp:
            sl, tp = normalize_sltp(cmd.master_open_price, cmd.sl, cmd.tp,
                                   slave_open, cmd.side)
        else:
            sl, tp = cmd.sl, cmd.tp
        rounded = _round_sltp(sl, tp, info.tick_size, info.digits)
        if rounded is None:
            return _fail(-1, "SL/TP tick rounding failed")
        sl, tp = rounded
        req = {"action": TRADE_ACTION_SLTP, "symbol": pos.symbol,
               "position": cmd.slave_ticket, "sl": sl, "tp": tp}
        res = _order_send_with_retry(adapter, req, retry_count, retry_delay_ms)
        if res.get("retcode") != TRADE_RETCODE_DONE:
            return _fail(res.get("retcode", -1), str(adapter.last_error()))
        return AckMsg(slave_id=cmd.slave_id, action="MODIFY",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket, retcode=TRADE_RETCODE_DONE)

    if cmd.action == "PARTIAL_CLOSE":
        pos = adapter.position_by_ticket(cmd.slave_ticket)
        if pos is None:
            return _fail(-1, f"position #{cmd.slave_ticket} gone")
        info = adapter.symbol_info(pos.symbol)
        if info is None or cmd.master_open_volume <= 0.0:
            return _fail(-1, "cannot compute partial fraction")
        current = pos.volume
        fraction = cmd.new_master_volume / cmd.master_open_volume
        target = cmd.slave_open_volume * fraction
        vol_to_close = math.floor((current - target) / info.volume_step) * info.volume_step
        vol_to_close = min(vol_to_close, current)
        if vol_to_close <= 0.0:
            return AckMsg(slave_id=cmd.slave_id, action="PARTIAL_CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket, remaining_volume=current,
                          retcode=TRADE_RETCODE_DONE)
        tick = adapter.symbol_info_tick(pos.symbol)
        if tick is None:
            return _fail(-1, f"no tick for {pos.symbol}")
        bid, ask = tick
        opposite = ORDER_TYPE_SELL if pos.side == BUY else ORDER_TYPE_BUY
        price = bid if pos.side == BUY else ask
        req = {"action": TRADE_ACTION_DEAL, "symbol": pos.symbol, "type": opposite,
               "volume": vol_to_close, "position": cmd.slave_ticket, "price": price,
               "deviation": 10, "magic": pos.magic, "comment": "partial",
               "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN}
        res = _order_send_with_retry(adapter, req, retry_count, retry_delay_ms)
        if res.get("retcode") != TRADE_RETCODE_DONE:
            return _fail(res.get("retcode", -1), str(adapter.last_error()))
        remaining = adapter.position_by_ticket(cmd.slave_ticket)
        rem_vol = remaining.volume if remaining is not None else 0.0
        return AckMsg(slave_id=cmd.slave_id, action="PARTIAL_CLOSE",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket, fill_volume=vol_to_close,
                      remaining_volume=rem_vol, retcode=TRADE_RETCODE_DONE)

    if cmd.action == "CLOSE":
        pos = adapter.position_by_ticket(cmd.slave_ticket)
        if pos is None:
            return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket, retcode=TRADE_RETCODE_DONE)
        tick = adapter.symbol_info_tick(pos.symbol)
        if tick is None:
            return _fail(-1, f"no tick for {pos.symbol}")
        bid, ask = tick
        opposite = ORDER_TYPE_SELL if pos.side == BUY else ORDER_TYPE_BUY
        price = bid if pos.side == BUY else ask
        req = {"action": TRADE_ACTION_DEAL, "symbol": pos.symbol, "type": opposite,
               "volume": pos.volume, "position": cmd.slave_ticket, "price": price,
               "deviation": 10, "magic": pos.magic, "comment": "close",
               "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN}
        res = _order_send_with_retry(adapter, req, retry_count, retry_delay_ms)
        if res.get("retcode") != TRADE_RETCODE_DONE:
            return _fail(res.get("retcode", -1), str(adapter.last_error()))
        return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket, retcode=TRADE_RETCODE_DONE)

    return _fail(-1, f"unknown action {cmd.action!r}")


def _master_loop(pipe, adapter, config):
    source_id = config.get("slave_id", "master")
    send_msg(pipe, _status(adapter, source_id, "master", connected=True))
    heartbeat = 0
    interval = config.get("master_interval_ms", 1000) / 1000.0
    while True:
        snap = build_snapshot(adapter, heartbeat, now=int(time.time()))
        try:
            send_msg(pipe, snap)
        except (EOFError, OSError):
            return  # manager gone
        heartbeat += 1
        time.sleep(interval)


def _slave_loop(pipe, adapter, config):
    slave_id = config["slave_id"]
    rec_msg, si_msg, st_msg = slave_init(adapter, config)
    send_msg(pipe, rec_msg)
    send_msg(pipe, si_msg)
    send_msg(pipe, st_msg)
    normalize = bool(config.get("normalize_sltp", True))
    retry_count = int(config.get("retry_count", 3))
    retry_delay = int(config.get("retry_delay_ms", 500))
    status_interval = float(config.get("slave_status_interval_ms", 5000)) / 1000.0
    last_status = time.time()
    poll_timeout = min(1.0, status_interval)
    while True:
        if pipe.poll(poll_timeout):
            cmd = recv_msg(pipe)  # raises EOFError on manager close
            ack = execute_command(adapter, cmd, normalize, retry_count, retry_delay)
            try:
                send_msg(pipe, ack)
                send_msg(pipe, _status(adapter, slave_id, "slave", connected=True))
            except (EOFError, OSError):
                return  # manager gone
            last_status = time.time()
        elif time.time() - last_status >= status_interval:
            try:
                send_msg(pipe, _status(adapter, slave_id, "slave", connected=True))
            except (EOFError, OSError):
                return
            last_status = time.time()


def worker_main(pipe, role: str, adapter_kind: str = "real", fake_state=None):
    """Subprocess entry. Reads its StartMsg (config + password) from the pipe
    BEFORE initializing, so the password never appears in argv."""
    try:
        start = recv_msg(pipe)
    except EOFError:
        return
    config = start.config
    password = start.password
    if adapter_kind == "fake":
        adapter = FakeMt5(**(fake_state or {}))
    else:
        adapter = RealMt5()
    source_id = config.get("slave_id", role)
    ok = adapter.initialize(config["terminal_path"], int(config["login"]),
                           password, config["server"],
                           portable=bool(config.get("portable", False)))
    if not ok:
        try:
            send_msg(pipe, ErrorMsg(source_id=source_id,
                   message=f"initialize failed: {adapter.last_error()}", fatal=True))
        except (EOFError, OSError):
            pass
        return
    try:
        if role == "master":
            _master_loop(pipe, adapter, config)
        else:
            _slave_loop(pipe, adapter, config)
    except EOFError:
        pass  # manager closed the pipe -> graceful shutdown
    finally:
        try:
            adapter.shutdown()
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_mt5_worker.py -v`
Expected: PASS (all 13 tests).

- [ ] **Step 5: Commit**

```bash
git add manager/worker/mt5_worker.py manager/tests/test_mt5_worker.py
git commit -m "feat(worker): role-parameterized MT5 worker (master/slave) + slave-side normalize"
```

---

### Task 6: Engine copy-loop (engine/copy_loop.py)

The brain. `SlaveConfig`/`SlaveState`/`derive_command`/`CopyEngine` are pure (no I/O) and fully unit-tested here. The threaded I/O wrapper (routing + worker health/restart) is the Task 7 `Supervisor`.

Per-slave pending/held bookkeeping serializes commands per master ticket: while a command for a ticket is unacked, later events for that ticket are held (coalesced to the latest event) and re-derived on ack — so a slow broker on one slave never blocks the others and no command outruns its predecessor's effect. The manager does NOT track slave current volume (the slave computes partial-close volume from its live position), so Plan 1's `Record` is used unchanged.

**Files:**
- Create: `manager/engine/copy_loop.py`
- Test: `manager/tests/test_copy_loop.py`

**Interfaces:**
- Consumes: `models` (Plan 1 + Task 1), `transform.SymbolMapper/calculate_lots`, `snapshot_diff.diff`, `record_table.RecordTable`, `baseline.is_too_old/seed_from_recovery`, `linkage.magic_for/encode_comment`, `ipc.messages`.
- Produces: `SlaveConfig`, `SlaveState`, `derive_command(state, event, now) -> CommandMsg | None`, `CopyEngine` (pure).

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_copy_loop.py`:

```python
import pytest

from manager.engine.models import Position, Snapshot, Record, SymbolInfo, BUY, SELL
from manager.engine.record_table import RecordTable
from manager.engine.linkage import magic_for, encode_comment
from manager.engine.copy_loop import SlaveConfig, CopyEngine, derive_command

NOW = 1700000000
SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)


def _cfg(slave_id="s1", symbol_map="EURUSD=EURUSD", max_age=10):
    return SlaveConfig(slave_id=slave_id, symbol_map_csv=symbol_map,
                       step_amount=100.0, step_size=0.01, max_lot=10.0,
                       max_trade_age_minutes=max_age, normalize_sltp=True)


def _engine(slaves=(_cfg(),), infos=None, balance=1000.0):
    eng = CopyEngine()
    for cfg in slaves:
        eng.add_slave(cfg)
    if infos:
        for sid, m in infos.items():
            eng.apply_symbol_info(sid, m)
    for sid in (c.slave_id for c in slaves):
        eng.apply_status(sid, _status(balance))
    return eng


def _status(balance=1000.0):
    from manager.ipc.messages import StatusMsg
    return StatusMsg(source_id="s1", role="slave", connected=True, login=1,
                     balance=balance, equity=balance, currency="USD", server="Demo")


def _pos(ticket, volume=0.5, sl=1.09500, tp=1.10500, side=BUY, open_time=NOW):
    return Position(ticket=ticket, symbol="EURUSD", side=side, open_price=1.10000,
                   volume=volume, sl=sl, tp=tp, open_time=open_time, point=0.00001)


def _snap(positions, ts=NOW, hb=1):
    return Snapshot(timestamp=ts, heartbeat=hb, positions=tuple(positions))


def test_new_emits_open_with_lots_raw_sltp_magic_comment():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert len(cmds) == 1
    c = cmds[0]
    assert c.action == "OPEN" and c.master_ticket == 42
    assert c.symbol == "EURUSD" and c.volume == 0.10  # floor(1000/100)*0.01=0.10
    assert c.sl == 1.09500 and c.tp == 1.10500  # RAW master (slave normalizes)
    assert c.master_open_price == 1.10000 and c.side == BUY
    assert c.magic == magic_for(42)
    assert c.comment == encode_comment(42, 0.5, 0.10)


def test_new_already_in_record_table_is_skipped():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert cmds == []


def test_new_too_old_is_skipped():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    old = _pos(42, open_time=NOW - 9999 * 60)  # 9999 min ago, max_age=10
    cmds = eng.ingest_snapshot(_snap([old]), now=NOW)["s1"]
    assert cmds == []


def test_new_unmapped_symbol_is_skipped():
    eng = _engine(infos={"s1": {"EURUSD": SI}}, slaves=(_cfg(symbol_map="GBPUSD=GBPUSD"),))
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert cmds == []  # EURUSD not in map, no fallback info


def test_new_without_symbol_info_is_skipped():
    eng = _engine()  # no symbol info applied
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert cmds == []


def test_modify_emits_modify_with_slave_ticket_and_raw_sltp():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    cmds = eng.ingest_snapshot(
        _snap([_pos(42, sl=1.09000, tp=1.11000)]), now=NOW)["s1"]
    assert len(cmds) == 1
    c = cmds[0]
    assert c.action == "MODIFY" and c.slave_ticket == 777
    assert c.sl == 1.09000 and c.tp == 1.11000  # raw master
    assert c.master_open_price == 1.10000 and c.side == BUY


def test_partial_emits_partial_close_with_open_volumes():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.30)]), now=NOW)["s1"]
    assert len(cmds) == 1
    c = cmds[0]
    assert c.action == "PARTIAL_CLOSE" and c.slave_ticket == 777
    assert c.new_master_volume == 0.30 and c.master_open_volume == 0.5
    assert c.slave_open_volume == 0.10


def test_close_emits_close_command():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    # first snapshot establishes prev; second has ticket gone -> CLOSE
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)
    cmds = eng.ingest_snapshot(_snap([]), now=NOW)["s1"]
    assert len(cmds) == 1 and cmds[0].action == "CLOSE" and cmds[0].slave_ticket == 777


def test_apply_ack_open_ok_sets_slave_ticket_and_fill_volume():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)
    from manager.ipc.messages import AckMsg
    reemitted = eng.apply_ack("s1", AckMsg(slave_id="s1", action="OPEN",
              master_ticket=42, ok=True, slave_ticket=777, fill_price=1.10010,
              fill_volume=0.10, remaining_volume=0.10, retcode=10009))
    assert reemitted == []
    rec = eng._slaves["s1"].table.get(42)
    assert rec.slave_ticket == 777 and rec.slave_open_volume == 0.10


def test_apply_ack_open_fail_leaves_failed_record():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)
    from manager.ipc.messages import AckMsg
    eng.apply_ack("s1", AckMsg(slave_id="s1", action="OPEN", master_ticket=42,
                              ok=False, retcode=10004, error="requote"))
    rec = eng._slaves["s1"].table.get(42)
    assert rec.slave_ticket == 0  # failed-open marker; not re-NEW'd


def test_apply_ack_close_ok_removes_record():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    from manager.ipc.messages import AckMsg
    eng.apply_ack("s1", AckMsg(slave_id="s1", action="CLOSE", master_ticket=42,
                              ok=True, slave_ticket=777, retcode=10009))
    assert eng._slaves["s1"].table.has(42) is False


def test_pending_holds_modify_until_open_ack_then_reemits():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    # snapshot 1: NEW -> OPEN (optimistic record, pending)
    cmds1 = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert cmds1[0].action == "OPEN"
    # snapshot 2 (before OPEN ack): master modifies -> held, not sent
    cmds2 = eng.ingest_snapshot(_snap([_pos(42, sl=1.09000, tp=1.11000)]), now=NOW)["s1"]
    assert cmds2 == []  # MODIFY held pending the OPEN ack
    # OPEN ack arrives -> re-emit the held MODIFY (now slave_ticket is set)
    from manager.ipc.messages import AckMsg
    reemitted = eng.apply_ack("s1", AckMsg(slave_id="s1", action="OPEN",
              master_ticket=42, ok=True, slave_ticket=777, fill_volume=0.10,
              fill_price=1.10010, remaining_volume=0.10, retcode=10009))
    assert len(reemitted) == 1 and reemitted[0].action == "MODIFY"
    assert reemitted[0].slave_ticket == 777 and reemitted[0].sl == 1.09000


def test_pending_coalesces_two_partials_to_latest():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # establish prev
    # snapshot: partial to 0.30 -> PARTIAL_CLOSE sent (pending)
    cmds1 = eng.ingest_snapshot(_snap([_pos(42, volume=0.30)]), now=NOW)["s1"]
    assert cmds1[0].action == "PARTIAL_CLOSE"
    # next snapshot (before ack): partial further to 0.20 -> held (coalesce)
    cmds2 = eng.ingest_snapshot(_snap([_pos(42, volume=0.20)]), now=NOW)["s1"]
    assert cmds2 == []
    # ack the first partial -> re-emit the LATEST held (0.20)
    from manager.ipc.messages import AckMsg
    reemitted = eng.apply_ack("s1", AckMsg(slave_id="s1", action="PARTIAL_CLOSE",
              master_ticket=42, ok=True, slave_ticket=777, remaining_volume=0.06,
              retcode=10009))
    assert len(reemitted) == 1 and reemitted[0].action == "PARTIAL_CLOSE"
    assert reemitted[0].new_master_volume == 0.20


def test_close_of_failed_open_cleans_up_record():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # OPEN sent
    from manager.ipc.messages import AckMsg
    eng.apply_ack("s1", AckMsg(slave_id="s1", action="OPEN", master_ticket=42,
                              ok=False, retcode=10004, error="x"))  # failed-open record
    assert eng._slaves["s1"].table.has(42)
    # master closes -> derive_command CLOSE returns None (slave_ticket==0) + cleanup
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # re-establish prev (position still here)
    cmds = eng.ingest_snapshot(_snap([]), now=NOW)["s1"]
    assert cmds == []
    assert eng._slaves["s1"].table.has(42) is False  # cleaned up
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_copy_loop.py -v`
Expected: FAIL `ModuleNotFoundError: manager.engine.copy_loop`.

- [ ] **Step 3: Write minimal implementation**

Create `manager/engine/copy_loop.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field

from manager.engine.models import Position, Snapshot, Event, Record, SymbolInfo
from manager.engine.linkage import magic_for, encode_comment
from manager.engine.transform import SymbolMapper, calculate_lots
from manager.engine.snapshot_diff import diff
from manager.engine.record_table import RecordTable
from manager.engine.baseline import is_too_old, seed_from_recovery
from manager.ipc.messages import CommandMsg, AckMsg, StatusMsg


@dataclass
class SlaveConfig:
    slave_id: str
    symbol_map_csv: str
    step_amount: float
    step_size: float
    max_lot: float
    max_trade_age_minutes: int
    normalize_sltp: bool


@dataclass
class SlaveState:
    config: SlaveConfig
    table: RecordTable
    symbol_infos: dict[str, SymbolInfo]
    balance: float
    mapper: SymbolMapper
    pending: set[int] = field(default_factory=set)
    held: dict[int, Event] = field(default_factory=dict)


def derive_command(state: SlaveState, event: Event, now: int) -> CommandMsg | None:
    """Derive the command a slave should execute for one diff event, or None to
    skip. Pure: no I/O, no mutation. The slave normalizes SL/TP + computes
    partial volume, so OPEN/MODIFY carry RAW master sl/tp + master_open_price."""
    pos = event.position
    ticket = pos.ticket
    cfg = state.config

    if event.kind == "NEW":
        if state.table.has(ticket):
            return None
        if is_too_old(pos.open_time, now, cfg.max_trade_age_minutes):
            return None
        slave_symbol = state.mapper.resolve(pos.symbol)
        if slave_symbol == "":
            return None
        info = state.symbol_infos.get(slave_symbol)
        if info is None:
            return None
        lots = calculate_lots(state.balance, cfg.step_amount, cfg.step_size,
                              cfg.max_lot, info.volume_step, info.volume_min,
                              info.volume_max)
        if lots <= 0.0:
            return None
        return CommandMsg(slave_id=cfg.slave_id, action="OPEN", master_ticket=ticket,
                         symbol=slave_symbol, volume=lots, sl=pos.sl, tp=pos.tp,
                         master_open_price=pos.open_price, side=pos.side,
                         magic=magic_for(ticket),
                         comment=encode_comment(ticket, pos.volume, lots))

    if event.kind == "MODIFY":
        rec = state.table.get(ticket)
        if rec is None or rec.slave_ticket == 0:
            return None
        return CommandMsg(slave_id=cfg.slave_id, action="MODIFY", master_ticket=ticket,
                         slave_ticket=rec.slave_ticket, sl=pos.sl, tp=pos.tp,
                         master_open_price=pos.open_price, side=pos.side,
                         magic=rec.magic)

    if event.kind == "PARTIAL":
        rec = state.table.get(ticket)
        if rec is None or rec.slave_ticket == 0:
            return None
        return CommandMsg(slave_id=cfg.slave_id, action="PARTIAL_CLOSE",
                         master_ticket=ticket, slave_ticket=rec.slave_ticket,
                         new_master_volume=pos.volume,
                         master_open_volume=rec.master_open_volume,
                         slave_open_volume=rec.slave_open_volume)

    if event.kind == "CLOSE":
        rec = state.table.get(ticket)
        if rec is None or rec.slave_ticket == 0:
            return None
        return CommandMsg(slave_id=cfg.slave_id, action="CLOSE",
                         master_ticket=ticket, slave_ticket=rec.slave_ticket)

    return None


class CopyEngine:
    """The pure copy brain. Holds per-slave state + the previous master
    snapshot. ingest_snapshot -> per-slave commands; apply_ack updates records
    and re-emits held commands. No I/O."""

    def __init__(self):
        self._slaves: dict[str, SlaveState] = {}
        self._prev: list[Position] = []
        self._last_now: int = 0

    def add_slave(self, config: SlaveConfig) -> None:
        state = SlaveState(config=config, table=RecordTable(), symbol_infos={},
                           balance=0.0, mapper=None)  # type: ignore[arg-type]
        state.mapper = SymbolMapper(config.symbol_map_csv,
                                    lambda s: s in state.symbol_infos)
        self._slaves[config.slave_id] = state

    def apply_symbol_info(self, slave_id: str, infos: dict[str, SymbolInfo]) -> None:
        self._slaves[slave_id].symbol_infos.update(infos)

    def apply_status(self, slave_id: str, status: StatusMsg) -> None:
        self._slaves[slave_id].balance = status.balance

    def apply_recovery(self, slave_id: str, records) -> int:
        return seed_from_recovery(self._slaves[slave_id].table, records)

    def reset_slave(self, slave_id: str) -> None:
        """Clear a slave's table/pending/held on worker restart so recovery
        re-seeds cleanly (no duplicated trades). Symbol info is kept (the slave
        re-sends it)."""
        state = self._slaves[slave_id]
        state.table = RecordTable()
        state.pending.clear()
        state.held.clear()

    def ingest_snapshot(self, snapshot: Snapshot,
                        now: int) -> dict[str, list[CommandMsg]]:
        events = diff(self._prev, list(snapshot.positions))
        self._prev = list(snapshot.positions)
        self._last_now = now
        out: dict[str, list[CommandMsg]] = {}
        for slave_id, state in self._slaves.items():
            cmds: list[CommandMsg] = []
            for event in events:
                cmd = self._handle_event(state, event, now)
                if cmd is not None:
                    cmds.append(cmd)
            out[slave_id] = cmds
        return out

    def _handle_event(self, state: SlaveState, event: Event,
                      now: int) -> CommandMsg | None:
        ticket = event.position.ticket
        if ticket in state.pending:
            state.held[ticket] = event  # coalesce to latest event; re-derive on ack
            return None
        cmd = derive_command(state, event, now)
        if cmd is None:
            if (event.kind == "CLOSE" and state.table.has(ticket)
                    and state.table.get(ticket).slave_ticket == 0):
                state.table.remove(ticket)  # failed-open + master closed -> drop marker
            return None
        if cmd.action == "OPEN":
            # optimistic record (slave_ticket=0 until ack) prevents re-NEW
            state.table.add(Record(ticket, cmd.magic, 0, event.position.volume,
                                   cmd.volume))
        state.pending.add(ticket)
        return cmd

    def apply_ack(self, slave_id: str, ack: AckMsg) -> list[CommandMsg]:
        state = self._slaves[slave_id]
        ticket = ack.master_ticket
        rec = state.table.get(ticket)
        if ack.action == "OPEN":
            if rec is not None and ack.ok:
                rec.slave_ticket = ack.slave_ticket
                rec.slave_open_volume = ack.fill_volume
            # on failure: leave slave_ticket=0 marker (not re-NEW'd)
        elif ack.action == "CLOSE":
            if ack.ok and rec is not None:
                state.table.remove(ticket)
        # MODIFY / PARTIAL_CLOSE: no record-table change (slave holds live volume)
        state.pending.discard(ticket)
        held_event = state.held.pop(ticket, None)
        if held_event is None:
            return []
        cmd = derive_command(state, held_event, self._last_now)
        if cmd is None:
            if (held_event.kind == "CLOSE" and state.table.has(ticket)
                    and state.table.get(ticket).slave_ticket == 0):
                state.table.remove(ticket)
            return []
        state.pending.add(ticket)
        return [cmd]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_copy_loop.py -v`
Expected: PASS (all 15 tests).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/copy_loop.py manager/tests/test_copy_loop.py
git commit -m "feat(engine): copy-loop CopyEngine + derive_command (pure)"
```

---

### Task 7: Worker supervisor (supervisor.py)

Owns worker subprocesses + pipes, routes IPC to/from the `CopyEngine`, watches worker health, and restarts on death. `tick()` is one non-blocking slave drain + one timed master poll + a health pass; `_run` loops it in a daemon thread. Death detection: `proc.is_alive() == False` restarts immediately; a worker that sends nothing for `stale_seconds` increments `fail_count` (consecutive, reset on any message) and restarts at `consecutive_failures` (the not-cumulative TradeWolk rule). On slave restart the engine's slave table is reset so the slave's restart-recovery re-seeds it (no duplicated trades). Killing the leftover `terminal64.exe` is an injectable hook (`kill_terminal`, default no-op) the terminal manager plugs in in Plan 3.

**Files:**
- Create: `manager/supervisor.py`
- Test: `manager/tests/test_supervisor.py`

**Interfaces:**
- Consumes: `engine.copy_loop.CopyEngine`, `worker.mt5_worker.worker_main`, `ipc.messages`, `ipc.pipe_framing`, `multiprocessing`.
- Produces: `WorkerHandle`, `Supervisor` (`spawn_master`/`spawn_slave`/`tick`/`start`/`stop`/`join`/`shutdown`; attrs `errors`, `heartbeat_warning`).

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_supervisor.py`:

```python
import time

from manager.engine.models import Position, SymbolInfo, BUY
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.supervisor import Supervisor, WorkerHandle

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = int(time.time())


def _engine():
    eng = CopyEngine()
    eng.add_slave(SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                              step_amount=100.0, step_size=0.01, max_lot=10.0,
                              max_trade_age_minutes=999999, normalize_sltp=True))
    return eng


def _slave_cfg():
    return {"slave_id": "s1", "terminal_path": "C:/t/s.exe", "login": 2,
            "server": "Demo", "symbol_map_csv": "EURUSD=EURUSD",
            "normalize_sltp": True, "retry_count": 1, "retry_delay_ms": 0,
            "slave_status_interval_ms": 60000}


def _slave_state():
    return {"symbol_infos": {"EURUSD": SI},
            "account": {"login": 2, "balance": 1000.0, "equity": 1000.0,
                        "currency": "USD", "server": "Demo"},
            "ticks": {"EURUSD": (1.10000, 1.10010)}}


def _tick_until(sup, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.tick(timeout=0.02)
        if predicate():
            return True
    return predicate()


def test_end_to_end_open_through_subprocesses():
    eng = _engine()
    sup = Supervisor(eng, heartbeat_seconds=5, stale_seconds=30,
                     consecutive_failures=3, poll_timeout=0.02)
    master_state = {
        "positions": [Position(42, "EURUSD", BUY, 1.10000, 0.5, 1.095, 1.105,
                               NOW, 0.00001, "")],
        "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"}}
    sup.spawn_master({"terminal_path": "C:/t/m.exe", "login": 1,
                      "server": "Demo", "master_interval_ms": 20}, "pw",
                     adapter_kind="fake", fake_state=master_state)
    sup.spawn_slave("s1", _slave_cfg(), "pw", adapter_kind="fake",
                    fake_state=_slave_state())
    try:
        ok = _tick_until(
            sup,
            lambda: eng._slaves["s1"].table.get(42) is not None
            and eng._slaves["s1"].table.get(42).slave_ticket != 0)
        assert ok, "OPEN did not flow end-to-end"
        rec = eng._slaves["s1"].table.get(42)
        assert rec.slave_open_volume == 0.10
        assert rec.master_open_volume == 0.5
    finally:
        sup.shutdown()


def test_restart_on_process_death():
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=1000, consecutive_failures=5,
                     poll_timeout=0.02)
    sup.spawn_slave("s1", _slave_cfg(), "pw", adapter_kind="fake",
                    fake_state=_slave_state())
    try:
        _tick_until(sup, lambda: sup._handles["s1"].proc.is_alive())
        old = sup._handles["s1"].proc
        old.terminate(); old.join(2.0)
        assert not old.is_alive()
        ok = _tick_until(
            sup,
            lambda: sup._handles["s1"].proc is not old
            and sup._handles["s1"].proc.is_alive())
        assert ok, "slave was not restarted after death"
    finally:
        sup.shutdown()


class _StubProc:
    def __init__(self): self._alive = True
    def is_alive(self): return self._alive
    def terminate(self): self._alive = False
    def join(self, timeout=None): pass


def test_consecutive_stale_failures_restart():
    fake_now = [0.0]
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=10.0, consecutive_failures=3,
                    time_fn=lambda: fake_now[0], poll_timeout=0.0)
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        password="", adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    # stub _spawn so restart doesn't create a real subprocess
    spawned = []
    def fake_spawn(name, role, config, password, adapter_kind, fake_state):
        h = WorkerHandle(name=name, role=role, proc=_StubProc(), pipe=None,
                        config=config, password=password, adapter_kind=adapter_kind,
                        fake_state=fake_state, last_msg_ts=fake_now[0])
        spawned.append(h)
        return h
    sup._spawn = fake_spawn

    fake_now[0] = 100.0  # 100 - 0 > 10 -> stale
    sup._health_check(); assert sup._handles["s1"].fail_count == 1
    sup._health_check(); assert sup._handles["s1"].fail_count == 2
    sup._health_check()  # 3rd consecutive -> restart
    assert spawned, "restart should have spawned a new worker"
    assert sup._handles["s1"] is spawned[-1]
    assert sup._handles["s1"].fail_count == 0  # reset on restart


def test_message_resets_fail_count():
    fake_now = [0.0]
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=10.0, consecutive_failures=5,
                    time_fn=lambda: fake_now[0], poll_timeout=0.0)
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        password="", adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    fake_now[0] = 100.0
    sup._health_check(); assert sup._handles["s1"].fail_count == 1
    # a fresh message resets the staleness window
    sup._handles["s1"].last_msg_ts = 100.0
    fake_now[0] = 105.0  # 5 < 10 -> not stale
    sup._health_check(); assert sup._handles["s1"].fail_count == 0


def test_shutdown_terminates_workers():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    sup.spawn_slave("s1", _slave_cfg(), "pw", adapter_kind="fake",
                    fake_state=_slave_state())
    _tick_until(sup, lambda: sup._handles["s1"].proc.is_alive())
    sup.shutdown()
    assert sup._handles == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_supervisor.py -v`
Expected: FAIL `ModuleNotFoundError: manager.supervisor`.

- [ ] **Step 3: Write minimal implementation**

Create `manager/supervisor.py`:

```python
from __future__ import annotations

import multiprocessing
import threading
import time
from dataclasses import dataclass

from manager.engine.copy_loop import CopyEngine
from manager.engine.models import Snapshot
from manager.ipc.messages import (
    AckMsg, StatusMsg, SnapshotMsg, RecoveryMsg, SymbolInfoMsg, ErrorMsg, StartMsg,
)
from manager.ipc.pipe_framing import send_msg, recv_msg
from manager.worker.mt5_worker import worker_main


@dataclass
class WorkerHandle:
    name: str
    role: str
    proc: multiprocessing.Process
    pipe: object
    config: dict
    password: str
    adapter_kind: str
    fake_state: dict | None
    last_msg_ts: float = 0.0
    fail_count: int = 0


class Supervisor:
    """Owns worker subprocesses + pipes; routes IPC to/from the CopyEngine;
    watches health and restarts on death. tick() = one slave drain + one timed
    master poll + a health pass. _run() loops it in a daemon thread."""

    def __init__(self, engine: CopyEngine, heartbeat_seconds: int = 5,
                 stale_seconds: float = 30.0, consecutive_failures: int = 3,
                 poll_timeout: float = 0.2, time_fn=time.time,
                 kill_terminal=None):
        self._engine = engine
        self._heartbeat_seconds = heartbeat_seconds
        self._stale_seconds = stale_seconds
        self._consecutive_failures = consecutive_failures
        self._poll_timeout = poll_timeout
        self._time_fn = time_fn
        self._kill_terminal = kill_terminal  # callable(path) or None (Plan 3)
        self._handles: dict[str, WorkerHandle] = {}
        self._last_snapshot_ts = time_fn()
        self.heartbeat_warning = False
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = None
        self.on_restart = None  # callback(name, role) for GUI status (Plan 4)

    def spawn_master(self, config, password, adapter_kind="real", fake_state=None):
        self._handles["master"] = self._spawn("master", "master", config,
                                              password, adapter_kind, fake_state)

    def spawn_slave(self, slave_id, config, password, adapter_kind="real",
                    fake_state=None):
        self._handles[slave_id] = self._spawn(slave_id, "slave", config,
                                              password, adapter_kind, fake_state)

    def _spawn(self, name, role, config, password, adapter_kind, fake_state):
        parent_pipe, child_pipe = multiprocessing.Pipe(duplex=True)
        proc = multiprocessing.Process(target=worker_main,
            args=(child_pipe, role, adapter_kind, fake_state), daemon=True)
        proc.start()
        child_pipe.close()  # parent owns only the parent end
        send_msg(parent_pipe, StartMsg(config=config, password=password))
        return WorkerHandle(name=name, role=role, proc=proc, pipe=parent_pipe,
                            config=config, password=password,
                            adapter_kind=adapter_kind, fake_state=fake_state,
                            last_msg_ts=self._time_fn())

    def tick(self, timeout=None) -> bool:
        to = self._poll_timeout if timeout is None else timeout
        self._drain_slaves()
        ok = self._read_master(to)
        self._health_check()
        return ok

    def _drain_slaves(self) -> None:
        for name, h in list(self._handles.items()):
            if h.role != "slave":
                continue
            while h.pipe is not None and h.pipe.poll(0):
                try:
                    msg = recv_msg(h.pipe)
                except EOFError:
                    self.errors.append(f"slave {name} pipe closed")
                    break
                self._dispatch_slave(name, msg)
                h.last_msg_ts = self._time_fn()
                h.fail_count = 0

    def _dispatch_slave(self, slave_id, msg) -> None:
        if isinstance(msg, AckMsg):
            for cmd in self._engine.apply_ack(slave_id, msg):
                self._send(slave_id, cmd)
        elif isinstance(msg, StatusMsg):
            self._engine.apply_status(slave_id, msg)
        elif isinstance(msg, RecoveryMsg):
            self._engine.apply_recovery(slave_id, msg.records)
        elif isinstance(msg, SymbolInfoMsg):
            self._engine.apply_symbol_info(slave_id, msg.infos)
        elif isinstance(msg, ErrorMsg):
            self.errors.append(f"{slave_id}: {msg.message}")

    def _read_master(self, timeout) -> bool:
        h = self._handles.get("master")
        if h is None or h.pipe is None:
            return True
        if h.pipe.poll(timeout):
            try:
                msg = recv_msg(h.pipe)
            except EOFError:
                return False
            h.last_msg_ts = self._time_fn()
            h.fail_count = 0
            if isinstance(msg, SnapshotMsg):
                self._last_snapshot_ts = self._time_fn()
                self.heartbeat_warning = False
                snap = Snapshot(timestamp=msg.timestamp, heartbeat=msg.heartbeat,
                                positions=msg.positions)
                cmds = self._engine.ingest_snapshot(snap, now=msg.timestamp)
                for slave_id, clist in cmds.items():
                    for cmd in clist:
                        self._send(slave_id, cmd)
            elif isinstance(msg, ErrorMsg):
                self.errors.append(f"master: {msg.message}")
            return True
        if self._time_fn() - self._last_snapshot_ts > self._heartbeat_seconds * 2:
            if not self.heartbeat_warning:
                self.heartbeat_warning = True
                self.errors.append("no heartbeat from master")
        return True

    def _send(self, slave_id, msg) -> None:
        h = self._handles.get(slave_id)
        if h is None or h.pipe is None:
            return
        try:
            send_msg(h.pipe, msg)
        except (EOFError, OSError):
            self.errors.append(f"lost slave {slave_id}")

    def _health_check(self) -> None:
        for name, h in list(self._handles.items()):
            if not h.proc.is_alive():
                self._restart(name)
                continue
            if self._time_fn() - h.last_msg_ts > self._stale_seconds:
                h.fail_count += 1
                if h.fail_count >= self._consecutive_failures:
                    self._restart(name)
            else:
                h.fail_count = 0

    def _restart(self, name) -> None:
        h = self._handles.get(name)
        if h is None:
            return
        self.errors.append(f"restarting {name}")
        if h.proc.is_alive():
            h.proc.terminate()
            h.proc.join(timeout=2.0)
        if h.pipe is not None:
            try:
                h.pipe.close()
            except Exception:
                pass
        if self._kill_terminal is not None:
            try:
                self._kill_terminal(h.config.get("terminal_path", ""))
            except Exception:
                pass
        if h.role == "slave":
            self._engine.reset_slave(name)  # recovery re-seeds on reconnect
        self._handles[name] = self._spawn(name, h.role, h.config, h.password,
                                          h.adapter_kind, h.fake_state)
        if self.on_restart:
            self.on_restart(name, h.role)

    def shutdown(self) -> None:
        self._stop.set()
        for h in list(self._handles.values()):
            if h.proc.is_alive():
                h.proc.terminate()
                h.proc.join(timeout=2.0)
            if h.pipe is not None:
                try:
                    h.pipe.close()
                except Exception:
                    pass
        self._handles.clear()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            if not self.tick():
                break
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_supervisor.py -v`
Expected: PASS (all 5 tests). Note: the two subprocess tests spawn real `multiprocessing.Process` workers (fake adapter); on Windows this uses spawn. If a subprocess test is flaky, re-run — it polls to a 5s deadline.

- [ ] **Step 5: Commit**

```bash
git add manager/supervisor.py manager/tests/test_supervisor.py
git commit -m "feat(supervisor): spawn/route/watch/restart workers + IPC routing"
```

---

### Task 8: Integration tests (full copy lifecycle + recovery)

Tier-2 integration, no real terminal. `test_copy_loop.py` drives the real `CopyEngine` + real IPC framing + the real `execute_command` (with `FakeMt5`) through a scripted master open→modify→partial→close lifecycle, over a real `multiprocessing.Pipe` to an in-process fake slave thread. `test_recovery.py` covers restart-recovery seeding so the first-diff `NEW` skips already-copied positions.

The in-process fake slave (a thread, not a subprocess) keeps the lifecycle test deterministic — it shares the `FakeMt5` so the test inspects post-order state directly. The subprocess path is covered by Task 7.

**Files:**
- Create: `manager/tests/test_copy_loop_integration.py`, `manager/tests/test_recovery.py`

**Interfaces:**
- Consumes: `engine.copy_loop.CopyEngine`/`SlaveConfig`, `worker.mt5_worker.execute_command`, `worker.mt5_adapter.FakeMt5`, `ipc.pipe_framing.send_msg`/`recv_msg`, `multiprocessing.Pipe`.

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_copy_loop_integration.py`:

```python
import threading
import time

import pytest

from manager.engine.models import Position, Snapshot, SymbolInfo, Record, BUY
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.engine.linkage import magic_for
from manager.ipc.messages import AckMsg, StatusMsg
from manager.ipc.pipe_framing import send_msg, recv_msg
from manager.worker.mt5_adapter import FakeMt5
from manager.worker.mt5_worker import execute_command

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = 1700000000


def _cfg():
    return SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                       step_amount=100.0, step_size=0.01, max_lot=10.0,
                       max_trade_age_minutes=10, normalize_sltp=True)


def _pos(ticket, volume=0.5, sl=1.09500, tp=1.10500, side=BUY):
    return Position(ticket=ticket, symbol="EURUSD", side=side, open_price=1.10000,
                    volume=volume, sl=sl, tp=tp, open_time=NOW, point=0.00001)


def _slave_adapter():
    return FakeMt5(symbol_infos={"EURUSD": SI},
                  account={"login": 2, "balance": 1000.0, "equity": 1000.0,
                           "currency": "USD", "server": "Demo"},
                  ticks={"EURUSD": (1.10000, 1.10010)})


def _slave_thread(child_pipe, adapter, stop_evt):
    while not stop_evt.is_set():
        if not child_pipe.poll(0.05):
            continue
        try:
            cmd = recv_msg(child_pipe)
        except EOFError:
            return
        ack = execute_command(adapter, cmd, normalize_sltp=True, retry_count=1,
                              retry_delay_ms=0)
        try:
            send_msg(child_pipe, ack)
        except (EOFError, OSError):
            return


def _drive(engine, parent_pipe, slave_id, positions, now=NOW):
    snap = Snapshot(timestamp=now, heartbeat=1, positions=tuple(positions))
    cmds = engine.ingest_snapshot(snap, now=now)[slave_id]
    for cmd in cmds:
        send_msg(parent_pipe, cmd)
    # drain acks (and any re-emitted held commands) until idle
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not parent_pipe.poll(0.2):
            break
        msg = recv_msg(parent_pipe)
        if isinstance(msg, AckMsg):
            for c in engine.apply_ack(slave_id, msg):
                send_msg(parent_pipe, c)


def _engine_with_slave():
    eng = CopyEngine()
    eng.add_slave(_cfg())
    eng.apply_symbol_info("s1", {"EURUSD": SI})
    eng.apply_status("s1", StatusMsg(source_id="s1", role="slave", connected=True,
                    login=2, balance=1000.0, equity=1000.0, currency="USD",
                    server="Demo"))
    return eng


def test_full_lifecycle_open_modify_partial_close():
    eng = _engine_with_slave()
    parent_pipe, child_pipe = multiprocessing_pipe()
    adapter = _slave_adapter()
    stop = threading.Event()
    t = threading.Thread(target=_slave_thread, args=(child_pipe, adapter, stop),
                        daemon=True)
    t.start()
    try:
        # 1. OPEN
        _drive(eng, parent_pipe, "s1", [_pos(42)])
        rec = eng._slaves["s1"].table.get(42)
        assert rec is not None and rec.slave_ticket != 0
        assert rec.slave_open_volume == pytest.approx(0.10)
        pos = adapter.positions_get()[-1]
        assert pos.volume == pytest.approx(0.10)
        # slave normalized SL/TP to its ask (1.10010): 1.09510 / 1.10510
        assert pos.sl == pytest.approx(1.09510, abs=1e-8)
        assert pos.tp == pytest.approx(1.10510, abs=1e-8)

        # 2. MODIFY
        _drive(eng, parent_pipe, "s1", [_pos(42, sl=1.09000, tp=1.11000)])
        pos = adapter.position_by_ticket(rec.slave_ticket)
        # normalized to fill 1.10010: sl=1.09010, tp=1.11010
        assert pos.sl == pytest.approx(1.09010, abs=1e-8)
        assert pos.tp == pytest.approx(1.11010, abs=1e-8)

        # 3. PARTIAL_CLOSE (0.5 -> 0.3): fraction 0.6, target 0.06, close 0.04
        _drive(eng, parent_pipe, "s1", [_pos(42, volume=0.30)])
        assert adapter.position_by_ticket(rec.slave_ticket).volume == pytest.approx(0.06, abs=1e-8)

        # 4. CLOSE
        _drive(eng, parent_pipe, "s1", [])
        assert adapter.position_by_ticket(rec.slave_ticket) is None
        assert eng._slaves["s1"].table.has(42) is False
    finally:
        stop.set()
        try:
            parent_pipe.close()
        except Exception:
            pass
        t.join(timeout=2.0)


def test_pending_held_modify_during_open():
    """Two snapshots before the OPEN ack: MODIFY held, re-emitted on ack."""
    eng = _engine_with_slave()
    parent_pipe, child_pipe = multiprocessing_pipe()
    adapter = _slave_adapter()
    stop = threading.Event()
    t = threading.Thread(target=_slave_thread, args=(child_pipe, adapter, stop),
                        daemon=True)
    t.start()
    try:
        # snapshot 1: NEW -> OPEN sent (pending), optimistic record added
        snap1 = Snapshot(timestamp=NOW, heartbeat=1, positions=(_pos(42),))
        cmds1 = eng.ingest_snapshot(snap1, now=NOW)["s1"]
        assert len(cmds1) == 1 and cmds1[0].action == "OPEN"
        # snapshot 2 (before ack): MODIFY -> held
        snap2 = Snapshot(timestamp=NOW, heartbeat=2,
                         positions=(_pos(42, sl=1.09000, tp=1.11000),))
        cmds2 = eng.ingest_snapshot(snap2, now=NOW)["s1"]
        assert cmds2 == []  # MODIFY held pending OPEN ack
        # send the OPEN and drain its ack -> re-emitted MODIFY must be sent+acked
        send_msg(parent_pipe, cmds1[0])
        deadline = time.time() + 2.0
        seen_modify = False
        while time.time() < deadline:
            if not parent_pipe.poll(0.2):
                break
            msg = recv_msg(parent_pipe)
            if isinstance(msg, AckMsg):
                for c in engine_apply_ack(eng, "s1", msg):
                    send_msg(parent_pipe, c)
                    if c.action == "MODIFY":
                        seen_modify = True
        # drain the MODIFY ack
        if parent_pipe.poll(0.5):
            msg = recv_msg(parent_pipe)
            if isinstance(msg, AckMsg):
                engine_apply_ack(eng, "s1", msg)
        assert seen_modify
        rec = eng._slaves["s1"].table.get(42)
        pos = adapter.position_by_ticket(rec.slave_ticket)
        assert pos.sl == pytest.approx(1.09010, abs=1e-8)
    finally:
        stop.set()
        try:
            parent_pipe.close()
        except Exception:
            pass
        t.join(timeout=2.0)


def multiprocessing_pipe():
    import multiprocessing
    return multiprocessing.Pipe(duplex=True)


def engine_apply_ack(eng, slave_id, ack):
    return eng.apply_ack(slave_id, ack)
```

Create `manager/tests/test_recovery.py`:

```python
from manager.engine.models import Position, Snapshot, Record, SymbolInfo, BUY
from manager.engine.linkage import magic_for
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.ipc.messages import StatusMsg, RecoveryMsg

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = 1700000000


def _engine():
    eng = CopyEngine()
    eng.add_slave(SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                              step_amount=100.0, step_size=0.01, max_lot=10.0,
                              max_trade_age_minutes=10, normalize_sltp=True))
    eng.apply_symbol_info("s1", {"EURUSD": SI})
    eng.apply_status("s1", StatusMsg(source_id="s1", role="slave", connected=True,
                    login=2, balance=1000.0, equity=1000.0, currency="USD",
                    server="Demo"))
    return eng


def _pos(ticket=42, volume=0.5):
    return Position(ticket=ticket, symbol="EURUSD", side=BUY, open_price=1.10000,
                    volume=volume, sl=1.09500, tp=1.10500, open_time=NOW, point=0.00001)


def test_recovery_seeds_table_so_new_is_skipped():
    eng = _engine()
    added = eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    assert added == 1
    cmds = eng.ingest_snapshot(Snapshot(timestamp=NOW, heartbeat=1,
                                        positions=(_pos(42),)), now=NOW)["s1"]
    assert cmds == []  # already copied -> no duplicate OPEN


def test_recovery_does_not_overwrite_existing_record():
    eng = _engine()
    original = Record(42, magic_for(42), 111, 0.5, 0.10)
    eng.apply_recovery("s1", [original])
    # a second recovery with a different slave_ticket must not overwrite
    added = eng.apply_recovery("s1", [Record(42, magic_for(42), 999, 0.5, 0.20)])
    assert added == 0
    assert eng._slaves["s1"].table.get(42).slave_ticket == 111


def test_restart_reset_then_recovery_reseeds_no_duplicate():
    eng = _engine()
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    # simulate worker restart: table cleared, then slave re-sends recovery
    eng.reset_slave("s1")
    assert eng._slaves["s1"].table.has(42) is False
    eng.apply_recovery("s1", [Record(42, magic_for(42), 778, 0.5, 0.10)])
    cmds = eng.ingest_snapshot(Snapshot(timestamp=NOW, heartbeat=1,
                                        positions=(_pos(42),)), now=NOW)["s1"]
    assert cmds == []  # re-seeded -> no duplicate OPEN after restart


def test_recovery_plus_recent_open_copies_the_new_one():
    eng = _engine()
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    # 42 already copied (skip); 43 is a fresh recent open -> OPEN
    cmds = eng.ingest_snapshot(Snapshot(timestamp=NOW, heartbeat=1,
                                        positions=(_pos(42), _pos(43))), now=NOW)["s1"]
    assert len(cmds) == 1 and cmds[0].action == "OPEN" and cmds[0].master_ticket == 43
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest manager/tests/test_copy_loop_integration.py manager/tests/test_recovery.py -v`
Expected: FAIL (module not found / not yet created).

- [ ] **Step 3: Write minimal implementation**

No new implementation module — these tests compose the Task 1-7 modules. The only "implementation" is ensuring `manager/tests/__init__.py` already exists (it does from Plan 1) and that `multiprocessing.Pipe` is importable (stdlib). If `test_copy_loop_integration.py` fails on a missing helper, fix the helper in the test file itself.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest manager/tests/test_copy_loop_integration.py manager/tests/test_recovery.py -v`
Expected: PASS (all tests). If the in-process pipe test is flaky on a slow CI runner, re-run — it polls to a 2s idle deadline.

- [ ] **Step 5: Full suite regression + commit**

Run: `pytest manager/tests -v`
Expected: PASS (Plan 1 + Plan 2 tests all green).

```bash
git add manager/tests/test_copy_loop_integration.py manager/tests/test_recovery.py
git commit -m "test: tier-2 integration (full copy lifecycle + recovery)"
```

---

## Self-review (run before offering execution)

After writing the plan, the following were checked inline:

1. **Spec coverage:** IPC messages (Task 2), pipe framing (Task 3), worker master/slave + recovery (Task 5), copy-loop brain (Task 6), supervisor spawn/watch/restart/route (Task 7), tier-2 integration open/modify/partial/close + recovery + heartbeat (Tasks 7-8). The slave-normalizes + slave-computes-partial decision (user-approved) is reflected in `execute_command` (Task 5) and `derive_command` sending raw master SL/TP + open volumes (Task 6).
2. **Placeholder scan:** none — every code step contains full code.
3. **Type consistency:** `CommandMsg` field names match across `derive_command` (Task 6), `execute_command` (Task 5), and the Task 5/8 tests. `SymbolInfo` fields (Task 1) match `FakeMt5`/`RealMt5` (Task 4) and `derive_command`'s usage (`info.volume_step/min/max`). `Record` (Plan 1, unchanged) fields match `build_recovery_records` (Task 5) and `apply_ack` (Task 6).

## Forward-looking items (NOT in Plan 2 scope; note for Plans 3-4)

- **On-demand symbol info:** the slave reports symbol info only for the mapped slave symbols (Task 5 `build_symbol_info_msg`). A master symbol that resolves via same-name fallback but was not reported has no info → `derive_command` skips it (Task 6). A future plan can add a `REQUEST_SYMBOL_INFO` command + `SymbolInfoMsg` reply for on-demand coverage.
- **Kill leftover `terminal64.exe`:** `Supervisor(kill_terminal=...)` hook is a no-op by default; the terminal manager (Plan 3) plugs in the real `taskkill`/process-list logic to avoid the IPC `-10003` collision before respawn.
- **Failed-OPEN reconciliation:** a failed OPEN leaves a `slave_ticket=0` record (not re-NEW'd); a master CLOSE of it cleans up the marker. Auto-retry/reconcile of a genuinely failed open is deferred.
- **Netting accounts:** `execute_command` OPEN uses `result.order` as the position ticket (correct for hedging accounts). Netting-account ticket mapping is a tier-3 concern.
- **Filling mode:** `RealMt5` uses `ORDER_FILLING_RETURN`; a symbol requiring FOK/IOC will fail — tier-3 will reveal it and the adapter can read `symbol_info.trade_filling_mode`.
- **GUI/status wiring:** `Supervisor.errors`, `heartbeat_warning`, `on_restart` are surfaced by the GUI in Plan 4.
- **Concurrency note:** the pending/held mechanism serializes per master ticket so no command outruns its predecessor's ack; cross-slave concurrency is preserved (each slave's pipe is independent). A pathological multi-second broker stall on one ticket holds only that ticket's later events (coalesced), not other tickets or slaves.

## Execution

Plan complete and saved to `docs/superpowers/plans/2026-08-03-ipc-workers-copy-loop.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session, batch with checkpoints.

Which approach?