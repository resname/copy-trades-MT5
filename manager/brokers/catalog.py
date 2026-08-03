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