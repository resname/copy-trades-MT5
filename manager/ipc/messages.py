from __future__ import annotations

from dataclasses import dataclass, fields

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