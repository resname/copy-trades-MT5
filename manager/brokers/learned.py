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